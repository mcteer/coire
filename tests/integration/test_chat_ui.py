"""Opt-in native Chat smoke against an admin-acquired tiny Studio model.

The operator prepares a published, ready model and enables Chat through the normal
control-plane path. This test never acquires weights or starts an engine directly.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import uuid
from collections.abc import Iterator
from io import BytesIO
from typing import cast

import httpx
import pytest
from PIL import Image

pytestmark = pytest.mark.integration


def _configuration() -> tuple[str, str, str]:
    url = os.environ.get("COIRE_TEST_CHAT_URL")
    model_id = os.environ.get("COIRE_TEST_MODEL")
    service = os.environ.get("COIRE_TEST_CHAT_KEYCHAIN_SERVICE")
    if not (url and model_id and service):
        pytest.skip("configure live Chat URL, acquired model ID and Keychain service")
    return url, model_id, service


def _events(response: httpx.Response) -> Iterator[dict[str, object]]:
    for line in response.iter_lines():
        if line.startswith("data: "):
            yield json.loads(line[6:])


def test_tiny_text_stream_usage_and_healthy_stop() -> None:
    url, model_id, service = _configuration()
    origin = os.environ.get("COIRE_TEST_CHAT_ORIGIN", "http://localhost:8180")
    key = subprocess.check_output(
        ["security", "find-generic-password", "-w", "-s", service], text=True
    ).strip()
    assert key
    with httpx.Client(
        base_url=url, timeout=90, headers={"Authorization": f"Bearer {key}"}
    ) as client:
        picker = client.get("/api/v1/chat/models")
        assert picker.status_code == 200
        assert model_id in {row["id"] for row in picker.json()["data"]}
        for stop_after_delta in (False, True):
            created = client.post(
                "/api/v1/chat/conversations",
                json={"mode": "chat", "model_id": model_id},
                headers={"Origin": origin},
            )
            assert created.status_code == 201
            conversation_id = created.json()["id"]
            request_id = str(uuid.uuid4())
            turn_body = {
                "client_request_id": request_id,
                "expected_revision": created.json()["revision"],
                "model_id": model_id,
                "content": "Reply briefly with the word ready.",
            }
            first_status_at: float | None = None
            start = time.monotonic()
            accepted: dict[str, object] | None = None
            terminal: dict[str, object] | None = None
            deltas = 0
            try:
                with client.stream(
                    "POST",
                    f"/api/v1/chat/conversations/{conversation_id}/turns",
                    json=turn_body,
                    headers={"Origin": origin},
                ) as response:
                    assert response.status_code == 200
                    for event in _events(response):
                        payload = event["payload"]
                        assert isinstance(payload, dict)
                        if payload["type"] == "turn.accepted":
                            accepted = payload["turn"]
                            assert isinstance(accepted, dict)
                            assert accepted["model_id"] == model_id
                        elif payload["type"] == "turn.status" and first_status_at is None:
                            first_status_at = time.monotonic() - start
                        elif payload["type"] == "message.delta":
                            deltas += 1
                            if stop_after_delta and deltas == 1:
                                assert accepted is not None
                                conflicting = client.post(
                                    f"/api/v1/chat/conversations/{conversation_id}/turns",
                                    json={**turn_body, "client_request_id": str(uuid.uuid4())},
                                    headers={"Origin": origin},
                                )
                                assert conflicting.status_code == 409
                                stopped = client.post(
                                    f"/api/v1/chat/conversations/{conversation_id}/turns/"
                                    f"{accepted['id']}/stop",
                                    json={"reason": "user_stop"},
                                    headers={"Origin": origin},
                                )
                                assert stopped.status_code == 200
                        elif payload["type"] == "turn.terminal":
                            terminal = payload
                            break
                assert accepted is not None and terminal is not None
                assert first_status_at is not None and first_status_at <= 1.0
                assert deltas > 0, (terminal["state"], terminal.get("safe_error"))
                if stop_after_delta:
                    assert terminal["state"] == "stopped"
                else:
                    assert terminal["state"] == "completed"
                    usage = terminal["usage"]
                    assert isinstance(usage, dict)
                    assert usage["prompt_tokens"] > 0
                    assert usage["completion_tokens"] > 0
                with client.stream(
                    "POST",
                    f"/api/v1/chat/conversations/{conversation_id}/turns",
                    json=turn_body,
                    headers={"Origin": origin},
                ) as replay:
                    assert replay.status_code == 200
                    saved = [cast(dict[str, object], event["payload"]) for event in _events(replay)]
                replayed_turn = saved[0]["turn"]
                assert isinstance(replayed_turn, dict)
                assert replayed_turn["id"] == accepted["id"]
                assert saved[-1]["state"] == terminal["state"]
            finally:
                detail = client.get(f"/api/v1/chat/conversations/{conversation_id}")
                if detail.status_code == 200:
                    deleted = client.request(
                        "DELETE",
                        f"/api/v1/chat/conversations/{conversation_id}",
                        json={"expected_revision": detail.json()["conversation"]["revision"]},
                        headers={"Origin": origin},
                    )
                    assert deleted.status_code == 202
                    assert (
                        client.get(f"/api/v1/chat/conversations/{conversation_id}").status_code
                        == 404
                    )
                    if accepted is not None:
                        assert (
                            client.get(
                                f"/api/v1/chat/conversations/{conversation_id}/turns/"
                                f"{accepted['id']}"
                            ).status_code
                            == 404
                        )


def test_private_image_processing_and_download() -> None:
    url, model_id, service = _configuration()
    origin = os.environ.get("COIRE_TEST_CHAT_ORIGIN", "http://localhost:8180")
    key = subprocess.check_output(
        ["security", "find-generic-password", "-w", "-s", service], text=True
    ).strip()
    fixture = BytesIO()
    Image.new("RGB", (16, 16), color=(255, 0, 0)).save(fixture, format="PNG")
    original = fixture.getvalue()
    with httpx.Client(
        base_url=url, timeout=30, headers={"Authorization": f"Bearer {key}"}
    ) as client:
        created = client.post(
            "/api/v1/chat/conversations",
            json={"mode": "chat", "model_id": model_id},
            headers={"Origin": origin},
        )
        assert created.status_code == 201
        conversation_id = created.json()["id"]
        file_id: str | None = None
        preview_id: str | None = None
        try:
            uploaded = client.post(
                f"/api/v1/chat/conversations/{conversation_id}/files",
                data={"filename": "tiny.png", "expected_revision": str(created.json()["revision"])},
                files={"file": ("tiny.png", original, "image/png")},
                headers={"Origin": origin},
            )
            assert uploaded.status_code == 202
            file_id = uploaded.json()["id"]
            file_url = f"/api/v1/chat/conversations/{conversation_id}/files/{file_id}"
            assert client.get(f"{file_url}/content").content == original
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline:
                file_response = client.get(file_url)
                assert file_response.status_code == 200
                attachment = file_response.json()
                if attachment["state"] in {"ready", "failed"}:
                    break
                time.sleep(0.5)
            assert attachment["state"] == "ready", attachment.get("safe_error")
            assert attachment["detected_type"] == "image/png"
            assert len(attachment["previews"]) == 1
            preview_id = attachment["previews"][0]["id"]
            preview = client.get(f"{file_url}/previews/{preview_id}")
            assert preview.status_code == 200
            assert preview.content.startswith(b"\x89PNG\r\n\x1a\n")
        finally:
            detail = client.get(f"/api/v1/chat/conversations/{conversation_id}")
            if detail.status_code == 200:
                deleted = client.request(
                    "DELETE",
                    f"/api/v1/chat/conversations/{conversation_id}",
                    json={"expected_revision": detail.json()["conversation"]["revision"]},
                    headers={"Origin": origin},
                )
                assert deleted.status_code == 202
                if file_id is not None:
                    file_url = f"/api/v1/chat/conversations/{conversation_id}/files/{file_id}"
                    assert client.get(f"{file_url}/content").status_code == 404
                    if preview_id is not None:
                        assert client.get(f"{file_url}/previews/{preview_id}").status_code == 404
