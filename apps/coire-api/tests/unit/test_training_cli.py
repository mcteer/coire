"""Installed CLI boundaries: authenticated HTTP, exact subjects and safe finite observers."""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from pathlib import Path
from string import Template
from typing import Any

import httpx
import pytest
import yaml

from coire_api import cli
from coire_api.training.specs import parse_spec
from coire_core.errors import TrainingValidationError
from coire_core.models.training import TrainingMeasurementRequest

MODEL = uuid.UUID("11111111-1111-4111-8111-111111111111")
VARIANT = uuid.UUID("22222222-2222-4222-8222-222222222222")
DATASET = uuid.UUID("33333333-3333-4333-8333-333333333333")
ADAPTER = uuid.UUID("44444444-4444-4444-8444-444444444444")
CHECKPOINT = uuid.UUID("55555555-5555-4555-8555-555555555555")
JOB = "01K6K4K0000000000000000000"
NOW = "2026-10-04T12:00:00Z"
DIGEST = "a" * 64
ROOT = "/api/v1/admin"
PREFIX = ["--api-url", "https://core.test", "--token", "private-token"]


def spec() -> dict[str, Any]:
    return {
        "model": {"model_id": str(MODEL), "variant_id": str(VARIANT)},
        "data": {
            "train": {
                "datasets": [
                    {"dataset_id": str(DATASET), "sample_count": 32, "mixture_proportion": 1.0}
                ],
                "epoch_samples": 32,
            },
            "validation": {"dataset_ids": [str(DATASET)]},
        },
        "parameterization": {"target_modules": ["self_attn.q_proj"]},
        "optim": {"updates": 100},
        "output": {"adapter_slug": "test-adapter"},
    }


def job(version: int = 7, state: str = "running") -> dict[str, Any]:
    return {
        "id": JOB,
        "version": version,
        "state": state,
        "source_yaml": "# private recipe\n",
        "source_sha256": DIGEST,
        "intent_sha256": DIGEST,
        "spec": spec(),
        "created_at": NOW,
        "updated_at": NOW,
    }


def dataset(version: int = 7, state: str = "analyzing") -> dict[str, Any]:
    return {
        "id": str(DATASET),
        "name": "test",
        "format": "conversation",
        "state": state,
        "provenance": {"source": "operator upload", "license_note": "owned"},
        "source_bytes": 42,
        "row_count": 2,
        "split_seed": 0,
        "version": version,
        "created_at": NOW,
    }


def adapter(version: int = 7) -> dict[str, Any]:
    return {
        "id": str(ADAPTER),
        "model_id": str(MODEL),
        "base_variant_id": str(VARIANT),
        "slug": "test-adapter",
        "selector": f"{MODEL}@test-adapter",
        "state": "ready",
        "manifest_sha256": DIGEST,
        "base_manifest_sha256": DIGEST,
        "source_job_id": JOB,
        "source_checkpoint_id": str(CHECKPOINT),
        "resolved_spec_sha256": DIGEST,
        "parameterization": "lora",
        "version": version,
        "created_at": NOW,
    }


def measurement() -> dict[str, Any]:
    return {
        "spec": spec(),
        "nodes": ["coire-edge-a"],
        "mode": "memory",
        "workload": {
            "sha256": DIGEST,
            "concurrency_per_target": 1,
            "arrival_interval_ms": 1000,
            "max_output_tokens": 16,
        },
    }


@pytest.fixture
def wire(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[list[httpx.Request], list[tuple[int, Any]]]]:
    calls: list[httpx.Request] = []
    replies: list[tuple[int, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.headers["Authorization"] == "Bearer private-token"
        assert request.url.host == "core.test"
        assert replies, f"unexpected HTTP call: {request.method} {request.url.path}"
        status, payload = replies.pop(0)
        return httpx.Response(status, json=payload)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        monkeypatch.setattr("coire_api.cli.httpx.request", client.request)
        monkeypatch.setattr("coire_api.cli.httpx.get", client.get)
        monkeypatch.setattr("coire_api.cli.httpx.post", client.post)
        yield calls, replies
    assert not replies


def body(request: httpx.Request) -> Any:
    return json.loads(request.content)


@pytest.mark.parametrize(
    ("argv", "path", "payload"),
    [
        (["data", "list"], f"{ROOT}/datasets", {"items": []}),
        (["data", "show", str(DATASET)], f"{ROOT}/datasets/{DATASET}", dataset()),
        (["train", "recipes"], f"{ROOT}/training/recipes", {"items": []}),
        (["train", "list"], f"{ROOT}/training/jobs", {"items": []}),
        (["train", "show", JOB], f"{ROOT}/training/jobs/{JOB}", job()),
        (["train", "checkpoints", JOB], f"{ROOT}/training/jobs/{JOB}/checkpoints", {"items": []}),
        (["train", "profiles"], f"{ROOT}/training/profiles", {"items": []}),
        (
            ["train", "measurement", str(DATASET)],
            f"{ROOT}/training/measurements/{DATASET}",
            {"id": str(DATASET), "request": measurement(), "state": "running", "created_at": NOW},
        ),
        (["adapter", "list"], f"{ROOT}/adapters", {"items": []}),
        (["adapter", "show", str(ADAPTER)], f"{ROOT}/adapters/{ADAPTER}", adapter()),
    ],
)
def test_reads_use_typed_admin_routes(wire: Any, argv: list[str], path: str, payload: Any) -> None:
    calls, replies = wire
    replies.append((200, payload))
    assert cli.main(PREFIX + argv) == 0
    assert len(calls) == 1 and calls[0].method == "GET" and calls[0].url.path == path
    assert "Idempotency-Key" not in calls[0].headers


@pytest.mark.parametrize("group", ["data", "train", "adapter"])
def test_pagination_is_explicit(wire: Any, group: str) -> None:
    calls, replies = wire
    replies.append((200, {"items": [], "next_cursor": "next"}))
    assert cli.main([*PREFIX, group, "list", "--limit", "100", "--cursor", "opaque"]) == 0
    assert dict(calls[0].url.params) == {"limit": "100", "cursor": "opaque"}


@pytest.mark.parametrize("verb", ["pause", "resume", "cancel", "delete"])
def test_job_controls_read_version_once(
    wire: Any, verb: str, capsys: pytest.CaptureFixture[str]
) -> None:
    calls, replies = wire
    replies.append((200, job()))
    replies.append(
        (
            202,
            {"job_id": JOB, "state": "retired"}
            if verb == "delete"
            else {"command_id": str(DATASET), "job_id": JOB, "state": "paused", "version": 8},
        )
    )
    assert cli.main([*PREFIX, "train", verb, JOB, "--idempotency-key", "original-command"]) == 0
    assert len(calls) == 2
    assert body(calls[1]) == {"expected_version": 7}
    assert calls[1].headers["Idempotency-Key"] == "original-command"
    assert calls[1].method == ("DELETE" if verb == "delete" else "POST")
    assert calls[1].url.path == f"{ROOT}/training/jobs/{JOB}" + (
        "" if verb == "delete" else f"/{verb}"
    )
    if verb == "delete":
        assert "lineage" in capsys.readouterr().err


@pytest.mark.parametrize("verb", ["publish", "unpublish", "retire"])
def test_adapter_controls_use_current_typed_binding(wire: Any, verb: str) -> None:
    calls, replies = wire
    replies.extend(
        [(200, adapter()), (202, {"adapter_id": str(ADAPTER), "state": "ready", "version": 8})]
    )
    assert cli.main([*PREFIX, "adapter", verb, str(ADAPTER)]) == 0
    expected: dict[str, Any] = {"expected_version": 7}
    if verb != "retire":
        expected["visibility"] = "published" if verb == "publish" else "admin_only"
    assert body(calls[1]) == expected
    assert calls[1].method == ("DELETE" if verb == "retire" else "PATCH")
    assert calls[1].url.path == f"{ROOT}/adapters/{ADAPTER}"
    uuid.UUID(calls[1].headers["Idempotency-Key"])


def test_dataset_delete_reads_version(wire: Any) -> None:
    calls, replies = wire
    replies.extend(
        [(200, dataset()), (202, {"dataset_id": str(DATASET), "state": "retired", "version": 8})]
    )
    assert cli.main([*PREFIX, "data", "delete", str(DATASET)]) == 0
    assert body(calls[1]) == {"expected_version": 7}
    assert calls[1].method == "DELETE"


def test_replay_pins_original_version_without_reading_advanced_state(wire: Any) -> None:
    calls, replies = wire
    for _ in range(2):
        replies.append(
            (202, {"command_id": str(DATASET), "job_id": JOB, "state": "paused", "version": 8})
        )
        assert (
            cli.main(
                [
                    *PREFIX,
                    "train",
                    "pause",
                    JOB,
                    "--idempotency-key",
                    "replay",
                    "--expected-version",
                    "7",
                ]
            )
            == 0
        )
    assert len(calls) == 2 and calls[0].content == calls[1].content
    assert all(
        call.method == "POST" and call.headers["Idempotency-Key"] == "replay" for call in calls
    )


@pytest.mark.parametrize("status", [401, 403, 404, 409, 413, 422, 429, 503, 500])
def test_http_refusals_never_retry_or_print_private_body(
    wire: Any, status: int, capsys: pytest.CaptureFixture[str]
) -> None:
    calls, replies = wire
    replies.append((status, {"detail": "private-token private-data /opt/private source_yaml"}))
    assert cli.main([*PREFIX, "train", "cancel", JOB, "--expected-version", "7"]) == 1
    assert len(calls) == 1
    output = capsys.readouterr()
    assert f"HTTP {status}" in output.err
    assert "private-token" not in output.err and "private-data" not in output.err
    assert "/opt/private" not in output.err and output.out == ""


@pytest.mark.parametrize("explicit", [True, False])
def test_checkpoint_promotion_binds_parent_job_version(wire: Any, explicit: bool) -> None:
    calls, replies = wire
    if not explicit:
        replies.append((200, job()))
    replies.append((202, {"adapter_id": str(ADAPTER), "state": "validating", "version": 1}))
    options = ["--expected-version", "7"] if explicit else ["--job", JOB]
    assert (
        cli.main([*PREFIX, "adapter", "promote", str(CHECKPOINT), "--name", "promoted", *options])
        == 0
    )
    assert body(calls[-1]) == {"expected_version": 7, "adapter_slug": "promoted"}
    assert calls[-1].url.path == f"{ROOT}/training/checkpoints/{CHECKPOINT}/promote"


@pytest.mark.parametrize("verb", ["validate", "submit"])
@pytest.mark.parametrize("objective", ["sft", "dpo", "orpo"])
def test_original_yaml_is_transported_verbatim(
    wire: Any, tmp_path: Path, verb: str, objective: str
) -> None:
    calls, replies = wire
    intent = spec()
    if objective != "sft":
        intent.update(
            schema_version=3,
            objective=objective,
            objective_options={"beta": 0.1} if objective == "dpo" else {"weight": 0.1},
            init_adapter=None,
        )
    source = "# original comment\r\n" + yaml.safe_dump(intent, sort_keys=False)
    path = tmp_path / "recipe.yaml"
    path.write_bytes(source.encode())
    payload = (
        {"spec": intent, "intent_sha256": DIGEST, "reasons": ["profile_missing"]}
        if verb == "validate"
        else {
            "job_id": JOB,
            "state": "queued",
            "version": 1,
            "events_path": f"{ROOT}/training/jobs/{JOB}/events",
        }
    )
    replies.append((202, payload))
    assert cli.main([*PREFIX, "train", verb, str(path), "--idempotency-key", "recipe"]) == (
        2 if verb == "validate" else 0
    )
    assert body(calls[0]) == {
        "source_yaml": source,
        "source_kind": "yaml",
        "form_spec": None,
        "preview_sha256": None,
    }


@pytest.mark.parametrize("content", [b"", b"x" * 65537, b"\xff"])
def test_invalid_recipe_size_and_encoding_fail_before_http(
    wire: Any, tmp_path: Path, content: bytes
) -> None:
    calls, _ = wire
    path = tmp_path / "recipe.yaml"
    path.write_bytes(content)
    assert cli.main([*PREFIX, "train", "submit", str(path)]) == 1
    assert not calls


def test_measurement_yaml_executes_typed_request(wire: Any, tmp_path: Path) -> None:
    calls, replies = wire
    path = tmp_path / "measurement.yaml"
    path.write_text(yaml.safe_dump(measurement()))
    replies.append((202, {"measurement_id": str(DATASET), "state": "queued"}))
    assert cli.main([*PREFIX, "train", "measure", str(path)]) == 0
    assert body(calls[0]) == TrainingMeasurementRequest.model_validate(measurement()).model_dump(
        mode="json"
    )
    assert calls[0].url.path == f"{ROOT}/training/measurements"


@pytest.mark.parametrize(
    "suffix",
    [
        "mode: memory\nmode: coexistence\n",
        "evil: !!python/object:private {}\n",
        "evil: &secret secret\nreplay: *secret\n",
        "unexpected: private-data\n",
    ],
)
def test_invalid_measurement_yaml_is_private_and_never_sent(
    wire: Any, tmp_path: Path, suffix: str, capsys: pytest.CaptureFixture[str]
) -> None:
    calls, _ = wire
    path = tmp_path / "measurement.yaml"
    path.write_text(yaml.safe_dump(measurement()) + suffix)
    assert cli.main([*PREFIX, "train", "measure", str(path)]) == 1
    assert not calls
    assert "private-data" not in capsys.readouterr().err


def test_upload_streams_multipart_and_no_caller_path(wire: Any, tmp_path: Path) -> None:
    calls, replies = wire
    path = tmp_path / "private-source.jsonl"
    path.write_text('{"text":"private training row"}\n')
    replies.append((202, {"dataset_id": str(DATASET), "state": "analyzing", "version": 1}))
    assert (
        cli.main(
            [
                *PREFIX,
                "data",
                "upload",
                str(path),
                "--name",
                "upload",
                "--format",
                "text",
                "--source",
                "operator",
                "--license-note",
                "owned",
                "--model",
                str(MODEL),
                "--variant",
                str(VARIANT),
                "--seed",
                "0",
            ]
        )
        == 0
    )
    call = calls[0]
    assert call.url.path == f"{ROOT}/datasets" and call.method == "POST"
    assert call.headers["Content-Type"].startswith("multipart/form-data;")
    assert int(call.headers["Content-Length"]) == len(call.content)
    assert b'filename="dataset.jsonl"' in call.content and str(path).encode() not in call.content
    assert b'"analysis_model_id":"' + str(MODEL).encode() in call.content
    assert b'"analysis_variant_id":"' + str(VARIANT).encode() in call.content
    assert b'"split_seed":0' in call.content and b"private training row" in call.content


def analysis(state: str = "running", **changes: Any) -> dict[str, Any]:
    result: dict[str, Any] = {
        "id": str(CHECKPOINT),
        "dataset_id": str(DATASET),
        "model_id": str(MODEL),
        "variant_id": str(VARIANT),
        "state": state,
        "created_at": NOW,
    }
    if state == "succeeded":
        result.update(
            tokenizer_sha256=DIGEST,
            template_sha256=DIGEST,
            runtime_sha256=DIGEST,
            tokens={
                "minimum": 2,
                "maximum": 8,
                "p50": 4,
                "p95": 8,
                "histogram": [2],
                "upper_bounds": [8],
            },
        )
    result.update(changes)
    return result


def analyze_args() -> list[str]:
    return [
        *PREFIX,
        "data",
        "analyze",
        str(DATASET),
        "--model",
        str(MODEL),
        "--variant",
        str(VARIANT),
        "--wait",
        "--poll-interval",
        "0.001",
    ]


@pytest.mark.parametrize("state", ["succeeded", "failed", "cancelled"])
def test_analysis_wait_uses_immutable_receipt_and_row_diagnostics(
    wire: Any, state: str, capsys: pytest.CaptureFixture[str]
) -> None:
    calls, replies = wire
    changes = (
        {
            "invalid_count": 1,
            "diagnostics": [{"row": 17, "field": "messages[0].content", "code": "overlength"}],
        }
        if state == "failed"
        else {}
    )
    replies.extend(
        [
            (202, {"analysis_id": str(CHECKPOINT), "state": "queued"}),
            (200, analysis()),
            (200, analysis(state, **changes)),
        ]
    )
    assert cli.main(analyze_args()) == (0 if state == "succeeded" else 1)
    assert body(calls[0]) == {"model_id": str(MODEL), "variant_id": str(VARIANT)}
    assert all(call.url.path == f"{ROOT}/dataset-analyses/{CHECKPOINT}" for call in calls[1:])
    if state == "failed":
        output = capsys.readouterr().out
        assert '"row": 17' in output and "overlength" in output


def test_analysis_refuses_changed_tokenizer_subject(wire: Any) -> None:
    calls, replies = wire
    replies.extend(
        [
            (202, {"analysis_id": str(CHECKPOINT), "state": "queued"}),
            (200, analysis(model_id=str(ADAPTER))),
        ]
    )
    assert cli.main(analyze_args()) == 1
    assert len(calls) == 2


def test_analysis_wait_is_finite_and_does_not_cancel(
    wire: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls, replies = wire
    clock = iter([0.0, 0.1, 1.0, 1.0])
    monkeypatch.setattr("coire_api.cli.time.monotonic", lambda: next(clock))
    monkeypatch.setattr("coire_api.cli.time.sleep", lambda _: None)
    replies.extend([(202, {"analysis_id": str(CHECKPOINT), "state": "queued"}), (200, analysis())])
    assert cli.main([*analyze_args(), "--wait-timeout", "1"]) == 2
    assert [call.method for call in calls] == ["POST", "GET"]
    assert "work continues" in capsys.readouterr().err


@pytest.mark.parametrize("value", ["nan", "inf", "0", "-1"])
def test_wait_requires_finite_positive_timeout(value: str) -> None:
    with pytest.raises(SystemExit) as error:
        cli.main([*analyze_args(), "--wait-timeout", value])
    assert error.value.code == 2


def test_transport_failure_has_no_url_token_or_content(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fail(*args: Any, **kwargs: Any) -> httpx.Response:
        raise httpx.ReadTimeout("private-token /private/source private-row")

    monkeypatch.setattr("coire_api.cli.httpx.request", fail)
    assert cli.main([*PREFIX, "train", "list"]) == 1
    output = capsys.readouterr().err
    assert "ReadTimeout" in output and "private-token" not in output and "/private" not in output


@pytest.mark.parametrize("kind", ["lora", "qlora", "dora"])
def test_seed_templates_require_real_bindings_and_then_parse(kind: str) -> None:
    root = Path(__file__).resolve().parents[4]
    source = (root / "recipes" / "training" / f"sft-{kind}.yaml").read_text()
    with pytest.raises(TrainingValidationError, match="Invalid training fields"):
        parse_spec(source)
    bound = Template(source).safe_substitute(
        model_id=str(MODEL),
        variant_id=str(VARIANT),
        dataset_id=str(DATASET),
        adapter_slug="bound-adapter",
    )
    parsed = parse_spec(bound)
    assert parsed.parameterization.kind == kind and parsed.model.variant_id == VARIANT
    assert parsed.data.train.datasets[0].dataset_id == DATASET


@pytest.mark.parametrize("exact_adapter", [True, False, None])
def test_evaluation_durable_submission_preserves_exact_subject(
    monkeypatch: pytest.MonkeyPatch, exact_adapter: bool | None
) -> None:
    target = {
        "model_id": str(MODEL),
        "variant_id": str(VARIANT),
        "adapter_id": str(ADAPTER) if exact_adapter else None,
        "base_manifest_sha256": DIGEST,
        "adapter_manifest_sha256": DIGEST if exact_adapter else None,
    }
    context: dict[str, Any] = {
        "variant_id": str(VARIANT),
        "model_id": str(MODEL),
        "capability_profile": {},
    }
    if exact_adapter is not None:
        context.update(
            target=target, public_selector=f"{MODEL}@test-adapter" if exact_adapter else str(MODEL)
        )
    generations: list[httpx.Request] = []
    submission: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer private-token"
        if request.method == "GET":
            assert request.url.path == f"{ROOT}/harness-evaluations/target/{VARIANT}"
            assert dict(request.url.params) == (
                {"adapter_id": str(ADAPTER)} if exact_adapter else {}
            )
            return httpx.Response(200, json=context)
        assert request.url.path == f"{ROOT}/evaluations"
        submitted = body(request)
        from coire_core.models.evaluation import EvaluationSubmission

        EvaluationSubmission.model_validate(submitted)
        assert submitted["subjects"] == [
            {
                "model_id": str(MODEL),
                "variant_id": str(VARIANT),
                "adapter_id": str(ADAPTER) if exact_adapter else None,
            }
        ]
        assert submitted["expected_engine_version"] == "pinned"
        submission.append(submitted)
        return httpx.Response(
            202,
            json={
                "id": JOB,
                "group_id": JOB,
                "state": "queued",
                "version": 1,
                "events_path": f"{ROOT}/evaluations/{JOB}/events",
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        monkeypatch.setattr("coire_api.cli.httpx.request", client.request)
        argv = [*PREFIX, "eval", "harness", str(VARIANT), "--engine-version", "pinned", "--no-wait"]
        if exact_adapter:
            argv += ["--adapter", str(ADAPTER)]
        assert cli.main(argv) == 0
    assert not generations and len(submission) == 1


def event(sequence: int, kind: str = "state", state: str = "running") -> dict[str, Any]:
    return {
        "id": sequence,
        "job_id": JOB,
        "state_version": 7,
        "occurred_at": NOW,
        "kind": kind,
        "payload": {"kind": kind, "state": state},
    }


@pytest.mark.parametrize("state", ["succeeded", "failed", "cancelled"])
def test_events_replay_cursor_dedup_and_terminal_exit(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], state: str
) -> None:
    events = [event(2), event(3), event(3), event(4, "terminal", state)]
    content = ": heartbeat\n\n" + "".join("data: " + json.dumps(value) + "\n\n" for value in events)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"{ROOT}/training/jobs/{JOB}/events"
        assert request.headers["Authorization"] == "Bearer private-token"
        assert (
            request.headers["Last-Event-ID"] == "2"
            and request.headers["Accept"] == "text/event-stream"
        )
        return httpx.Response(200, text=content)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        monkeypatch.setattr("coire_api.cli.httpx.stream", client.stream)
        assert cli.main([*PREFIX, "train", "events", JOB, "--last-event-id", "2"]) == (
            0 if state == "succeeded" else 1
        )
    output = capsys.readouterr().out
    assert output.count('"id": 3') == 1 and '"id": 2' not in output and '"id": 4' in output


def test_events_disconnect_reports_last_cursor_without_cancel(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, text="data: " + json.dumps(event(5)) + "\n\n")
        )
    ) as client:
        monkeypatch.setattr("coire_api.cli.httpx.stream", client.stream)
        assert cli.main([*PREFIX, "train", "events", JOB]) == 2
    assert "--last-event-id 5" in capsys.readouterr().err


def test_strict_response_never_echoes_unexpected_private_fields(
    wire: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    _, replies = wire
    replies.append((200, {"items": [], "source_content": "private-row"}))
    assert cli.main([*PREFIX, "data", "list"]) == 1
    output = capsys.readouterr()
    assert "private-row" not in output.err and output.out == ""


def test_missing_token_rejects_before_http(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("COIRE_API_TOKEN", raising=False)
    with pytest.raises(SystemExit) as error:
        cli.main(["data", "list"])
    assert error.value.code == 2


@pytest.mark.parametrize("state", ["ready", "failed", "analysis_failed"])
def test_upload_wait_checks_analysis_binding_and_failed_rows(
    wire: Any, tmp_path: Path, state: str, capsys: pytest.CaptureFixture[str]
) -> None:
    calls, replies = wire
    path = tmp_path / "rows.jsonl"
    path.write_text('{"text":"row one"}\n{"text":"row two"}\n')
    detail = dataset(state=state)
    detail["analysis_id"] = str(CHECKPOINT)
    if state != "ready":
        detail.update(
            invalid_count=1, diagnostics=[{"row": 2, "field": "text", "code": "zero_target"}]
        )
    replies.extend(
        [
            (202, {"dataset_id": str(DATASET), "state": "analyzing", "version": 1}),
            (200, detail),
        ]
    )
    if state == "ready":
        replies.append((200, analysis("succeeded")))
    assert cli.main(
        [
            *PREFIX,
            "data",
            "upload",
            str(path),
            "--name",
            "upload",
            "--format",
            "text",
            "--source",
            "operator",
            "--license-note",
            "owned",
            "--model",
            str(MODEL),
            "--variant",
            str(VARIANT),
            "--wait",
        ]
    ) == (0 if state == "ready" else 1)
    assert calls[1].url.path == f"{ROOT}/datasets/{DATASET}"
    if state != "ready":
        output = capsys.readouterr().out
        assert '"row": 2' in output and "zero_target" in output
    else:
        assert calls[2].url.path == f"{ROOT}/dataset-analyses/{CHECKPOINT}"


@pytest.mark.parametrize("state", ["failed", "inconclusive"])
def test_measurement_outcomes_have_failure_exit(wire: Any, state: str) -> None:
    _, replies = wire
    replies.append(
        (200, {"id": str(DATASET), "request": measurement(), "state": state, "created_at": NOW})
    )
    assert cli.main([*PREFIX, "train", "measurement", str(DATASET)]) == 1


@pytest.mark.parametrize("state", ["failed", "cancelled"])
def test_job_terminal_outcomes_have_failure_exit(wire: Any, state: str) -> None:
    _, replies = wire
    replies.append((200, job(state=state)))
    assert cli.main([*PREFIX, "train", "show", JOB]) == 1


def test_version_conflict_after_current_read_is_not_rebased(wire: Any) -> None:
    calls, replies = wire
    replies.extend([(200, job()), (409, {"detail": "advanced version"})])
    assert cli.main([*PREFIX, "train", "resume", JOB, "--idempotency-key", "resume-original"]) == 1
    assert len(calls) == 2 and body(calls[-1]) == {"expected_version": 7}


def test_poll_transport_timeout_preserves_background_work(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(202, json={"analysis_id": str(CHECKPOINT), "state": "queued"})
        raise httpx.ReadTimeout("private-row private-token", request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        monkeypatch.setattr("coire_api.cli.httpx.request", client.request)
        assert cli.main(analyze_args()) == 2
    output = capsys.readouterr().err
    assert "work continues" in output and "private-row" not in output


@pytest.mark.parametrize(
    "mode",
    [
        "idle_timeout",
        "wall_timeout",
        "wrong_subject",
        "invalid_content",
        "auth_revoked",
        "terminal_reset",
    ],
)
def test_event_observer_deadlines_privacy_and_reset(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], mode: str
) -> None:
    payload = event(3)
    if mode == "wrong_subject":
        payload["job_id"] = "01K6K4K0000000000000000001"
    if mode == "invalid_content":
        payload["source_yaml"] = "private-row"
    if mode == "terminal_reset":
        payload.update(
            kind="reset",
            payload={
                "kind": "reset",
                "snapshot": {
                    "id": JOB,
                    "version": 7,
                    "state": "succeeded",
                    "completed_update": 100,
                },
            },
        )

    def handler(request: httpx.Request) -> httpx.Response:
        if mode == "idle_timeout":
            raise httpx.ReadTimeout("private-token private-row", request=request)
        if mode == "auth_revoked":
            return httpx.Response(403, json={"detail": "private-token"})
        return httpx.Response(200, text="data: " + json.dumps(payload) + "\n\n")

    if mode == "wall_timeout":
        clock = iter([0.0, 0.5])
        monkeypatch.setattr("coire_api.cli.time.monotonic", lambda: next(clock))
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        monkeypatch.setattr("coire_api.cli.httpx.stream", client.stream)
        expected = 0 if mode == "terminal_reset" else 2 if mode.endswith("timeout") else 1
        assert cli.main([*PREFIX, "train", "events", JOB, "--timeout", "0.5"]) == expected
    output = capsys.readouterr()
    assert "private-row" not in output.out + output.err
    assert "private-token" not in output.out + output.err


@pytest.mark.parametrize(
    "argv",
    [
        ["train", "pause", JOB, "--expected-version", "0"],
        ["train", "pause", JOB, "--idempotency-key", "bad\nheader"],
        ["train", "pause", "../../private"],
        ["adapter", "promote", str(CHECKPOINT), "--name", "test"],
        ["data", "show", "/opt/private"],
    ],
)
def test_invalid_cli_bindings_reject_before_http(wire: Any, argv: list[str]) -> None:
    calls, _ = wire
    with pytest.raises(SystemExit) as error:
        cli.main(PREFIX + argv)
    assert error.value.code == 2 and not calls


def test_missing_recipe_is_actionable_and_private(
    wire: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    calls, _ = wire
    assert cli.main([*PREFIX, "train", "submit", "/private/missing-recipe.yaml"]) == 1
    assert not calls
    output = capsys.readouterr().err
    assert "Cannot read input file" in output and "/private" not in output


def test_requested_adapter_cannot_use_legacy_context(wire: Any) -> None:
    calls, replies = wire
    replies.append(
        (200, {"variant_id": str(VARIANT), "model_id": str(MODEL), "capability_profile": {}})
    )
    assert cli.main([*PREFIX, "eval", "harness", str(VARIANT), "--adapter", str(ADAPTER)]) == 2
    assert len(calls) == 1


def test_evaluation_transport_diagnostics_do_not_include_exception_content(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail(*args: object, **kwargs: object) -> httpx.Response:
        raise httpx.ReadTimeout("private-token private-row /private/path")

    monkeypatch.setattr("coire_api.cli.httpx.request", fail)
    assert cli.main([*PREFIX, "eval", "harness", str(VARIANT)]) == 2
    output = capsys.readouterr().err
    assert "ReadTimeout" in output
    assert (
        "private-token" not in output
        and "private-row" not in output
        and "/private/path" not in output
    )


@pytest.mark.parametrize(
    "reason",
    [
        "invalid_input",
        "impossible_fit",
        "unauthorized",
        "analysis_pending",
        "profile_missing",
        "capacity_busy",
    ],
)
def test_validation_refusal_and_pending_are_distinct(
    wire: Any, tmp_path: Path, reason: str
) -> None:
    _, replies = wire
    path = tmp_path / "recipe.yaml"
    path.write_text(yaml.safe_dump(spec()))
    replies.append((200, {"spec": spec(), "intent_sha256": DIGEST, "reasons": [reason]}))
    expected = 1 if reason in ("invalid_input", "impossible_fit", "unauthorized") else 2
    assert cli.main([*PREFIX, "train", "validate", str(path)]) == expected


def test_adapter_context_without_public_pair_is_rejected(wire: Any) -> None:
    calls, replies = wire
    replies.append(
        (
            200,
            {
                "variant_id": str(VARIANT),
                "model_id": str(MODEL),
                "capability_profile": {},
                "target": {
                    "model_id": str(MODEL),
                    "variant_id": str(VARIANT),
                    "adapter_id": str(ADAPTER),
                    "base_manifest_sha256": DIGEST,
                    "adapter_manifest_sha256": DIGEST,
                },
            },
        )
    )
    assert cli.main([*PREFIX, "eval", "harness", str(VARIANT), "--adapter", str(ADAPTER)]) == 2
    assert len(calls) == 1


def test_adapter_lineage_is_a_typed_authenticated_read(wire: Any) -> None:
    calls, replies = wire
    replies.append(
        (
            200,
            {
                "adapter_id": str(ADAPTER),
                "base": {
                    "model_id": str(MODEL),
                    "variant_id": str(VARIANT),
                    "base_manifest_sha256": DIGEST,
                },
                "parent": None,
                "ancestors": [],
            },
        )
    )
    assert cli.main([*PREFIX, "adapter", "lineage", str(ADAPTER)]) == 0
    assert len(calls) == 1 and calls[0].method == "GET"
    assert calls[0].url.path == f"{ROOT}/adapters/{ADAPTER}/lineage"
    assert calls[0].headers["Authorization"] == "Bearer private-token"
