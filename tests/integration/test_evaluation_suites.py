"""Offline real suite generation on an isolated node, never on core."""

from collections.abc import Callable
from pathlib import Path

import pytest
from evaluation_engine_fixtures import evaluation_engine, phase

from coire_core.models.evaluation import TemplateId, canonical_digest
from coire_node.testing.harness import Agent

pytestmark = [pytest.mark.engine, pytest.mark.integration]
__all__ = ["evaluation_engine"]


@pytest.mark.parametrize("template_id", ["harness-capability", "task-coding-instructions"])
async def test_real_fixed_suite_records_every_generation(
    evaluation_engine: tuple[Agent, Path, Path],
    template_id: TemplateId,
) -> None:
    agent, candidate, _ = evaluation_engine
    work, result = await phase(agent, candidate, template_id)
    assert result.outcome == "succeeded"
    assert len(result.outputs) == work.suite.template.case_count
    assert len(result.scores) == work.suite.template.case_count
    assert all(
        output.prompt_tokens > 0 and output.completion_tokens > 0 for output in result.outputs
    )
    assert all(score.score is not None and 0 <= score.score <= 1 for score in result.scores)


async def test_real_distinct_judge_keeps_strict_success_or_explicit_infrastructure_failure(
    evaluation_engine: tuple[Agent, Path, Path],
    record_property: Callable[[str, object], None],
) -> None:
    agent, candidate, judge = evaluation_engine
    _, generated = await phase(agent, candidate, "judge-rubric")
    assert generated.outcome == "succeeded"
    work, judged = await phase(agent, judge, "judge-rubric", previous=generated.outputs)
    record_property("judge_outcome", judged.outcome)
    record_property("judge_reason", judged.reason or "")
    record_property("judge_cases", len(judged.scores))
    record_property("judge_base_sha256", work.target.target.base_manifest_sha256)
    if judged.outcome == "succeeded":
        assert len(judged.scores) == work.suite.template.case_count
        assert all(score.rubric is not None for score in judged.scores)
    else:
        # A small judge may not satisfy the fixed schema. This must remain an
        # explicit failure, never synthesized passing evidence or a numeric aggregate.
        assert judged.outcome == "failed" and judged.reason == "malformed_judge"
    assert not judged.outputs


async def test_real_fresh_execution_preserves_old_evidence_digest(
    evaluation_engine: tuple[Agent, Path, Path],
) -> None:
    agent, candidate, _ = evaluation_engine
    first_work, first = await phase(agent, candidate, "task-coding-instructions")
    digest = canonical_digest(first)
    second_work, second = await phase(agent, candidate, "task-coding-instructions")
    assert first_work.attempt_id != second_work.attempt_id
    assert first_work.run_id != second_work.run_id
    assert first.outcome == second.outcome == "succeeded"
    assert first.target == second.target and first.suite_sha256 == second.suite_sha256
    assert canonical_digest(first) == digest
