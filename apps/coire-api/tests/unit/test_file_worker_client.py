"""Typed private worker client never accepts caller URLs or leaks worker bodies."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from pydantic import SecretStr

from coire_api.file_worker_client import (
    FileWorkerBusy,
    FileWorkerClient,
    FileWorkerError,
    FileWorkerMissing,
)
from coire_core.models.files import FileProcessRequest, FileProcessStatus
from coire_core.settings import Settings

JOB_ID = "01K00000000000000000000000"


def _settings(token: str = "private-worker-token") -> Settings:
    return Settings(  # type: ignore[call-arg]
        _secrets_dir="/nonexistent",
        file_worker_url="http://worker",
        file_worker_service_token=SecretStr(token),
    )


def _request() -> FileProcessRequest:
    return FileProcessRequest(
        job_id=JOB_ID,
        input_id=uuid.uuid4(),
        source_sha256="a" * 64,
        operation="inspect",
        output_ids=[uuid.uuid4()],
        deadline_at=datetime.now(UTC) + timedelta(seconds=20),
    )


async def test_typed_process_status_cancel_and_scoped_bearer() -> None:
    seen: list[tuple[str, str]] = []
    request = _request()

    def handler(call: httpx.Request) -> httpx.Response:
        assert call.headers["Authorization"] == "Bearer private-worker-token"
        assert call.url.host == "worker"
        seen.append((call.method, call.url.path))
        return httpx.Response(
            202 if call.url.path == "/v1/process" else 200,
            json=FileProcessStatus(
                job_id=JOB_ID, state="running", updated_at=datetime.now(UTC)
            ).model_dump(mode="json"),
        )

    async with (
        httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="http://worker"
        ) as transport,
        FileWorkerClient(_settings(), client=transport) as client,
    ):
        assert (await client.process(request)).state == "running"
        assert (await client.status(JOB_ID)).job_id == JOB_ID
        assert (await client.cancel(JOB_ID)).job_id == JOB_ID
    assert seen == [
        ("POST", "/v1/process"),
        ("GET", f"/v1/jobs/{JOB_ID}"),
        ("POST", f"/v1/jobs/{JOB_ID}/cancel"),
    ]


@pytest.mark.parametrize(
    "code,error", [(429, FileWorkerBusy), (404, FileWorkerMissing), (500, FileWorkerError)]
)
async def test_worker_refusals_are_content_free(code: int, error: type[Exception]) -> None:
    async with (
        httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _call: httpx.Response(code, text="private stack trace")
            ),
            base_url="http://worker",
        ) as transport,
        FileWorkerClient(_settings(), client=transport) as client,
    ):
        with pytest.raises(error) as caught:
            await client.status(JOB_ID)
    assert "private stack trace" not in str(caught.value)


async def test_worker_identity_and_schema_are_verified() -> None:
    mismatch = FileProcessStatus(
        job_id="01K00000000000000000000001", state="failed", updated_at=datetime.now(UTC)
    ).model_dump(mode="json")
    for payload in (mismatch, {"unexpected": "body"}):
        async with (
            httpx.AsyncClient(
                transport=httpx.MockTransport(
                    lambda _call, selected=payload: httpx.Response(200, json=selected)
                ),
                base_url="http://worker",
            ) as transport,
            FileWorkerClient(_settings(), client=transport) as client,
        ):
            with pytest.raises(FileWorkerError):
                await client.status(JOB_ID)


async def test_missing_token_cannot_call_worker() -> None:
    called = False

    def handler(_call: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200)

    async with (
        httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="http://worker"
        ) as transport,
        FileWorkerClient(_settings(""), client=transport) as client,
    ):
        with pytest.raises(FileWorkerError, match="worker unavailable"):
            await client.status(JOB_ID)
    assert not called
