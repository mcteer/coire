"""Evaluation dispatch cannot widen the admitted environment or run a user harness."""

import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

import coire_agent.__main__ as entry
from coire_core.models.evaluation import (
    EvaluationWorkerResult,
    EvaluationWorkload,
    canonical_digest,
)

FIXTURE = Path(__file__).resolve().parents[3] / "tests/fixtures/evaluations/workload.json"


def environment(work: EvaluationWorkload, output: Path) -> dict[str, str]:
    return {
        "COIRE_RUN_ID": str(work.run_id),
        "COIRE_PROFILE": "general",
        "COIRE_MODEL_ID": str(work.target.target.model_id),
        "COIRE_VERIFIED_VARIANT_ID": str(work.target.target.variant_id),
        "COIRE_INFERENCE_TARGET": work.target.target.model_dump_json(),
        "COIRE_OUTPUT_DIR": str(output.parent),
        "COIRE_RUN_PURPOSE": "evaluation",
        "COIRE_EVALUATION_RUNTIME": work.target.runtime.model_dump_json(),
        "COIRE_API_URL": "https://synthetic.test/v1",
        "COIRE_RUN_TOKEN": "synthetic-inert-token",
    }


@pytest.mark.parametrize(
    "field,value",
    [
        ("COIRE_RUN_PURPOSE", "harness"),
        ("COIRE_MODEL_ID", "00000000-0000-4000-8000-000000000001"),
        ("COIRE_OUTPUT_DIR", "/foreign"),
    ],
)
async def test_wrong_admitted_environment_refuses_before_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: str,
) -> None:
    work = EvaluationWorkload.model_validate_json(FIXTURE.read_bytes())
    request, output = tmp_path / "request.json", tmp_path / "output/result.json"
    request.write_text(work.model_dump_json())
    env = environment(work, output)
    env[field] = value
    execute = AsyncMock()
    monkeypatch.setattr(entry, "execute_phase", execute)
    with pytest.raises(ValueError, match="admitted"):
        await entry.execute(environ=env, request_path=request, result_path=output)
    execute.assert_not_awaited()
    assert not output.exists()


async def test_evaluation_dispatch_writes_only_typed_worker_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    work = EvaluationWorkload.model_validate_json(FIXTURE.read_bytes())
    request, output = tmp_path / "request.json", tmp_path / "output/result.json"
    request.write_text(work.model_dump_json())
    now = datetime.now(UTC)
    result = EvaluationWorkerResult(
        evaluation_id=work.evaluation_id,
        attempt_id=work.attempt_id,
        run_id=work.run_id,
        fence=work.fence,
        phase=work.phase,
        request_sha256=canonical_digest(work),
        suite_sha256=work.suite.content_sha256,
        cases_sha256=work.suite.template.cases_sha256,
        runtime=work.target.runtime,
        target=work.target.target,
        outcome="failed",
        reason="model_unavailable",
        started_at=now,
        finished_at=now,
    )
    execute = AsyncMock(return_value=result)
    monkeypatch.setattr(entry, "execute_phase", execute)
    await entry.execute(environ=environment(work, output), request_path=request, result_path=output)
    assert EvaluationWorkerResult.model_validate_json(output.read_bytes()) == result
    assert execute.await_args is not None and execute.await_args.args[0] == work
    assert not output.with_suffix(".json.tmp").exists()


def test_unknown_workload_version_refused(tmp_path: Path) -> None:
    document = json.loads(FIXTURE.read_bytes())
    document["workload_version"] = 99
    request = tmp_path / "request.json"
    request.write_text(json.dumps(document))
    with pytest.raises(ValueError):
        entry.load_request(request)
