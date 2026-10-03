"""Private transfer proves exact fenced bytes before core issues a cleanup receipt."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient
from PIL import Image
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.db import ImageJobRow, ImageTransferRow, NodeRow, get_session
from coire_api.images.transfer import mint_transfer_grant
from coire_api.routes import internal_images
from coire_core.errors import CoireError, ImageConflict
from coire_core.models.image_worker import (
    ImageTransferGrantRequest,
    ImageWorkerOutputManifest,
    NodeImageJob,
    NodeImageStartRequest,
    NodeImageTransferRequest,
)
from coire_core.models.images import (
    ImageJobSettingsSnapshot,
    ImageSpec,
    ResolvedImageSpec,
    canonical_recipe_bytes,
    canonical_spec_hash,
)
from coire_core.settings import Settings, get_settings
from coire_node.image_runtime.metadata import write_image_png
from coire_node.image_transfer import push_image_outputs

JOB = "01K00000000000000000000000"
NODE = "coire-studio-b"


class FakeSession:
    def __init__(self, resolved: ResolvedImageSpec) -> None:
        self.node = NodeRow(id=uuid.uuid4(), name=NODE)
        self.job = ImageJobRow(
            id=JOB,
            state="transferring",
            selected_node_id=self.node.id,
            attempt=1,
            fence=3,
            cancel_requested_at=None,
            resolved_spec=ImageJobSettingsSnapshot(
                effective_spec=resolved.spec, resolved=resolved
            ).model_dump(mode="json"),
        )
        self.transfer: ImageTransferRow | None = None
        self.commits = 0

    async def get(self, model: type[object], identity: object, **_: object) -> Any:
        if model is ImageJobRow:
            return self.job if identity == JOB else None
        if model is NodeRow:
            return self.node if identity == self.node.id else None
        if model is ImageTransferRow:
            return self.transfer if identity == (JOB, 1, 0) else None
        raise AssertionError(model)

    def add(self, row: object) -> None:
        assert isinstance(row, ImageTransferRow)
        self.transfer = row

    async def commit(self) -> None:
        self.commits += 1


def _resolved() -> ResolvedImageSpec:
    spec = ImageSpec(
        model_id=uuid.uuid4(),
        prompt="private prompt",
        width=64,
        height=64,
        steps=4,
        guidance=Decimal("0"),
        seed=7,
    )
    return ResolvedImageSpec(
        spec=spec,
        seeds=(7,),
        pipeline_version="mflux-0.20.0",
        environment_fingerprint="a" * 64,
        model_sha256="b" * 64,
        spec_hash=canonical_spec_hash(spec),
    )


def _app(session: FakeSession, settings: Settings) -> FastAPI:
    app = FastAPI()
    app.include_router(internal_images.router)
    app.dependency_overrides[get_settings] = lambda: settings

    async def session_dependency() -> Any:
        yield session

    app.dependency_overrides[get_session] = session_dependency

    @app.exception_handler(CoireError)
    async def problem(_: Request, exc: CoireError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status, content=exc.to_problem().model_dump(mode="json")
        )

    return app


@pytest.mark.asyncio
async def test_unbound_queue_settings_cannot_mint_transfer_grant() -> None:
    resolved = _resolved()
    session = FakeSession(resolved)
    session.job.resolved_spec = ImageJobSettingsSnapshot(effective_spec=resolved.spec).model_dump(
        mode="json"
    )
    with pytest.raises(ImageConflict, match="runtime is not bound"):
        await mint_transfer_grant(
            cast(AsyncSession, session),
            ImageTransferGrantRequest(
                job_id=JOB,
                attempt=1,
                fence=3,
                node=NODE,
                index=0,
                expected_bytes=100,
                expected_sha256="a" * 64,
            ),
        )


@pytest.mark.asyncio
async def test_transfer_requires_node_and_grant_then_replays_exact_receipt(tmp_path: Path) -> None:
    resolved = _resolved()
    source = tmp_path / "source.png"
    written = write_image_png(Image.new("RGB", (64, 64), (1, 2, 3)), resolved, 0, source)
    settings = Settings(  # type: ignore[call-arg]
        _secrets_dir="/nonexistent",
        image_blob_root=str(tmp_path / "blobs"),
        node_tokens=SecretStr(json.dumps({NODE: "node-secret"})),
    )
    session = FakeSession(resolved)
    grant = await mint_transfer_grant(
        cast(AsyncSession, session),
        ImageTransferGrantRequest(
            job_id=JOB,
            attempt=1,
            fence=3,
            node=NODE,
            index=0,
            expected_bytes=written.byte_count,
            expected_sha256=written.sha256,
        ),
    )
    await session.commit()
    url = f"/api/v1/internal/images/{JOB}/outputs/0"
    headers = {
        "Authorization": "Bearer node-secret",
        "x-coire-node": NODE,
        "x-coire-attempt": "1",
        "x-coire-fence": "3",
        "x-coire-transfer-grant": grant.token,
    }
    async with AsyncClient(
        transport=ASGITransport(app=_app(session, settings)), base_url="http://test"
    ) as client:
        assert session.transfer is not None
        session.transfer.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        expired = await client.put(url, content=source.read_bytes(), headers=headers)
        session.transfer.lease_expires_at = grant.expires_at
        wrong_grant = await client.put(
            url,
            content=source.read_bytes(),
            headers={**headers, "x-coire-transfer-grant": "other-token"},
        )
        wrong_node = await client.put(
            url, content=source.read_bytes(), headers={**headers, "Authorization": "Bearer bad"}
        )
        stale = await client.put(
            url, content=source.read_bytes(), headers={**headers, "x-coire-fence": "4"}
        )
        bad_bytes = await client.put(url, content=b"not a png", headers=headers)
        accepted = await client.put(url, content=source.read_bytes(), headers=headers)
        replay = await client.put(url, content=b"ignored after durable receipt", headers=headers)
        staged = tmp_path / "blobs" / "image-staging" / JOB / "1" / "0.png"
        assert staged.read_bytes() == source.read_bytes()
        staged.unlink()
        missing = await client.put(url, content=source.read_bytes(), headers=headers)
    assert expired.status_code == 409
    assert wrong_grant.status_code == 409
    assert wrong_node.status_code == 401
    assert stale.status_code == 409
    assert bad_bytes.status_code == 422
    assert accepted.status_code == 201
    assert replay.status_code == 200
    assert missing.status_code == 507
    assert replay.json() == accepted.json()
    assert (
        accepted.json()["recipe_sha256"]
        == hashlib.sha256(canonical_recipe_bytes(written.recipe)).hexdigest()
    )
    assert not list((tmp_path / "blobs" / "image-staging" / JOB / "1").glob("*.uploading"))
    assert session.commits == 3


@pytest.mark.asyncio
async def test_node_pushes_only_manifest_bound_scratch_to_core(tmp_path: Path) -> None:
    resolved = _resolved()
    state_dir = tmp_path / "node"
    state_dir.mkdir(mode=0o700)
    scratch_root = state_dir / "image-scratch"
    scratch_root.mkdir(mode=0o700)
    scratch = scratch_root / f"{JOB}-1-3"
    scratch.mkdir(mode=0o700)
    written = write_image_png(Image.new("RGB", (64, 64), (4, 5, 6)), resolved, 0, scratch / "0.png")
    api_settings = Settings(  # type: ignore[call-arg]
        _secrets_dir="/nonexistent",
        image_blob_root=str(tmp_path / "blobs"),
        node_tokens=SecretStr(json.dumps({NODE: "node-secret"})),
    )
    node_settings = Settings(  # type: ignore[call-arg]
        _secrets_dir="/nonexistent",
        node_name=NODE,
        node_token=SecretStr("node-secret"),
        node_state_dir=str(state_dir),
        image_transfer_api_url="http://test",
    )
    session = FakeSession(resolved)
    grant = await mint_transfer_grant(
        cast(AsyncSession, session),
        ImageTransferGrantRequest(
            job_id=JOB,
            attempt=1,
            fence=3,
            node=NODE,
            index=0,
            expected_bytes=written.byte_count,
            expected_sha256=written.sha256,
        ),
    )
    await session.commit()
    instance_id = uuid.uuid4()
    original = NodeImageStartRequest(
        job_id=JOB,
        attempt=1,
        fence=3,
        node=NODE,
        model_id=resolved.spec.model_id,
        instance_id=instance_id,
        resolved=resolved,
        deadline_at=datetime.now(UTC) + timedelta(minutes=2),
        reservation_bytes=1024,
    )
    current = NodeImageJob(
        job_id=JOB,
        attempt=1,
        fence=3,
        node=NODE,
        instance_id=instance_id,
        state="transferring",
        outputs=(
            ImageWorkerOutputManifest(
                index=0,
                byte_count=written.byte_count,
                sha256=written.sha256,
                recipe_sha256=hashlib.sha256(canonical_recipe_bytes(written.recipe)).hexdigest(),
            ),
        ),
        updated_at=datetime.now(UTC),
    )
    command = NodeImageTransferRequest(
        job_id=JOB,
        attempt=1,
        fence=3,
        node=NODE,
        grants=(grant,),
    )
    result = await push_image_outputs(
        state_dir,
        current,
        original,
        command,
        node_settings,
        transport=ASGITransport(app=_app(session, api_settings)),
    )
    assert len(result) == 1
    assert result[0].sha256 == written.sha256
    assert scratch.joinpath("0.png").exists()
    assert (
        tmp_path / "blobs" / "image-staging" / JOB / "1" / "0.png"
    ).read_bytes() == scratch.joinpath("0.png").read_bytes()
