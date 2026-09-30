"""A disposable Chat PDF render survives a worker killed after accepting the job."""

from __future__ import annotations

import os
import subprocess
import time
import uuid
from collections.abc import Callable
from io import BytesIO
from pathlib import Path

import httpx
import pytest
from PIL import Image

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("COIRE_INTEGRATION") != "1"
        or os.environ.get("COIRE_CHAT_ENABLED") != "true"
        or "chat-files" not in os.environ.get("COMPOSE_PROFILES", "").split(",")
        or not os.environ.get("COIRE_CHAT_PUBLIC_ORIGIN"),
        reason="run in disposable Chat Compose with the private file worker",
    ),
]

COMPOSE_DIR = Path(__file__).resolve().parents[2] / "deploy/compose"
COMPOSE = ["docker", "compose", "-p", "coire-it"]


def _sql(statement: str) -> str:
    result = subprocess.run(
        [
            *COMPOSE,
            "exec",
            "-T",
            "postgres",
            "psql",
            "-U",
            "coire",
            "-d",
            "coire",
            "-v",
            "ON_ERROR_STOP=1",
            "-Atc",
            statement,
        ],
        cwd=COMPOSE_DIR,
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return result.stdout.strip()


def _worker_accepted(job_id: str) -> bool:
    script = """
import asyncio
import sys
from coire_api.file_worker_client import FileWorkerClient, FileWorkerMissing
from coire_core.settings import get_settings

async def main():
    try:
        async with FileWorkerClient(get_settings()) as client:
            status = await client.status(sys.argv[1])
        print(status.state)
    except FileWorkerMissing:
        print('missing')

asyncio.run(main())
"""
    result = subprocess.run(
        [*COMPOSE, "exec", "-T", "coire-scheduler", "/app/.venv/bin/python3", "-c", script, job_id],
        cwd=COMPOSE_DIR,
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return result.stdout.strip() == "running"


def test_inflight_pdf_parser_crash_recovers(
    api_url: str,
    admin_headers: dict[str, str],
    access_token_factory: Callable[..., str],
) -> None:
    email = f"parser-crash-{uuid.uuid4().hex[:12]}@integration.test"
    noisy = Image.effect_noise((1024, 1024), 80).convert("RGB")
    pages = [noisy.copy() for _ in range(10)]
    source = BytesIO()
    pages[0].save(source, format="PDF", save_all=True, append_images=pages[1:], quality=25)
    assert len(source.getvalue()) < 10 * 1024 * 1024
    with httpx.Client(base_url=api_url, timeout=45) as client:
        user = client.post(
            "/api/v1/admin/users",
            headers=admin_headers,
            json={"email": email, "display_name": "Parser Crash", "role": "user"},
        )
        assert user.status_code == 201, user.text
        client.headers.update(
            {
                "cf-access-jwt-assertion": access_token_factory(email=email),
                "Origin": os.environ["COIRE_CHAT_PUBLIC_ORIGIN"],
            }
        )
        created = client.post("/api/v1/chat/conversations", json={"mode": "chat"})
        assert created.status_code == 201, created.text
        conversation_id = created.json()["id"]
        path = f"/api/v1/chat/conversations/{conversation_id}"
        worker_killed = False
        try:
            uploaded = client.post(
                f"{path}/files",
                data={
                    "filename": "many-pages.pdf",
                    "expected_revision": str(created.json()["revision"]),
                },
                files={"file": ("many-pages.pdf", source.getvalue(), "application/pdf")},
            )
            assert uploaded.status_code == 202, uploaded.text
            file_id = uploaded.json()["id"]
            file_path = f"{path}/files/{file_id}"
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline:
                attachment = client.get(file_path).json()
                if attachment["state"] in {"ready", "failed"}:
                    break
                time.sleep(0.25)
            assert attachment["state"] == "ready", attachment.get("safe_error")
            assert attachment["page_count"] == 10
            detail = client.get(path).json()
            render = client.post(
                f"{file_path}/process",
                json={
                    "request_id": str(uuid.uuid4()),
                    "expected_revision": detail["conversation"]["revision"],
                    "operation": "render",
                    "selected_pages": list(range(1, 11)),
                },
            )
            assert render.status_code == 202, render.text
            deadline = time.monotonic() + 20
            accepted_job: str | None = None
            while time.monotonic() < deadline:
                job_id = _sql(
                    "SELECT id FROM chat_file_processing "
                    f"WHERE attachment_id='{file_id}' AND operation='render' "
                    "AND state='running' ORDER BY created_at DESC LIMIT 1"
                )
                if job_id and _worker_accepted(job_id):
                    accepted_job = job_id
                    break
                time.sleep(0.05)
            assert accepted_job is not None, "worker never accepted the PDF render"
            subprocess.run(
                [*COMPOSE, "kill", "-s", "SIGKILL", "coire-file-worker"],
                cwd=COMPOSE_DIR,
                check=True,
                capture_output=True,
                timeout=15,
            )
            worker_killed = True
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline:
                attachment = client.get(file_path).json()
                if attachment["state"] == "failed":
                    break
                time.sleep(0.5)
            assert attachment["state"] == "failed"
            assert attachment["safe_error"] == "worker_unavailable"
            assert not attachment["previews"]
            subprocess.run(
                [*COMPOSE, "start", "coire-file-worker"],
                cwd=COMPOSE_DIR,
                check=True,
                capture_output=True,
                timeout=30,
            )
            worker_killed = False
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline:
                manifest = _sql(
                    "SELECT COALESCE(output_manifest->>'output_purged','false') "
                    f"FROM chat_file_processing WHERE id='{accepted_job}'"
                )
                if manifest == "true":
                    break
                time.sleep(0.5)
            assert manifest == "true", "failed parser output was not purged"
        finally:
            if worker_killed:
                subprocess.run(
                    [*COMPOSE, "start", "coire-file-worker"],
                    cwd=COMPOSE_DIR,
                    check=True,
                    capture_output=True,
                    timeout=30,
                )
            detail = client.get(path)
            if detail.status_code == 200:
                deleted = client.request(
                    "DELETE",
                    path,
                    json={"expected_revision": detail.json()["conversation"]["revision"]},
                )
                assert deleted.status_code == 202, deleted.text
