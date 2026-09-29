"""Opt-in native Chat smoke against an admin-acquired tiny Studio model.

The operator prepares a published, ready model and enables Chat through the normal
control-plane path. This test never acquires weights or starts an engine directly.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import time
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from typing import Any, cast

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


def _event_payload(event: dict[str, object]) -> dict[str, object]:
    payload = event["payload"]
    assert isinstance(payload, dict)
    return cast(dict[str, object], payload)


class _RateLimitedClient(httpx.Client):
    """Honor the temporary key's minute window during multi-case live acceptance."""

    def send(self, request: httpx.Request, **kwargs: Any) -> httpx.Response:
        for _ in range(3):
            response = super().send(request, **kwargs)
            if response.status_code != 429:
                return response
            delay = min(65, max(1, int(response.headers.get("Retry-After", "1")) + 1))
            response.close()
            time.sleep(delay)
        raise AssertionError("live Chat API key remained rate limited")


def test_tiny_text_stream_usage_and_healthy_stop() -> None:
    url, model_id, service = _configuration()
    origin = os.environ.get("COIRE_TEST_CHAT_ORIGIN", "http://localhost:8180")
    key = subprocess.check_output(
        ["security", "find-generic-password", "-w", "-s", service], text=True
    ).strip()
    assert key
    with _RateLimitedClient(
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


def test_completed_turn_observer_replays_without_regeneration() -> None:
    """A second tab can resume saved deltas with a cursor after the writer exits."""
    url, model_id, service = _configuration()
    origin = os.environ.get("COIRE_TEST_CHAT_ORIGIN", "http://localhost:8180")
    key = subprocess.check_output(
        ["security", "find-generic-password", "-w", "-s", service], text=True
    ).strip()
    with _RateLimitedClient(
        base_url=url, timeout=90, headers={"Authorization": f"Bearer {key}"}
    ) as client:
        created = client.post(
            "/api/v1/chat/conversations",
            json={"mode": "chat", "model_id": model_id},
            headers={"Origin": origin},
        )
        assert created.status_code == 201
        conversation_id = created.json()["id"]
        path = f"/api/v1/chat/conversations/{conversation_id}"
        try:
            with client.stream(
                "POST",
                f"{path}/turns",
                json={
                    "client_request_id": str(uuid.uuid4()),
                    "expected_revision": created.json()["revision"],
                    "model_id": model_id,
                    "content": "Reply briefly with the word ready.",
                },
                headers={"Origin": origin},
            ) as response:
                assert response.status_code == 200
                writer_events = list(_events(response))
            writer_terminal = next(
                event for event in writer_events if _event_payload(event)["type"] == "turn.terminal"
            )
            assert _event_payload(writer_terminal)["state"] == "completed"
            assert any(_event_payload(event)["type"] == "message.delta" for event in writer_events)
            writer_cursor = writer_terminal["cursor"]
            assert isinstance(writer_cursor, int)
            with client.stream(
                "GET", f"{path}/events", headers={"Last-Event-ID": f"{conversation_id}:0"}
            ) as observer:
                assert observer.status_code == 200
                replayed: list[dict[str, object]] = []
                for event in _events(observer):
                    replayed.append(event)
                    if _event_payload(event)["type"] == "turn.terminal":
                        break
            assert replayed[-1]["cursor"] == writer_cursor
            assert any(_event_payload(event)["type"] == "message.delta" for event in replayed)
            first_cursor = replayed[0]["cursor"]
            assert isinstance(first_cursor, int)
            with client.stream(
                "GET",
                f"{path}/events",
                headers={"Last-Event-ID": f"{conversation_id}:{first_cursor}"},
            ) as resumed:
                assert resumed.status_code == 200
                resumed_events: list[dict[str, object]] = []
                for event in _events(resumed):
                    resumed_events.append(event)
                    if _event_payload(event)["type"] == "turn.terminal":
                        break
            assert resumed_events
            assert all(isinstance(event["cursor"], int) for event in resumed_events)
            assert all(cast(int, event["cursor"]) > first_cursor for event in resumed_events)
            assert resumed_events[-1]["cursor"] == writer_cursor
            detail = client.get(path)
            assert detail.status_code == 200
            assert len(detail.json()["turns"]) == 1
        finally:
            detail = client.get(path)
            if detail.status_code == 200:
                deleted = client.request(
                    "DELETE",
                    path,
                    json={"expected_revision": detail.json()["conversation"]["revision"]},
                    headers={"Origin": origin},
                )
                assert deleted.status_code == 202
                assert client.get(f"{path}/events").status_code == 404


def test_private_image_processing_and_download() -> None:
    url, model_id, service = _configuration()
    origin = os.environ.get("COIRE_TEST_CHAT_ORIGIN", "http://localhost:8180")
    key = subprocess.check_output(
        ["security", "find-generic-password", "-w", "-s", service], text=True
    ).strip()
    fixture = BytesIO()
    Image.new("RGB", (16, 16), color=(255, 0, 0)).save(fixture, format="PNG")
    original = fixture.getvalue()
    with _RateLimitedClient(
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
                time.sleep(1)
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


def test_concurrent_file_uploads_preserve_revision_and_private_storage() -> None:
    url, model_id, service = _configuration()
    origin = os.environ.get("COIRE_TEST_CHAT_ORIGIN", "http://localhost:8180")
    key = subprocess.check_output(
        ["security", "find-generic-password", "-w", "-s", service], text=True
    ).strip()
    headers = {"Authorization": f"Bearer {key}"}
    with _RateLimitedClient(base_url=url, timeout=30, headers=headers) as client:
        created = client.post(
            "/api/v1/chat/conversations",
            json={"mode": "chat", "model_id": model_id},
            headers={"Origin": origin},
        )
        assert created.status_code == 201
        conversation_id = created.json()["id"]
        revision = created.json()["revision"]
        path = f"/api/v1/chat/conversations/{conversation_id}/files"

        def upload(index: int) -> httpx.Response:
            with _RateLimitedClient(base_url=url, timeout=30, headers=headers) as parallel:
                return parallel.post(
                    path,
                    data={"filename": f"race-{index}.txt", "expected_revision": str(revision)},
                    files={
                        "file": (f"race-{index}.txt", f"private {index}".encode(), "text/plain")
                    },
                    headers={"Origin": origin},
                )

        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                responses = list(pool.map(upload, (1, 2)))
            assert sorted(response.status_code for response in responses) == [202, 409]
            accepted = next(
                response.json() for response in responses if response.status_code == 202
            )
            detail = client.get(f"/api/v1/chat/conversations/{conversation_id}")
            assert detail.status_code == 200
            assert detail.json()["conversation"]["revision"] == revision + 1
            assert [row["id"] for row in detail.json()["attachments"]] == [accepted["id"]]
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


def test_managed_vlm_compatible_image_usage() -> None:
    url, _, service = _configuration()
    vision_model = os.environ.get("COIRE_TEST_VISION_MODEL")
    if not vision_model:
        pytest.skip("configure a verified, managed tiny VLM instance")
    key = subprocess.check_output(
        ["security", "find-generic-password", "-w", "-s", service], text=True
    ).strip()
    fixture = BytesIO()
    Image.new("RGB", (16, 16), color=(255, 0, 0)).save(fixture, format="PNG", optimize=True)
    image = fixture.getvalue()
    with _RateLimitedClient(
        base_url=url, timeout=120, headers={"Authorization": f"Bearer {key}"}
    ) as client:
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": vision_model,
                "max_tokens": 8,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "What color is the image? Reply briefly."},
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": "data:image/png;base64,"
                                    + base64.b64encode(image).decode("ascii")
                                },
                            },
                        ],
                    }
                ],
            },
        )
        assert response.status_code == 200
        result = response.json()
        assert len(result["choices"]) == 1
        assert result["usage"]["prompt_tokens"] > 0
        assert result["usage"]["completion_tokens"] > 0


def test_native_visual_file_stream_and_stop() -> None:
    url, _, service = _configuration()
    vision_model = os.environ.get("COIRE_TEST_VISION_MODEL")
    if not vision_model:
        pytest.skip("configure a published, verified tiny VLM for native Chat")
    origin = os.environ.get("COIRE_TEST_CHAT_ORIGIN", "http://localhost:8180")
    key = subprocess.check_output(
        ["security", "find-generic-password", "-w", "-s", service], text=True
    ).strip()
    fixture = BytesIO()
    Image.new("RGB", (16, 16), color=(255, 0, 0)).save(fixture, format="PNG", optimize=True)
    with _RateLimitedClient(
        base_url=url, timeout=120, headers={"Authorization": f"Bearer {key}"}
    ) as client:
        picker = client.get("/api/v1/chat/models")
        assert picker.status_code == 200
        selected = next(row for row in picker.json()["data"] if row["id"] == vision_model)
        assert selected["accepts_images"] is True and selected["max_images"] >= 1
        created = client.post(
            "/api/v1/chat/conversations",
            json={"mode": "chat", "model_id": vision_model},
            headers={"Origin": origin},
        )
        assert created.status_code == 201
        conversation_id = created.json()["id"]
        try:
            uploaded = client.post(
                f"/api/v1/chat/conversations/{conversation_id}/files",
                data={"filename": "red.png", "expected_revision": str(created.json()["revision"])},
                files={"file": ("red.png", fixture.getvalue(), "image/png")},
                headers={"Origin": origin},
            )
            assert uploaded.status_code == 202
            file_id = uploaded.json()["id"]
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline:
                attachment = client.get(
                    f"/api/v1/chat/conversations/{conversation_id}/files/{file_id}"
                ).json()
                if attachment["state"] in {"ready", "failed"}:
                    break
                time.sleep(1)
            assert attachment["state"] == "ready", attachment.get("safe_error")
            assert len(attachment["previews"]) == 1
            for stop_at_running in (False, True):
                detail = client.get(f"/api/v1/chat/conversations/{conversation_id}")
                assert detail.status_code == 200
                turn_body = {
                    "client_request_id": str(uuid.uuid4()),
                    "expected_revision": detail.json()["conversation"]["revision"],
                    "model_id": vision_model,
                    "content": (
                        "Describe this image in detail."
                        if stop_at_running
                        else "What color is this image? Reply briefly."
                    ),
                    "attachments": (
                        []
                        if stop_at_running
                        else [{"file_id": file_id, "mode": "visual", "pages": []}]
                    ),
                }
                accepted: dict[str, object] | None = None
                terminal: dict[str, object] | None = None
                deltas = 0
                stop_sent = False
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
                            assert accepted["model_id"] == vision_model
                        elif payload["type"] == "turn.status":
                            if stop_at_running and payload["state"] == "running" and not stop_sent:
                                assert accepted is not None
                                stopped = client.post(
                                    f"/api/v1/chat/conversations/{conversation_id}/turns/"
                                    f"{accepted['id']}/stop",
                                    json={"reason": "user_stop"},
                                    headers={"Origin": origin},
                                )
                                assert stopped.status_code == 200
                                stop_sent = True
                        elif payload["type"] == "message.delta":
                            deltas += 1
                        elif payload["type"] == "turn.terminal":
                            terminal = payload
                            break
                assert accepted is not None and terminal is not None
                if stop_at_running:
                    assert stop_sent and terminal["state"] == "stopped"
                else:
                    assert deltas > 0 and terminal["state"] == "completed"
                    usage = terminal["usage"]
                    assert isinstance(usage, dict)
                    assert usage["prompt_tokens"] > 0 and usage["completion_tokens"] > 0
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


def test_native_scanned_pdf_page_stream() -> None:
    url, _, service = _configuration()
    vision_model = os.environ.get("COIRE_TEST_VISION_MODEL")
    if not vision_model:
        pytest.skip("configure a published, verified tiny VLM for native Chat")
    origin = os.environ.get("COIRE_TEST_CHAT_ORIGIN", "http://localhost:8180")
    key = subprocess.check_output(
        ["security", "find-generic-password", "-w", "-s", service], text=True
    ).strip()
    fixture = BytesIO()
    Image.new("RGB", (8, 8), color=(255, 0, 0)).save(fixture, format="PDF", resolution=72.0)
    with _RateLimitedClient(
        base_url=url, timeout=120, headers={"Authorization": f"Bearer {key}"}
    ) as client:
        created = client.post(
            "/api/v1/chat/conversations",
            json={"mode": "chat", "model_id": vision_model},
            headers={"Origin": origin},
        )
        assert created.status_code == 201
        conversation_id = created.json()["id"]
        try:
            uploaded = client.post(
                f"/api/v1/chat/conversations/{conversation_id}/files",
                data={"filename": "scan.pdf", "expected_revision": str(created.json()["revision"])},
                files={"file": ("scan.pdf", fixture.getvalue(), "application/pdf")},
                headers={"Origin": origin},
            )
            assert uploaded.status_code == 202
            file_id = uploaded.json()["id"]
            file_url = f"/api/v1/chat/conversations/{conversation_id}/files/{file_id}"
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline:
                attachment = client.get(file_url).json()
                if attachment["state"] in {"ready", "failed"}:
                    break
                time.sleep(1)
            assert attachment["state"] == "ready", attachment.get("safe_error")
            assert attachment["detected_type"] == "application/pdf"
            assert attachment["page_count"] == 1
            detail = client.get(f"/api/v1/chat/conversations/{conversation_id}").json()
            rendering = client.post(
                f"{file_url}/process",
                json={
                    "request_id": str(uuid.uuid4()),
                    "expected_revision": detail["conversation"]["revision"],
                    "operation": "render",
                    "selected_pages": [1],
                },
                headers={"Origin": origin},
            )
            assert rendering.status_code == 202
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline:
                attachment = client.get(file_url).json()
                if attachment["state"] == "failed" or (
                    attachment["state"] == "ready" and attachment["previews"]
                ):
                    break
                time.sleep(1)
            assert attachment["state"] == "ready", attachment.get("safe_error")
            assert [asset["page"] for asset in attachment["previews"]] == [1]
            detail = client.get(f"/api/v1/chat/conversations/{conversation_id}").json()
            with client.stream(
                "POST",
                f"/api/v1/chat/conversations/{conversation_id}/turns",
                json={
                    "client_request_id": str(uuid.uuid4()),
                    "expected_revision": detail["conversation"]["revision"],
                    "model_id": vision_model,
                    "content": "What color is on the scanned page? Reply briefly.",
                    "attachments": [{"file_id": file_id, "mode": "visual", "pages": [1]}],
                },
                headers={"Origin": origin},
            ) as response:
                assert response.status_code == 200
                payloads = [
                    cast(dict[str, object], event["payload"]) for event in _events(response)
                ]
            assert any(payload["type"] == "message.delta" for payload in payloads)
            terminal = next(payload for payload in payloads if payload["type"] == "turn.terminal")
            assert terminal["state"] == "completed"
            usage = terminal["usage"]
            assert isinstance(usage, dict) and usage["prompt_tokens"] > 0
        finally:
            detail_response = client.get(f"/api/v1/chat/conversations/{conversation_id}")
            if detail_response.status_code == 200:
                deleted = client.request(
                    "DELETE",
                    f"/api/v1/chat/conversations/{conversation_id}",
                    json={"expected_revision": detail_response.json()["conversation"]["revision"]},
                    headers={"Origin": origin},
                )
                assert deleted.status_code == 202
