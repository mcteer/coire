"""Exact input overlap excludes completions and samples never consumed at the boundary."""

import uuid
from pathlib import Path

import pytest
from evaluation_input_fixtures import staged_workload

from coire_core.models.datasets import DatasetFormat
from coire_core.models.evaluation import EvaluationAssertion, EvaluationCase, EvaluationWorkload
from coire_core.training_data import normalize_row


def test_same_input_different_completion_and_unconsumed_rows() -> None:
    from coire_agent.evaluation_contamination import scan_inputs

    identity = uuid.uuid4()
    consumed = normalize_row(
        {"prompt": "exact prompt", "completion": "different answer"},
        format=DatasetFormat.PROMPT_COMPLETION,
        dataset_id=identity,
        source_row=1,
    )
    unconsumed = normalize_row(
        {"prompt": "not consumed", "completion": "exact prompt"},
        format=DatasetFormat.PROMPT_COMPLETION,
        dataset_id=identity,
        source_row=2,
    )
    fixtures = [
        EvaluationCase(
            id="hit",
            category="instruction",
            assertions=[EvaluationAssertion(kind="exact", value="answer")],
            prompt="exact prompt",
        ),
        EvaluationCase(
            id="no-hit",
            category="instruction",
            assertions=[EvaluationAssertion(kind="exact", value="answer")],
            prompt="not consumed",
        ),
    ]
    result = scan_inputs(fixtures, [consumed], data_sha256=["a" * 64])
    assert result.status == "overlap"
    assert result.case_ids == ["hit"] and result.selected_rows == 1 and result.checked_cases == 2
    assert scan_inputs(fixtures[:1], [unconsumed], data_sha256=["a" * 64]).status == "clean"


@pytest.mark.parametrize("source", ["missing", "text"])
def test_missing_data_and_all_tokens_text_never_claim_clean(source: str) -> None:
    from coire_agent.evaluation_contamination import scan_inputs

    examples = (
        None
        if source == "missing"
        else [
            normalize_row(
                {"text": "evaluation prompt"},
                format=DatasetFormat.TEXT,
                dataset_id=uuid.uuid4(),
                source_row=1,
            )
        ]
    )
    result = scan_inputs(
        [
            EvaluationCase(
                id="sample",
                category="instruction",
                assertions=[EvaluationAssertion(kind="exact", value="answer")],
                prompt="evaluation prompt",
            )
        ],
        examples,
        data_sha256=["a" * 64],
    )
    assert result.status == "unavailable"
    assert result.reason == ("input_missing" if source == "missing" else "unsupported_format")
    assert result.hit_count == 0


def test_chat_input_preserves_system_context_and_excludes_only_final_target() -> None:
    from coire_agent.evaluation_contamination import case_input_digest, input_digest

    identity = uuid.uuid4()
    example = normalize_row(
        {
            "messages": [
                {"role": "system", "content": "system policy"},
                {"role": "user", "content": "exact prompt"},
                {"role": "assistant", "content": "private target"},
            ]
        },
        format=DatasetFormat.CONVERSATION,
        dataset_id=identity,
        source_row=1,
    )
    assert input_digest(example) == case_input_digest("exact prompt", system="system policy")
    assert input_digest(example) != case_input_digest("exact prompt")


def test_private_checkpoint_scan_checks_only_consumed_inputs_and_fails_closed(
    tmp_path: Path,
) -> None:
    from coire_agent.evaluation_contamination import scan_training_inputs

    work = staged_workload(tmp_path)
    result = scan_training_inputs(work, tmp_path)
    assert result.status == "overlap" and result.selected_rows == 1
    assert result.case_ids == ["coding-1"]
    assert work.training is not None
    changed = work.model_copy(
        update={"training": work.training.model_copy(update={"completed_update": 2})}
    )
    assert scan_training_inputs(changed, tmp_path).status == "unavailable"
    (tmp_path / "training-state.json").unlink()
    assert scan_training_inputs(work, tmp_path).status == "unavailable"


@pytest.mark.parametrize(
    "changed", ["state_digest", "source_bytes", "missing_file", "duplicate_source"]
)
def test_training_workload_rejects_mismatched_private_input_declarations(
    tmp_path: Path, changed: str
) -> None:
    work = staged_workload(tmp_path)
    value = work.model_dump(mode="json")
    if changed == "state_digest":
        value["training"]["state_sha256"] = "f" * 64
    elif changed == "source_bytes":
        value["training"]["sources"][0]["source_bytes"] += 1
    elif changed == "duplicate_source":
        value["training"]["sources"].append(value["training"]["sources"][0])
    else:
        value["input_files"] = value["input_files"][1:]
    with pytest.raises(ValueError):
        EvaluationWorkload.model_validate(value)


def test_reused_input_marks_every_case_with_that_exact_input() -> None:
    from coire_agent.evaluation_contamination import scan_inputs

    first = EvaluationCase(
        id="first",
        category="instruction",
        prompt="shared",
        assertions=[EvaluationAssertion(kind="exact", value="a")],
    )
    second = first.model_copy(update={"id": "second"})
    example = normalize_row(
        {"prompt": "shared", "completion": "target"},
        format=DatasetFormat.PROMPT_COMPLETION,
        dataset_id=uuid.uuid4(),
        source_row=1,
    )
    result = scan_inputs([first, second], [example], data_sha256=["a" * 64])
    assert result.case_ids == ["first", "second"] and result.hit_count == 2
