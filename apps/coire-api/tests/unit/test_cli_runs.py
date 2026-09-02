from __future__ import annotations

import uuid
from unittest.mock import Mock

from coire_api.cli import main


def response(payload: object) -> Mock:
    result = Mock()
    result.is_error = False
    result.json.return_value = payload
    return result


def test_run_submit_sends_registry_ids_and_no_runtime_controls(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    model_id = uuid.uuid4()
    post = Mock(return_value=response({"id": str(uuid.uuid4()), "state": "queued"}))
    monkeypatch.setattr("coire_api.cli.httpx.post", post)
    assert (
        main(
            [
                "--token",
                "token",
                "run",
                "submit",
                "--profile",
                "general",
                "--model",
                str(model_id),
                "--workspace",
                "workspace-1",
            ]
        )
        == 0
    )
    body = post.call_args.kwargs["json"]
    assert body["primary_model_id"] == str(model_id)
    assert body["permitted_model_ids"] == [str(model_id)]
    assert "image" not in body and "argv" not in body
    assert "queued" in capsys.readouterr().out


def test_run_list_and_kill_use_owner_and_admin_routes(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    run_id = uuid.uuid4()
    get = Mock(return_value=response([]))
    delete = Mock(return_value=response({"id": str(run_id), "state": "kill_requested"}))
    monkeypatch.setattr("coire_api.cli.httpx.get", get)
    monkeypatch.setattr("coire_api.cli.httpx.request", delete)
    assert main(["--token", "token", "run", "list"]) == 0
    assert main(["--token", "token", "run", "kill", str(run_id)]) == 0
    assert get.call_args.args[0].endswith("/api/v1/runs")
    assert delete.call_args.args[0] == "DELETE"
    assert delete.call_args.args[1].endswith(f"/api/v1/admin/runs/{run_id}")
