"""Scoring observes bounded outputs without executing generated code."""

import difflib
from pathlib import Path

import pytest

from coire_agent.evaluation_tasks import apply_fixture_patch, score_case
from coire_core.evaluation_suites import cases
from coire_core.models.evaluation import TemplateId

WORKLOAD_PATH = Path(__file__).resolve().parents[3] / "tests/fixtures/evaluations/workload.json"


def test_all_four_harness_categories_require_real_evidence() -> None:
    harness = cases("harness-capability")
    outputs = [
        '{"name":"read_file","arguments":{"path":"README.md"}}',
        '{"answer":"ok"}',
        "--- a/note.txt\n+++ b/note.txt\n@@ -1 +1,2 @@\n hello\n+coire-eval\n",
        "coire-context-sentinel-7419",
    ]
    assert {case.category for case in harness} == {
        "tool_calling",
        "structured_output",
        "edit_application",
        "long_context",
    }
    for case, output in zip(harness, outputs, strict=True):
        assert score_case(case, output)
        assert not score_case(case, "wrong")


def test_patch_only_changes_in_memory_fixed_fixture() -> None:
    fixture = "def add(a, b):\n    return 0\n"
    patch = "--- a/fixture.py\n+++ b/fixture.py\n@@ -1,2 +1,2 @@\n def add(a, b):\n-    return 0\n+    return a + b\n"
    assert apply_fixture_patch(fixture, patch) == "def add(a, b):\n    return a + b\n"
    assert score_case(cases("task-coding-instructions")[0], patch)
    for bad in (
        patch.replace("fixture.py", "../../secret"),
        patch + "--- a/other.py\n",
        patch.replace("return 0", "return 1"),
    ):
        with pytest.raises(ValueError):
            apply_fixture_patch(fixture, bad)


def test_instruction_scoring_requires_exact_whitespace_and_all_assertions() -> None:
    case = cases("task-coding-instructions")[8]
    assert score_case(case, "amber")
    assert not score_case(case, " amber ")
    assert not score_case(case, "amber\n")


async def test_phase_infrastructure_failure_is_explicit_and_never_complete_scores(
    caplog: pytest.LogCaptureFixture,
) -> None:

    from coire_agent.evaluation import execute_phase
    from coire_core.models.evaluation import EvaluationGeneration, EvaluationWorkload

    request = EvaluationWorkload.model_validate_json(WORKLOAD_PATH.read_bytes())

    async def fail(
        system: str, prompt: str, generation: EvaluationGeneration
    ) -> tuple[str, int, int]:
        raise ConnectionError("private transport error")

    with caplog.at_level("INFO", logger="coire_agent.evaluation"):
        result = await execute_phase(request, fail, runtime=request.target.runtime)
    assert result.outcome == "failed" and result.reason == "invalid_evidence"
    assert result.outputs == [] and result.scores == []
    assert "private transport error" not in result.model_dump_json()
    assert "private transport error" not in caplog.text
    assert len(caplog.records) == 1
    recorded = caplog.records[0]
    assert recorded.__dict__["run_id"] == str(request.run_id)
    assert recorded.__dict__["model_id"] == str(request.target.target.model_id)
    assert recorded.__dict__["safe_reason"] == "invalid_evidence"
    assert recorded.__dict__["error_type"] == "ConnectionError"
    assert recorded.exc_info is None
    assert not {"prompt", "outputs", "scores", "target"} & recorded.__dict__.keys()


@pytest.mark.parametrize("template_id", ["task-coding-instructions", "harness-capability"])
async def test_complete_phase_collects_every_case_and_finite_scores(
    template_id: TemplateId,
) -> None:
    from coire_agent.evaluation import execute_phase
    from coire_core.evaluation_suites import template
    from coire_core.models.evaluation import EvaluationGeneration, EvaluationWorkload

    work = EvaluationWorkload.model_validate_json(WORKLOAD_PATH.read_bytes())
    installed = template(template_id)
    work = work.model_copy(
        update={
            "phase": "harness" if template_id == "harness-capability" else "base",
            "suite": work.suite.model_copy(update={"template": installed}),
        }
    )
    fixtures = cases(template_id)
    by_prompt = {case.prompt: case for case in fixtures}
    calls: list[str] = []

    async def generate(
        system: str, prompt: str, generation: EvaluationGeneration
    ) -> tuple[str, int, int]:
        assert generation == work.suite.generation
        calls.append(prompt)
        assertion = by_prompt[prompt].assertions[0]
        if assertion.kind == "patch":
            name = "note.txt" if template_id == "harness-capability" else "fixture.py"
            answer = "".join(
                difflib.unified_diff(
                    (assertion.fixture or "").splitlines(keepends=True),
                    assertion.value.splitlines(keepends=True),
                    fromfile=f"a/{name}",
                    tofile=f"b/{name}",
                )
            )
        elif assertion.kind == "tool_call":
            answer = '{"name":"read_file","arguments":{"path":"README.md"}}'
        elif assertion.kind == "json_object":
            answer = '{"answer":"ok"}'
        else:
            answer = assertion.value
        return answer, 50, 10

    result = await execute_phase(work, generate, runtime=work.target.runtime)
    assert result.outcome == "succeeded" and result.reason is None
    assert len(calls) == len(result.outputs) == len(result.scores) == installed.case_count
    assert all(score.score == 1.0 and score.passed is True for score in result.scores)
    assert all(
        output.prompt_tokens == 50 and output.completion_tokens == 10 for output in result.outputs
    )
