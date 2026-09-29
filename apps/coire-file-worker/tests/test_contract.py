"""Private worker route, authentication and immutable-job contracts."""

from __future__ import annotations

import asyncio
import hashlib
import os
import threading
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from coire_core.models.files import FileProcessRequest, FileProcessResult
from coire_core.settings import Settings
from coire_file_worker.app import Worker, create_app
from coire_file_worker.processor import FileProcessingError

TOKEN = "worker-test-token"
JOB_ID = "01K00000000000000000000000"
OTHER_JOB_ID = "01K00000000000000000000001"


def _settings(tmp_path: Path) -> Settings:
    original = tmp_path / "originals"
    original.mkdir()
    return Settings(
        file_worker_service_token=SecretStr(TOKEN),
        file_worker_input_root=str(original),
        file_worker_output_root=str(tmp_path / "derived"),
    )


def _request(settings: Settings, *, job_id: str = JOB_ID) -> FileProcessRequest:
    data = b"private text"
    file_id = uuid.uuid4()
    (Path(settings.file_worker_input_root) / str(file_id)).write_bytes(data)
    return FileProcessRequest(
        job_id=job_id,
        input_id=file_id,
        source_sha256=hashlib.sha256(data).hexdigest(),
        operation="inspect",
        deadline_at=datetime.now(UTC) + timedelta(seconds=20),
    )


async def _status(client: httpx.AsyncClient, job_id: str) -> dict[str, object]:
    for _ in range(100):
        response = await client.get(f"/v1/jobs/{job_id}")
        assert response.status_code == 200
        body: dict[str, object] = response.json()
        if body["state"] != "running":
            return body
        await asyncio.sleep(0.01)
    pytest.fail("worker job did not finish")


@pytest.mark.asyncio
async def test_all_routes_require_dedicated_token(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    request = _request(settings)
    app = create_app(settings)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://worker"
    ) as client:
        for method, path, body in (
            ("GET", "/health", None),
            ("POST", "/v1/process", request.model_dump(mode="json")),
            ("GET", f"/v1/jobs/{JOB_ID}", None),
            ("POST", f"/v1/jobs/{JOB_ID}/cancel", {"job_id": JOB_ID}),
            ("DELETE", f"/v1/jobs/{JOB_ID}/output", None),
        ):
            response = await client.request(method, path, json=body)
            assert response.status_code == 401
            response = await client.request(
                method, path, json=body, headers={"Authorization": "Bearer wrong"}
            )
            assert response.status_code == 401
        client.headers["Authorization"] = f"Bearer {TOKEN}"
        assert (await client.get("/health")).json() == {"status": "ok"}
        response = await client.post(
            "/v1/process", json={**request.model_dump(mode="json"), "path": "/etc/passwd"}
        )
        assert response.status_code == 422


@pytest.mark.asyncio
async def test_processing_result_and_immutable_idempotency(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    request = _request(settings)
    app = create_app(settings)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://worker",
        headers={"Authorization": f"Bearer {TOKEN}"},
    ) as client:
        payload = request.model_dump(mode="json")
        accepted = await client.post("/v1/process", json=payload)
        assert accepted.status_code == 202
        result = await _status(client, JOB_ID)
        assert result["state"] == "processed"
        assert result["result"] == FileProcessResult(
            job_id=JOB_ID,
            input_id=request.input_id,
            source_sha256=request.source_sha256,
            detected_type="text/plain",
            extracted_text="private text",
            assets=[],
        ).model_dump(mode="json")
        duplicate = await client.post("/v1/process", json=payload)
        assert duplicate.status_code == 202
        assert duplicate.json() == result
        conflict = await client.post("/v1/process", json={**payload, "source_sha256": "0" * 64})
        assert conflict.status_code == 409
        assert (await client.get("/v1/jobs/not-a-ulid")).status_code == 404
        mismatch = await client.post(f"/v1/jobs/{JOB_ID}/cancel", json={"job_id": OTHER_JOB_ID})
        assert mismatch.status_code == 409


@pytest.mark.asyncio
async def test_one_active_job_and_cancel_discards_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    request = _request(settings)
    second = _request(settings, job_id=OTHER_JOB_ID)
    started = threading.Event()
    release = threading.Event()

    def blocked_process(*_args: object) -> FileProcessResult:
        started.set()
        assert release.wait(2)
        return FileProcessResult(
            job_id=JOB_ID,
            input_id=request.input_id,
            source_sha256=request.source_sha256,
            detected_type="text/plain",
            extracted_text="hidden",
            assets=[],
        )

    monkeypatch.setattr("coire_file_worker.app.process_file", blocked_process)
    app = create_app(settings)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://worker",
        headers={"Authorization": f"Bearer {TOKEN}"},
    ) as client:
        assert (
            await client.post("/v1/process", json=request.model_dump(mode="json"))
        ).status_code == 202
        assert await asyncio.to_thread(started.wait, 2)
        assert (
            await client.post("/v1/process", json=second.model_dump(mode="json"))
        ).status_code == 429
        assert (await client.delete(f"/v1/jobs/{JOB_ID}/output")).status_code == 409
        cancelled = await client.post(f"/v1/jobs/{JOB_ID}/cancel", json={"job_id": JOB_ID})
        assert cancelled.status_code == 200
        assert cancelled.json()["state"] == "cancelled"
        release.set()
        for _ in range(100):
            if app.state.worker.active_job is None:
                break
            await asyncio.sleep(0.01)
        assert app.state.worker.active_job is None
        assert (await client.get(f"/v1/jobs/{JOB_ID}")).json()["result"] is None
        assert not (Path(settings.file_worker_output_root) / JOB_ID).exists()


async def test_output_purge_survives_worker_restart_and_is_idempotent(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    folder = Path(settings.file_worker_output_root) / JOB_ID
    folder.mkdir(parents=True)
    (folder / f"{uuid.uuid4()}.png").write_bytes(b"private output")
    app = create_app(settings)  # Fresh worker has no in-memory job status.
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://worker",
        headers={"Authorization": f"Bearer {TOKEN}"},
    ) as client:
        assert (await client.delete("/v1/jobs/not-a-ulid/output")).status_code == 404
        first = await client.delete(f"/v1/jobs/{JOB_ID}/output")
        assert first.status_code == 200
        assert first.json() == {"job_id": JOB_ID, "state": "purged"}
        assert not folder.exists()
        assert (await client.delete(f"/v1/jobs/{JOB_ID}/output")).status_code == 200


@pytest.mark.asyncio
async def test_safe_failure_and_no_automatic_retry(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    request = _request(settings)
    (Path(settings.file_worker_input_root) / str(request.input_id)).write_bytes(b"altered")
    app = create_app(settings)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://worker",
        headers={"Authorization": f"Bearer {TOKEN}"},
    ) as client:
        payload = request.model_dump(mode="json")
        assert (await client.post("/v1/process", json=payload)).status_code == 202
        failure = await _status(client, JOB_ID)
        assert failure["state"] == "failed"
        assert failure["safe_error"] == "original_digest_mismatch"
        assert "altered" not in str(failure)
        assert (await client.post("/v1/process", json=payload)).json() == failure


def test_native_watchdog_arms_and_disarms(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(tmp_path)
    request = _request(settings)
    calls: list[str] = []

    class Timer:
        def __init__(self, interval: float, function: object, args: tuple[int, ...]) -> None:
            assert 0 < interval <= 20
            assert function is os._exit
            assert args == (124,)
            self.daemon = False

        def start(self) -> None:
            calls.append("start")

        def cancel(self) -> None:
            calls.append("cancel")

    monkeypatch.setattr("coire_file_worker.app.threading.Timer", Timer)
    worker = Worker(settings)
    result = worker._process_with_watchdog(request)
    assert result.extracted_text == "private text"
    assert calls == ["start", "cancel"]
    expired = request.model_copy(update={"deadline_at": datetime.now(UTC) - timedelta(seconds=1)})
    with pytest.raises(FileProcessingError, match="deadline_exceeded"):
        worker._process_with_watchdog(expired)
