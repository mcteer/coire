"""Recipes preserve source while rejecting executable, ambiguous or unbounded YAML."""

import hashlib

import pytest

from coire_api.training.specs import parse_spec, parse_submission
from coire_core.errors import TrainingUploadTooLarge, TrainingValidationError
from coire_core.models.training import TrainingSubmission

SOURCE = """# This exact comment is retained.
model:
  model_id: 10000000-0000-4000-8000-000000000001
  variant_id: 10000000-0000-4000-8000-000000000002
data:
  train:
    datasets:
      - dataset_id: 10000000-0000-4000-8000-000000000003
        sample_count: 8
        mixture_proportion: 1.0
    epoch_samples: 8
  validation:
    dataset_ids: [10000000-0000-4000-8000-000000000003]
parameterization:
  kind: lora
  target_modules: [self_attn.q_proj]
optim:
  updates: 32
  batch_size: 2
  learning_rate: 1e-5
output:
  adapter_slug: recipe-test
"""


def test_source_is_verbatim_and_comment_changes_do_not_change_client_intent() -> None:
    parsed = parse_submission(TrainingSubmission(source_yaml=SOURCE))
    changed = parse_submission(
        TrainingSubmission(source_yaml=SOURCE.replace("exact comment", "new comment"))
    )
    assert parsed.source_yaml == SOURCE
    assert parsed.source_sha256 == hashlib.sha256(SOURCE.encode()).hexdigest()
    assert parsed.source_sha256 != changed.source_sha256
    assert parsed.intent_sha256 == changed.intent_sha256
    assert parsed.spec.optim.learning_rate == 1e-5


@pytest.mark.parametrize(
    "suffix",
    [
        "seed: 0\nseed: 1\n",
        "unexpected: !execute code\n",
        "unexpected: &x [1,2]\ncopy: *x\n",
        "unexpected: !!binary SGVsbG8=\n",
    ],
)
def test_ambiguous_or_executable_yaml_is_refused(suffix: str) -> None:
    with pytest.raises(TrainingValidationError):
        parse_spec(SOURCE + suffix)


def test_duplicate_nested_key_and_depth_overflow_are_refused() -> None:
    with pytest.raises(TrainingValidationError, match="duplicate"):
        parse_spec(SOURCE.replace("  updates: 32", "  updates: 32\n  updates: 64"))
    with pytest.raises(TrainingValidationError, match="depth"):
        parse_spec(SOURCE + "extra: " + "[" * 32 + "1" + "]" * 32)


@pytest.mark.parametrize("value", [".nan", ".inf", "-.inf", "0", "-0.1", "true", '"1e-5"'])
def test_invalid_learning_rate_reports_field_without_echoing_value(value: str) -> None:
    with pytest.raises(TrainingValidationError, match=r"optim\.learning_rate"):
        parse_spec(SOURCE.replace("learning_rate: 1e-5", f"learning_rate: {value}"))


def test_large_utf8_and_non_mapping_recipe_are_refused() -> None:
    with pytest.raises(TrainingUploadTooLarge):
        parse_spec("é" * 32769)
    with pytest.raises(TrainingValidationError):
        parse_spec("[one, two]")


def test_form_and_yaml_are_equivalent_and_contradictions_are_refused() -> None:
    spec = parse_spec(SOURCE)
    assert (
        parse_submission(
            TrainingSubmission(source_yaml=SOURCE, source_kind="form", form_spec=spec)
        ).spec
        == spec
    )
    modified = spec.model_copy(update={"seed": 42})
    with pytest.raises(TrainingValidationError, match="form"):
        parse_submission(
            TrainingSubmission(source_yaml=SOURCE, source_kind="form", form_spec=modified)
        )


def test_error_does_not_echo_an_unknown_key_or_source_snippet() -> None:
    sensitive = "coire_not_a_real_secret_but_not_for_diagnostics"
    with pytest.raises(TrainingValidationError) as error:
        parse_spec(SOURCE + sensitive + ": value\n")
    assert sensitive not in error.value.detail
