"""Temporary visual reuse is scoped and validates the worker's immutable asset."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from PIL import Image

from coire_api.auth import Principal, PrincipalKind
from coire_api.chat.processing import InvalidAsset
from coire_api.db import ChatFileProcessingRow
from coire_api.gateway import temporary
from coire_core.models.files import FileProcessAsset, FileProcessRequest, FileProcessResult
from coire_core.models.gateway import ChatMessage, OpenAIImagePart, OpenAIImageURL
from coire_core.models.registry import VisualCapability
from coire_core.settings import Settings
from coire_file_worker.processor import process_file

JOB_ID = "01K00000000000000000000000"


def _image() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (16, 16), (255, 0, 0)).save(output, format="PNG")
    return output.getvalue()


def _visual(image: bytes) -> VisualCapability:
    return VisualCapability(
        verified=True, max_images=1, max_image_pixels=256, max_encoded_bytes=len(image)
    )


async def test_ready_temporary_asset_checks_worker_identity_and_digest(tmp_path: Path) -> None:
    image = _image()
    source_id, output_id = uuid.uuid4(), uuid.uuid4()
    digest = hashlib.sha256(image).hexdigest()
    directory = tmp_path / JOB_ID
    directory.mkdir()
    (directory / f"{output_id}.png").write_bytes(image)
    request = FileProcessRequest(
        job_id=JOB_ID,
        input_id=source_id,
        source_sha256=digest,
        operation="inspect",
        output_ids=[output_id],
        deadline_at=datetime.now(UTC) + timedelta(seconds=10),
    )
    result = FileProcessResult(
        job_id=JOB_ID,
        input_id=source_id,
        source_sha256=digest,
        detected_type="image/png",
        assets=[
            FileProcessAsset(
                id=output_id,
                sha256=digest,
                bytes=len(image),
                media_type="image/png",
                width=16,
                height=16,
            )
        ],
    )
    job = ChatFileProcessingRow(
        id=JOB_ID,
        attachment_id=None,
        principal_kind="user",
        principal_subject=str(uuid.uuid4()),
        request_id=uuid.uuid4(),
        operation="inspect",
        source_key=str(source_id),
        source_sha256=digest,
        selected_pages=[],
        output_manifest={
            "schema_version": temporary.TEMPORARY_SCHEMA_VERSION,
            "request": request.model_dump(mode="json"),
            "result": result.model_dump(mode="json"),
        },
        state="ready",
        attempt=1,
        deadline_at=request.deadline_at,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    settings = Settings(chat_derived_root=str(tmp_path), _secrets_dir="/nonexistent")  # type: ignore[call-arg]
    assert await temporary._ready_asset(job, settings, _visual(image)) == image
    (directory / f"{output_id}.png").write_bytes(b"private replacement")
    with pytest.raises(InvalidAsset):
        await temporary._ready_asset(job, settings, _visual(image))


async def test_temporary_request_is_normalized_by_real_cpu_worker(tmp_path: Path) -> None:
    image = _image()
    originals, derived = tmp_path / "originals", tmp_path / "derived"
    originals.mkdir()
    derived.mkdir()
    source_id, output_id = uuid.uuid4(), uuid.uuid4()
    (originals / str(source_id)).write_bytes(image)
    digest = hashlib.sha256(image).hexdigest()
    request = FileProcessRequest(
        job_id=JOB_ID,
        input_id=source_id,
        source_sha256=digest,
        operation="inspect",
        output_ids=[output_id],
        deadline_at=datetime.now(UTC) + timedelta(seconds=10),
    )
    result = process_file(request, originals, derived)
    job = ChatFileProcessingRow(
        id=JOB_ID,
        attachment_id=None,
        principal_kind="user",
        principal_subject=str(uuid.uuid4()),
        request_id=uuid.uuid4(),
        operation="inspect",
        source_key=str(source_id),
        source_sha256=digest,
        selected_pages=[],
        output_manifest={
            "schema_version": temporary.TEMPORARY_SCHEMA_VERSION,
            "request": request.model_dump(mode="json"),
            "result": result.model_dump(mode="json"),
        },
        state="ready",
        attempt=1,
        deadline_at=request.deadline_at,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    settings = Settings(chat_derived_root=str(derived), _secrets_dir="/nonexistent")  # type: ignore[call-arg]
    normalized = await temporary._ready_asset(job, settings, _visual(image))
    assert normalized.startswith(b"\x89PNG\r\n\x1a\n")
    assert result.assets[0].sha256 == hashlib.sha256(normalized).hexdigest()


async def test_normalizer_reuses_scoped_digest_before_submitting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image = _image()
    principal = Principal(kind=PrincipalKind.USER, user_id=uuid.uuid4())
    message = ChatMessage(
        role="user",
        content=[
            OpenAIImagePart(
                image_url=OpenAIImageURL(
                    url="data:image/png;base64," + base64.b64encode(image).decode()
                )
            )
        ],
    )
    calls: list[tuple[str, str]] = []

    async def cached(owner: Principal, digest: str, *_args: object) -> bytes:
        calls.append((str(owner.user_id), digest))
        return image

    async def submit(*_args: object) -> str:
        pytest.fail("validated principal+digest cache should avoid a second worker job")

    monkeypatch.setattr(temporary, "_cached", cached)
    monkeypatch.setattr(temporary, "_submit", submit)
    settings = Settings(_secrets_dir="/nonexistent")  # type: ignore[call-arg]
    result = await temporary.normalize_inline_images([message], principal, settings, _visual(image))
    assert calls == [(str(principal.user_id), hashlib.sha256(image).hexdigest())]
    assert isinstance(result[0].content, list)
    assert isinstance(result[0].content[0], OpenAIImagePart)
    assert result[0].content[0].image_url.url.endswith(base64.b64encode(image).decode())


async def test_cache_lookup_filters_principal_and_digest(monkeypatch: pytest.MonkeyPatch) -> None:
    image = _image()
    digest = hashlib.sha256(image).hexdigest()
    principal = Principal(kind=PrincipalKind.USER, user_id=uuid.uuid4())

    class Session:
        async def scalar(self, statement: object) -> None:
            params = statement.compile().params  # type: ignore[attr-defined]
            values = set(params.values())
            assert {principal.kind.value, str(principal.user_id), digest} <= values
            return None

    @asynccontextmanager
    async def sessions() -> AsyncIterator[Session]:
        yield Session()

    monkeypatch.setattr(temporary, "session_scope", sessions)
    assert (
        await temporary._cached(
            principal,
            digest,
            Settings(_secrets_dir="/nonexistent"),  # type: ignore[call-arg]
            _visual(image),
        )
        is None
    )


async def test_submit_stages_generated_original_for_durable_worker_job(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    image = _image()
    digest = hashlib.sha256(image).hexdigest()
    principal = Principal(kind=PrincipalKind.USER, user_id=uuid.uuid4())
    jobs: list[ChatFileProcessingRow] = []

    class Session:
        async def execute(self, _statement: object) -> None:
            return None

        async def scalar(self, _statement: object) -> int:
            return 0

        def add(self, job: ChatFileProcessingRow) -> None:
            jobs.append(job)

    @asynccontextmanager
    async def sessions() -> AsyncIterator[Session]:
        yield Session()

    monkeypatch.setattr(temporary, "session_scope", sessions)
    settings = Settings(chat_original_root=str(tmp_path), _secrets_dir="/nonexistent")  # type: ignore[call-arg]
    job_id = await temporary._submit(image, digest, principal, settings)
    assert len(jobs) == 1
    assert jobs[0].id == job_id and jobs[0].attachment_id is None
    assert jobs[0].principal_subject == str(principal.user_id)
    assert jobs[0].state == "queued"
    assert (tmp_path / jobs[0].source_key).read_bytes() == image


async def test_temporary_quota_refuses_ninth_job_and_removes_staged_original(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class Session:
        async def execute(self, _statement: object) -> None:
            return None

        async def scalar(self, _statement: object) -> int:
            return temporary.MAX_TEMPORARY_JOBS_PER_PRINCIPAL

        def add(self, _job: ChatFileProcessingRow) -> None:
            pytest.fail("quota refusal must precede job insert")

    @asynccontextmanager
    async def sessions() -> AsyncIterator[Session]:
        yield Session()

    monkeypatch.setattr(temporary, "session_scope", sessions)
    image = _image()
    settings = Settings(chat_original_root=str(tmp_path), _secrets_dir="/nonexistent")  # type: ignore[call-arg]
    with pytest.raises(temporary.TemporaryVisualQuotaExceeded):
        await temporary._submit(
            image,
            hashlib.sha256(image).hexdigest(),
            Principal(kind=PrincipalKind.API_KEY, api_key_id=uuid.uuid4()),
            settings,
        )
    assert await asyncio.to_thread(lambda: list(tmp_path.iterdir())) == []


async def test_temporary_failure_expires_job_for_ordered_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expired: list[str] = []

    async def fail(*_args: object) -> bytes:
        raise temporary.TemporaryVisualUnavailable("image processing unavailable")

    async def expire(job_id: str) -> None:
        expired.append(job_id)

    monkeypatch.setattr(temporary, "_poll_asset", fail)
    monkeypatch.setattr(temporary, "_expire_job", expire)
    image = _image()
    with pytest.raises(temporary.TemporaryVisualUnavailable):
        await temporary._wait_for_asset(
            JOB_ID,
            Settings(_secrets_dir="/nonexistent"),  # type: ignore[call-arg]
            _visual(image),
        )
    assert expired == [JOB_ID]
