"""Qualification pressure repeats installed suites and obeys only bound stop receipts."""

import uuid
from pathlib import Path

import pytest

from coire_agent.evaluation_pressure import execute_pressure, stopped
from coire_core.models.evaluation import (
    EvaluationGeneration,
    EvaluationPressureBinding,
    EvaluationPressureStop,
    EvaluationWorkload,
    canonical_digest,
)

FIXTURE = Path(__file__).resolve().parents[3] / "tests/fixtures/evaluations/workload.json"


async def test_pressure_stops_after_a_complete_phase_and_does_not_generate_again(
    tmp_path: Path,
) -> None:
    work = EvaluationWorkload.model_validate_json(FIXTURE.read_bytes()).model_copy(
        update={"pressure": EvaluationPressureBinding(measurement_id=uuid.uuid4())}
    )
    marker = tmp_path / "measurement-stop.json"
    calls = 0

    async def generate(
        system: str, prompt: str, generation: EvaluationGeneration
    ) -> tuple[str, int, int]:
        nonlocal calls
        calls += 1
        if calls == work.suite.template.case_count:
            assert work.pressure is not None
            marker.write_text(
                EvaluationPressureStop(
                    run_id=work.run_id,
                    measurement_id=work.pressure.measurement_id,
                    request_sha256=canonical_digest(work),
                ).model_dump_json()
            )
        return "wrong", 10, 1

    result = await execute_pressure(work, generate, runtime=work.target.runtime, stop_path=marker)
    assert result.outcome == "succeeded" and calls == 16 and len(result.outputs) == 16


def test_foreign_or_linked_stop_marker_is_refused(tmp_path: Path) -> None:
    work = EvaluationWorkload.model_validate_json(FIXTURE.read_bytes()).model_copy(
        update={"pressure": EvaluationPressureBinding(measurement_id=uuid.uuid4())}
    )
    marker = tmp_path / "stop.json"
    marker.write_text(
        EvaluationPressureStop(
            run_id=work.run_id, measurement_id=uuid.uuid4(), request_sha256=canonical_digest(work)
        ).model_dump_json()
    )
    with pytest.raises(ValueError, match="another"):
        stopped(work, marker)
    linked = tmp_path / "linked.json"
    linked.symlink_to(marker)
    with pytest.raises(OSError):
        stopped(work, linked)


async def test_training_pressure_measures_input_scanning_during_every_iteration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import Mock

    from evaluation_input_fixtures import staged_workload

    import coire_agent.evaluation_contamination as module

    work = staged_workload(tmp_path).model_copy(
        update={
            "pressure": EvaluationPressureBinding(measurement_id=uuid.uuid4(), max_iterations=2)
        }
    )
    scan = Mock(wraps=module.scan_training_inputs)
    monkeypatch.setattr(module, "scan_training_inputs", scan)

    async def generate(
        system: str, prompt: str, generation: EvaluationGeneration
    ) -> tuple[str, int, int]:
        return "wrong", 10, 1

    result = await execute_pressure(
        work,
        generate,
        runtime=work.target.runtime,
        stop_path=tmp_path / "stop.json",
        input_root=tmp_path,
    )
    assert result.outcome == "succeeded" and scan.call_count == 2
    assert result.contamination is not None and result.contamination.status == "overlap"
