"""Feedback commands use typed files and the authenticated private API."""

import json
from pathlib import Path

import httpx
import pytest

from coire_api import cli

PREFIX = ["--api-url", "https://core.test", "--token", "private-token"]
EXPORT = "01K6K4K0000000000000000000"


def test_export_cli_submits_typed_request_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    body = {
        "name": "private pairs",
        "license_note": "local consent",
        "model_id": "11111111-1111-4111-8111-111111111111",
        "variant_id": "22222222-2222-4222-8222-222222222222",
    }
    path = tmp_path / "request.json"
    path.write_text(json.dumps(body))

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST" and request.url.path == "/api/v1/admin/feedback/exports"
        assert request.headers["authorization"] == "Bearer private-token"
        assert request.headers["idempotency-key"] == "command"
        assert json.loads(request.content)["source"] == "owner_preferred"
        return httpx.Response(202, json={"id": EXPORT, "state": "queued", "version": 1})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        monkeypatch.setattr("coire_api.cli.httpx.request", client.request)
        assert (
            cli.main([*PREFIX, "feedback", "export", str(path), "--idempotency-key", "command"])
            == 0
        )
    assert EXPORT in capsys.readouterr().out


def test_export_cli_rejects_extra_fields_before_http(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "invalid.json"
    path.write_text('{"prompt":"private"}')

    def forbidden(*args: object, **kwargs: object) -> httpx.Response:
        raise AssertionError("Invalid request reached HTTP")

    monkeypatch.setattr("coire_api.cli.httpx.request", forbidden)
    assert cli.main([*PREFIX, "feedback", "export", str(path)]) == 1


def test_export_cli_lists_bounded_history(monkeypatch: pytest.MonkeyPatch) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET" and request.url.params["limit"] == "10"
        return httpx.Response(200, json={"items": [], "next_cursor": None})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        monkeypatch.setattr("coire_api.cli.httpx.request", client.request)
        assert cli.main([*PREFIX, "feedback", "exports", "--limit", "10"]) == 0
