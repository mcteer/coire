"""Concurrent owner file quota admission against disposable PostgreSQL."""

from __future__ import annotations

import os
import subprocess
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("COIRE_INTEGRATION") != "1"
        or os.environ.get("COIRE_CHAT_ENABLED") != "true"
        or os.environ.get("COIRE_CHAT_OWNER_QUOTA_BYTES") != "35651584"
        or not os.environ.get("COIRE_CHAT_PUBLIC_ORIGIN"),
        reason="run in the disposable Chat-enabled Compose project with the 34 MiB owner quota",
    ),
]


def _owner(
    client: httpx.Client,
    admin_headers: dict[str, str],
    access_token_factory: Callable[..., str],
    email: str,
) -> tuple[str, dict[str, str]]:
    created = client.post(
        "/api/v1/admin/users",
        headers=admin_headers,
        json={"email": email, "display_name": "Quota Test", "role": "user"},
    )
    assert created.status_code == 201, created.status_code
    return created.json()["id"], {
        "cf-access-jwt-assertion": access_token_factory(email=email),
        "Origin": os.environ["COIRE_CHAT_PUBLIC_ORIGIN"],
    }


def _sql(statement: str) -> str:
    result = subprocess.run(
        [
            "docker",
            "compose",
            "-p",
            "coire-it",
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
        cwd=Path(__file__).resolve().parents[2] / "deploy/compose",
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def test_concurrent_owner_uploads_respect_shared_byte_quota(
    api_url: str,
    admin_headers: dict[str, str],
    access_token_factory: Callable[..., str],
) -> None:
    with httpx.Client(base_url=api_url, timeout=30) as client:
        _, headers = _owner(client, admin_headers, access_token_factory, "quota-race@example.test")
        client.headers.update(headers)
        conversations: list[str] = []
        try:
            for _ in range(2):
                created = client.post("/api/v1/chat/conversations", json={"mode": "chat"})
                assert created.status_code == 201, created.status_code
                conversations.append(created.json()["id"])

            barrier = threading.Barrier(2)

            def upload(conversation_id: str) -> httpx.Response:
                with httpx.Client(base_url=api_url, headers=headers, timeout=30) as parallel:
                    barrier.wait(timeout=10)
                    return parallel.post(
                        f"/api/v1/chat/conversations/{conversation_id}/files",
                        data={"filename": "quota.txt", "expected_revision": "1"},
                        files={"file": ("quota.txt", b"x" * 1024**2, "text/plain")},
                    )

            with ThreadPoolExecutor(max_workers=2) as pool:
                responses = list(pool.map(upload, conversations))
            assert sorted(response.status_code for response in responses) == [202, 413], [
                (response.status_code, response.headers.get("content-type"), len(response.content))
                for response in responses
            ]
            assert (
                next(response.json() for response in responses if response.status_code == 413)[
                    "coire_code"
                ]
                == "chat_quota_exceeded"
            )
            for conversation_id, response in zip(conversations, responses, strict=True):
                detail = client.get(f"/api/v1/chat/conversations/{conversation_id}")
                assert detail.status_code == 200
                assert len(detail.json()["attachments"]) == (
                    1 if response.status_code == 202 else 0
                )
        finally:
            for conversation_id in conversations:
                detail = client.get(f"/api/v1/chat/conversations/{conversation_id}")
                if detail.status_code == 200:
                    deleted = client.request(
                        "DELETE",
                        f"/api/v1/chat/conversations/{conversation_id}",
                        json={"expected_revision": detail.json()["conversation"]["revision"]},
                    )
                    assert deleted.status_code == 202


def test_derived_retry_competes_with_new_upload_under_owner_lock(
    api_url: str,
    admin_headers: dict[str, str],
    access_token_factory: Callable[..., str],
) -> None:
    with httpx.Client(base_url=api_url, timeout=30) as client:
        owner_id, headers = _owner(
            client, admin_headers, access_token_factory, "quota-retry@example.test"
        )
        client.headers.update(headers)
        conversations: list[str] = []
        try:
            for _ in range(2):
                created = client.post("/api/v1/chat/conversations", json={"mode": "chat"})
                assert created.status_code == 201
                conversations.append(created.json()["id"])
            original, contender = conversations
            uploaded = client.post(
                f"/api/v1/chat/conversations/{original}/files",
                data={"filename": "original.txt", "expected_revision": "1"},
                files={"file": ("original.txt", b"a" * 1024**2, "text/plain")},
            )
            assert uploaded.status_code == 202, uploaded.status_code
            file_id = str(uuid.UUID(uploaded.json()["id"]))
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                file = client.get(f"/api/v1/chat/conversations/{original}/files/{file_id}")
                assert file.status_code == 200
                if file.json()["state"] == "failed":
                    break
                time.sleep(0.5)
            else:
                raise AssertionError("disposable stack did not record worker outage")
            # Simulate the scheduler's verified failed-output cleanup in this private
            # disposable database. The worker profile is intentionally absent.
            _sql(
                "UPDATE chat_file_processing SET "
                "output_manifest='{\"output_purged\":true}'::jsonb "
                f"WHERE attachment_id='{file_id}'; "
                "UPDATE chat_quota_reservations SET reserved_bytes=1048576 "
                f"WHERE attachment_id='{file_id}';"
            )
            barrier = threading.Barrier(2)

            def retry() -> httpx.Response:
                with httpx.Client(base_url=api_url, headers=headers, timeout=30) as parallel:
                    barrier.wait(timeout=10)
                    return parallel.post(
                        f"/api/v1/chat/conversations/{original}/files/{file_id}/process",
                        json={
                            "request_id": str(uuid.uuid4()),
                            "expected_revision": 2,
                            "operation": "inspect",
                        },
                    )

            def upload() -> httpx.Response:
                with httpx.Client(base_url=api_url, headers=headers, timeout=30) as parallel:
                    barrier.wait(timeout=10)
                    return parallel.post(
                        f"/api/v1/chat/conversations/{contender}/files",
                        data={"filename": "contender.txt", "expected_revision": "1"},
                        files={"file": ("contender.txt", b"b" * 1024**2, "text/plain")},
                    )

            with ThreadPoolExecutor(max_workers=2) as pool:
                retry_future = pool.submit(retry)
                upload_future = pool.submit(upload)
                responses = [retry_future.result(), upload_future.result()]
            assert sorted(response.status_code for response in responses) == [202, 413], [
                (response.status_code, response.headers.get("content-type"))
                for response in responses
            ]
            refused = next(response for response in responses if response.status_code == 413)
            assert refused.json()["coire_code"] == "chat_quota_exceeded"
            safe_owner_id = str(uuid.UUID(owner_id))
            total = int(
                _sql(
                    "SELECT coalesce(sum(reserved_bytes),0) FROM chat_quota_reservations "
                    f"WHERE owner_user_id='{safe_owner_id}' AND state='active';"
                )
            )
            assert total <= 34 * 1024**2
        finally:
            for conversation_id in conversations:
                detail = client.get(f"/api/v1/chat/conversations/{conversation_id}")
                if detail.status_code == 200:
                    deleted = client.request(
                        "DELETE",
                        f"/api/v1/chat/conversations/{conversation_id}",
                        json={"expected_revision": detail.json()["conversation"]["revision"]},
                    )
                    assert deleted.status_code == 202
