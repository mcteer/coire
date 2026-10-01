"""Scheduler rejects private parser responses that do not bind to the staged input."""

from __future__ import annotations

import json
import uuid
from decimal import Decimal

import httpx
import pytest
from pydantic import SecretStr

from coire_api.file_worker_client import (
    FileWorkerBusy,
    FileWorkerClient,
    FileWorkerError,
    FileWorkerParseRefused,
)
from coire_core.models.image_worker import ImageRecipeParseRequest, ImageRecipeParseResult
from coire_core.models.images import (
    ImageRecipe,
    ImageSpec,
    ResolvedImageSpec,
    canonical_spec_hash,
)
from coire_core.settings import Settings


def _request() -> ImageRecipeParseRequest:
    return ImageRecipeParseRequest(input_id=uuid.uuid4(), source_sha256="a" * 64, byte_count=123)


def _result(request: ImageRecipeParseRequest) -> ImageRecipeParseResult:
    spec = ImageSpec(
        model_id=uuid.uuid4(),
        prompt="private",
        seed=7,
        width=512,
        height=512,
        steps=20,
        guidance=Decimal("3"),
    )
    recipe = ImageRecipe(
        resolved=ResolvedImageSpec(
            spec=spec,
            seeds=(7,),
            pipeline_version="mflux-0.20.0",
            environment_fingerprint="b" * 64,
            model_sha256="c" * 64,
            spec_hash=canonical_spec_hash(spec),
        ),
        output_index=0,
        seed=7,
        pixel_sha256="d" * 64,
        width=512,
        height=512,
    )
    return ImageRecipeParseResult(
        input_id=request.input_id,
        source_sha256=request.source_sha256,
        byte_count=request.byte_count,
        recipe=recipe,
    )


def _client(handler: httpx.MockTransport) -> FileWorkerClient:
    settings = Settings(file_worker_service_token=SecretStr("private-token"))
    return FileWorkerClient(
        settings,
        client=httpx.AsyncClient(transport=handler, base_url="http://worker"),
    )


@pytest.mark.asyncio
async def test_recipe_client_sends_token_and_returns_bound_result() -> None:
    request = _request()
    result = _result(request)

    def respond(http_request: httpx.Request) -> httpx.Response:
        assert http_request.url.path == "/v1/image-recipes/parse"
        assert http_request.headers["Authorization"] == "Bearer private-token"
        assert json.loads(http_request.content)["input_id"] == str(request.input_id)
        return httpx.Response(200, json=result.model_dump(mode="json"))

    transport = httpx.MockTransport(respond)
    client = _client(transport)
    async with client:
        assert await client.parse_image_recipe(request) == result


@pytest.mark.asyncio
async def test_recipe_client_rejects_wrong_identity_and_malformed_body() -> None:
    request = _request()
    good = _result(request).model_dump(mode="json")
    wrong_id = {**good, "input_id": str(uuid.uuid4())}
    wrong_size = {**good, "byte_count": request.byte_count + 1}
    wrong_digest = {**good, "source_sha256": "0" * 64}
    for payload in (wrong_id, wrong_size, wrong_digest, {"private": "content"}):
        client = _client(
            httpx.MockTransport(lambda _, body=payload: httpx.Response(200, json=body))
        )
        async with client:
            with pytest.raises(FileWorkerError, match="recipe worker"):
                await client.parse_image_recipe(request)


@pytest.mark.asyncio
async def test_recipe_client_distinguishes_busy_and_refused_without_body() -> None:
    request = _request()
    for status, error in (
        (429, FileWorkerBusy),
        (422, FileWorkerParseRefused),
        (503, FileWorkerError),
    ):
        client = _client(
            httpx.MockTransport(lambda _, code=status: httpx.Response(code, text="private prompt"))
        )
        async with client:
            with pytest.raises(error) as caught:
                await client.parse_image_recipe(request)
            assert "private prompt" not in str(caught.value)
