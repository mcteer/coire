"""Owner and live entitlement are required at grant issue and content redemption."""

from __future__ import annotations

import hashlib
import os
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.auth import Principal, PrincipalKind, require_principal
from coire_api.db import (
    ApiKeyRow,
    ImageDownloadGrantRow,
    ImageOutputRow,
    UserRow,
    get_session,
)
from coire_api.images import authorization, downloads
from coire_api.routes import image_outputs
from coire_core.errors import CoireError, ImageForbidden, ImageNotFound, ImageStorageUnavailable
from coire_core.settings import Settings

OWNER = uuid.uuid4()
OTHER = uuid.uuid4()
KEY1 = uuid.uuid4()
KEY2 = uuid.uuid4()
ORIGIN = "https://coire.test"


def _output(*, explicit: bool = False, blob_key: str = "outputs/sample.png") -> ImageOutputRow:
    return ImageOutputRow(
        id=uuid.uuid4(),
        owner_user_id=OWNER,
        job_id="01K00000000000000000000000",
        output_index=0,
        blob_key=blob_key,
        size_bytes=7,
        file_sha256=hashlib.sha256(b"pngdata").hexdigest(),
        pixel_sha256="a" * 64,
        recipe={"resolved": {"spec": {"content_mode": "explicit" if explicit else "standard"}}},
        content_tag="explicit" if explicit else "normal",
        classifier_provenance={},
        entitlement_snapshot={},
        state="published",
        deleted_at=None,
        created_at=datetime.now(UTC),
    )


def _key(key_id: uuid.UUID, *, explicit: bool = True) -> Principal:
    scopes = {"images", "images:explicit"} if explicit else {"images"}
    return Principal(
        kind=PrincipalKind.API_KEY,
        user_id=OWNER,
        api_key_id=key_id,
        credential_version=3,
        scopes=frozenset(scopes),
    )


class FakeScalars:
    def __init__(self, names: list[str]) -> None:
        self.names = names

    def all(self) -> list[str]:
        return self.names


class FakeSession:
    def __init__(self, output: ImageOutputRow) -> None:
        self.output = output
        self.grants: dict[str, ImageDownloadGrantRow] = {}
        self.entitlements = ["explicit"]
        self.keys = {
            key_id: SimpleNamespace(
                user_id=OWNER,
                revoked_at=None,
                credential_version=3,
                scopes=["images", "images:explicit"],
            )
            for key_id in (KEY1, KEY2)
        }

    async def get(self, model: type[object], identity: object, **kwargs: object) -> object | None:
        if model is ImageOutputRow:
            return self.output if self.output.id == identity else None
        if model is UserRow:
            return SimpleNamespace(active=True)
        if model is ApiKeyRow:
            return self.keys.get(cast(uuid.UUID, identity))
        if model is ImageDownloadGrantRow:
            return self.grants.get(cast(str, identity))
        raise AssertionError(model)

    async def scalars(self, statement: object) -> FakeScalars:
        return FakeScalars(self.entitlements)

    def add(self, row: ImageDownloadGrantRow) -> None:
        self.grants[row.grant_hash] = row

    async def commit(self) -> None:
        pass


def _token(url: str) -> str:
    assert urlsplit(url).query == ""
    return parse_qs(urlsplit(url).fragment)["grant"][0]


async def test_grant_is_hashed_subject_bound_and_expires() -> None:
    output = _output()
    session = FakeSession(output)
    grant = await downloads.issue_download_grant(cast(AsyncSession, session), _key(KEY1), output.id)
    token = _token(grant.url)
    assert grant.output_id == output.id
    assert grant.expires_at <= datetime.now(UTC) + timedelta(minutes=5)
    assert token not in str(session.grants)
    assert len(session.grants) == 1
    assert (
        await downloads.redeem_download_grant(
            cast(AsyncSession, session), _key(KEY1), output.id, token
        )
        is output
    )
    with pytest.raises(ImageNotFound):
        await downloads.redeem_download_grant(
            cast(AsyncSession, session), _key(KEY2), output.id, token
        )
    next(iter(session.grants.values())).expires_at = datetime.now(UTC) - timedelta(seconds=1)
    with pytest.raises(ImageNotFound):
        await downloads.redeem_download_grant(
            cast(AsyncSession, session), _key(KEY1), output.id, token
        )


async def test_explicit_revocation_and_tombstone_block_redemption() -> None:
    output = _output(explicit=True)
    session = FakeSession(output)
    principal = _key(KEY1)
    grant = await downloads.issue_download_grant(cast(AsyncSession, session), principal, output.id)
    session.entitlements = []
    with pytest.raises(ImageForbidden):
        await downloads.redeem_download_grant(
            cast(AsyncSession, session), principal, output.id, _token(grant.url)
        )
    session.entitlements = ["explicit"]
    session.keys[KEY1].revoked_at = datetime.now(UTC)
    with pytest.raises(ImageForbidden):
        await downloads.redeem_download_grant(
            cast(AsyncSession, session), principal, output.id, _token(grant.url)
        )
    session.keys[KEY1].revoked_at = None
    session.keys[KEY1].scopes = ["images"]
    with pytest.raises(ImageForbidden):
        await downloads.redeem_download_grant(
            cast(AsyncSession, session), principal, output.id, _token(grant.url)
        )
    session.keys[KEY1].scopes = ["images", "images:explicit"]
    output.deleted_at = datetime.now(UTC)
    with pytest.raises(ImageNotFound):
        await downloads.redeem_download_grant(
            cast(AsyncSession, session), principal, output.id, _token(grant.url)
        )


def test_blob_open_rejects_escape_symlink_and_corruption(tmp_path: Path) -> None:
    output = _output()
    (tmp_path / "outputs").mkdir()
    (tmp_path / "outputs" / "sample.png").write_bytes(b"pngdata")
    fd = downloads.open_verified_blob(tmp_path, output)
    try:
        assert os.read(fd, 7) == b"pngdata"
    finally:
        os.close(fd)
    output.blob_key = "../secret.png"
    with pytest.raises(ImageStorageUnavailable):
        downloads.open_verified_blob(tmp_path, output)
    output.blob_key = "alias/sample.png"
    (tmp_path / "alias").symlink_to(tmp_path / "outputs", target_is_directory=True)
    with pytest.raises(ImageStorageUnavailable):
        downloads.open_verified_blob(tmp_path, output)
    output.blob_key = "outputs/link.png"
    (tmp_path / "outputs" / "link.png").symlink_to(tmp_path / "outputs" / "sample.png")
    with pytest.raises(ImageStorageUnavailable):
        downloads.open_verified_blob(tmp_path, output)
    output.blob_key = "outputs/sample.png"
    output.file_sha256 = "0" * 64
    with pytest.raises(ImageStorageUnavailable):
        downloads.open_verified_blob(tmp_path, output)


def _app(
    principal: Principal, session: FakeSession, root: Path, monkeypatch: pytest.MonkeyPatch
) -> FastAPI:
    app = FastAPI()
    app.state.settings = Settings(  # type: ignore[call-arg]
        _secrets_dir="/nonexistent", chat_browser_origin=ORIGIN, image_blob_root=str(root)
    )
    app.include_router(image_outputs.router)
    app.dependency_overrides[require_principal] = lambda: principal

    async def fake_get_session() -> Any:
        yield session

    app.dependency_overrides[get_session] = fake_get_session

    @asynccontextmanager
    async def fake_scope() -> Any:
        yield session

    monkeypatch.setattr(authorization, "session_scope", fake_scope)

    @app.exception_handler(CoireError)
    async def problem(request: Request, exc: CoireError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status, content=exc.to_problem().model_dump(mode="json")
        )

    return app


async def test_content_route_requires_credentials_and_grant_with_no_store_headers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = _output()
    session = FakeSession(output)
    (tmp_path / "outputs").mkdir()
    (tmp_path / "outputs" / "sample.png").write_bytes(b"pngdata")
    app = _app(_key(KEY1), session, tmp_path, monkeypatch)
    async with AsyncClient(transport=ASGITransport(app=app), base_url=ORIGIN) as client:
        issued = await client.post(f"/api/v1/image-outputs/{output.id}/download-grants")
        assert issued.status_code == 200
        url = issued.json()["url"]
        missing = await client.get(url)
        content = await client.get(urlsplit(url).path, headers={"X-Coire-Image-Grant": _token(url)})
    assert issued.headers["cache-control"] == "private, no-store"
    assert missing.status_code == 422
    assert content.status_code == 200
    assert content.content == b"pngdata"
    assert content.headers["cache-control"] == "private, no-store"
    assert content.headers["content-type"] == "image/png"


async def test_human_grant_requires_exact_origin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = _output()
    app = _app(
        Principal(kind=PrincipalKind.USER, user_id=OWNER),
        FakeSession(output),
        tmp_path,
        monkeypatch,
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url=ORIGIN) as client:
        denied = await client.post(f"/api/v1/image-outputs/{output.id}/download-grants")
        allowed = await client.post(
            f"/api/v1/image-outputs/{output.id}/download-grants", headers={"Origin": ORIGIN}
        )
    assert denied.status_code == 403
    assert allowed.status_code == 200
