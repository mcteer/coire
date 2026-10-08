"""The CLI submits durable work and never executes suite generations locally."""

import argparse
import json
import uuid

import httpx
import pytest

from coire_api import cli

RUN = "01J00000000000000000000F01"
GROUP = "01J00000000000000000000F02"
MODEL = uuid.UUID(int=17)
VARIANT = uuid.UUID(int=18)
PREFIX = ["--api-url", "http://api.invalid", "--token", "private-token", "eval"]


@pytest.mark.parametrize("kind", ["task", "judge"])
def test_suite_submission_is_typed_durable_and_no_wait(
    monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == "POST" and request.url.path == "/api/v1/admin/evaluations"
        assert request.headers["Idempotency-Key"] == "evaluation-key"
        body = json.loads(request.content)
        assert body["suite_id"] == "registered" and body["suite_version"] == 2
        assert body["subjects"] == [
            {"model_id": str(MODEL), "variant_id": str(VARIANT), "adapter_id": None}
        ]
        return httpx.Response(
            202,
            json={
                "id": RUN,
                "group_id": GROUP,
                "state": "queued",
                "version": 1,
                "events_path": f"/api/v1/admin/evaluations/{RUN}/events",
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        monkeypatch.setattr("coire_api.cli.httpx.request", client.request)
        assert not hasattr(cli, "_run_suite")
        assert (
            cli.main(
                [
                    *PREFIX,
                    kind,
                    "--suite",
                    "registered",
                    "--suite-version",
                    "2",
                    "--model",
                    str(MODEL),
                    "--variant",
                    str(VARIANT),
                    "--idempotency-key",
                    "evaluation-key",
                    "--no-wait",
                ]
            )
            == 0
        )
    assert len(requests) == 1


def test_legacy_harness_preserves_positional_variant_and_version_assertion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET":
            assert request.url.path.endswith(f"/harness-evaluations/target/{VARIANT}")
            return httpx.Response(
                200,
                json={"model_id": str(MODEL), "variant_id": str(VARIANT), "capability_profile": {}},
            )
        assert request.url.path == "/api/v1/admin/evaluations"
        body = json.loads(request.content)
        assert body["expected_engine_version"] == "measured-version"
        assert body["suite_id"] == "harness-capability"
        return httpx.Response(
            202,
            json={
                "id": RUN,
                "group_id": GROUP,
                "state": "queued",
                "version": 1,
                "events_path": f"/api/v1/admin/evaluations/{RUN}/events",
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        monkeypatch.setattr("coire_api.cli.httpx.request", client.request)
        assert (
            cli.main(
                [
                    *PREFIX,
                    "harness",
                    str(VARIANT),
                    "--engine-version",
                    "measured-version",
                    "--idempotency-key",
                    "legacy",
                    "--no-wait",
                ]
            )
            == 0
        )
    assert len(requests) == 2


@pytest.mark.parametrize("verdict, expected", [("passed", 0), ("failed", 1), (None, 0)])
def test_wait_uses_measured_outcome_and_only_harness_gate_changes_exit(
    monkeypatch: pytest.MonkeyPatch,
    verdict: str | None,
    expected: int,
) -> None:
    from types import SimpleNamespace

    from coire_api.evaluation.cli import wait
    from coire_core.models.evaluation import EvaluationState

    responses = [
        SimpleNamespace(
            id=RUN, state=EvaluationState.RUNNING, phase="harness", cleanup_state="pending"
        ),
        SimpleNamespace(
            id=RUN, state=EvaluationState.SUCCEEDED, result=SimpleNamespace(harness_verdict=verdict)
        ),
    ]
    monkeypatch.setattr(cli, "_api", lambda *_: responses.pop(0))
    monkeypatch.setattr(cli, "_print_model", lambda _: None)
    monkeypatch.setattr("coire_api.evaluation.cli.time.sleep", lambda _: None)
    assert wait(argparse.Namespace(wait_timeout=30), {}, RUN) == expected


@pytest.mark.parametrize("verb, path", [("cancel", "cancel"), ("rerun", "rerun")])
def test_control_replay_preserves_explicit_version_and_key(
    monkeypatch: pytest.MonkeyPatch, verb: str, path: str
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == f"/api/v1/admin/evaluations/{RUN}/{path}"
        assert request.headers["Idempotency-Key"] == "retry-key"
        assert json.loads(request.content) == {"expected_version": 7}
        return httpx.Response(
            202,
            json={
                "id": RUN,
                "group_id": GROUP,
                "state": "queued",
                "version": 8,
                "events_path": f"/api/v1/admin/evaluations/{RUN}/events",
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        monkeypatch.setattr("coire_api.cli.httpx.request", client.request)
        for _ in range(2):
            assert (
                cli.main(
                    [
                        *PREFIX,
                        verb,
                        RUN,
                        "--expected-version",
                        "7",
                        "--idempotency-key",
                        "retry-key",
                    ]
                )
                == 0
            )
