"""Durable execution plans preserve ordered exact targets and a separate judge phase."""

from pathlib import Path

from coire_core.models.evaluation import EvaluationWorkload, SuiteKind, SuiteMode
from coire_scheduler.evaluations import phase_plan

FIXTURE = Path(__file__).resolve().parents[4] / "tests/fixtures/evaluations/workload.json"


def test_task_and_judge_phases_are_sequential_and_subject_bound() -> None:
    work = EvaluationWorkload.model_validate_json(FIXTURE.read_bytes())
    assert phase_plan(work.suite, [work.target]) == [("base", 0, work.target)]
    candidate = work.target.model_copy(update={"display_name": "candidate"})
    assert phase_plan(work.suite, [work.target, candidate]) == [
        ("base", 0, work.target),
        ("candidate", 1, candidate),
    ]
    judge = work.target.model_copy(update={"display_name": "judge"})
    suite = work.suite.model_copy(
        update={
            "judge": judge,
            "template": work.suite.template.model_copy(
                update={"kind": SuiteKind.JUDGE, "mode": SuiteMode.RUBRIC}
            ),
        }
    )
    assert phase_plan(suite, [work.target, candidate])[-1] == ("judge", 0, judge)


def test_harness_has_one_gate_phase() -> None:
    work = EvaluationWorkload.model_validate_json(FIXTURE.read_bytes())
    suite = work.suite.model_copy(
        update={
            "template": work.suite.template.model_copy(
                update={"kind": SuiteKind.HARNESS, "mode": SuiteMode.CAPABILITY}
            )
        }
    )
    assert phase_plan(suite, [work.target]) == [("harness", 0, work.target)]
