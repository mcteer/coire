"""Feedback requests cannot carry authority, content or unbounded labels."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from coire_core.models.feedback import (
    AdminPairJudgement,
    ComparisonCreate,
    FeedbackPreferenceUpdate,
    PreferenceExportFilters,
    ThumbUpdate,
)


def test_caller_cannot_supply_answer_model_or_owner() -> None:
    valid = {
        "client_request_id": uuid.uuid4(),
        "expected_revision": 1,
        "source_message_id": uuid.uuid4(),
    }
    for field in ("prompt", "model_id", "owner_id", "candidate"):
        with pytest.raises(ValidationError):
            ComparisonCreate.model_validate({**valid, field: "forged"})


@pytest.mark.parametrize("tags", [["x"] * 2, ["../escape"], ["x"] * 17, ["x" * 33]])
def test_tag_bounds_and_uniqueness(tags: list[str]) -> None:
    with pytest.raises(ValidationError):
        ThumbUpdate(client_request_id=uuid.uuid4(), expected_version=0, judgement="up", tags=tags)


def test_clear_is_explicit_and_setting_version_is_required() -> None:
    assert (
        ThumbUpdate(client_request_id=uuid.uuid4(), expected_version=0, judgement=None).judgement
        is None
    )
    with pytest.raises(ValidationError):
        FeedbackPreferenceUpdate.model_validate(
            {"client_request_id": uuid.uuid4(), "enabled": False}
        )


def test_dates_are_half_open_and_aware() -> None:
    now = datetime.now(UTC)
    assert PreferenceExportFilters.model_validate(
        {"from": now, "until": now + timedelta(seconds=1)}
    )
    with pytest.raises(ValidationError):
        PreferenceExportFilters.model_validate({"from": now, "until": now})
    with pytest.raises(ValidationError):
        PreferenceExportFilters.model_validate({"from": now.replace(tzinfo=None)})


def test_new_admin_judgement_has_explicit_zero_version_and_skip() -> None:
    assert AdminPairJudgement(expected_version=0, choice="skip").choice == "skip"
    with pytest.raises(ValidationError):
        AdminPairJudgement(expected_version=-1, choice="original")


def test_source_settings_allow_unseeded_native_chat_but_freeze_sampling() -> None:
    from coire_core.models.feedback import FeedbackGenerationSettings

    settings = FeedbackGenerationSettings(
        temperature=0.0,
        top_p=1.0,
        top_k=0,
        min_p=0.0,
        max_tokens=1024,
        seed=None,
        enable_thinking=False,
    )
    assert settings.seed is None and settings.top_k == 0
    assert settings.model_dump()["enable_thinking"] is False


def test_comparison_accounting_is_content_free_and_survives_source_erasure() -> None:
    import uuid
    from datetime import UTC, datetime

    from coire_core.models.feedback import ComparisonAccounting

    accounting = ComparisonAccounting(
        request_id=uuid.uuid4(),
        owner_id=uuid.uuid4(),
        principal_kind="user",
        model_id=uuid.uuid4(),
        variant_id=uuid.uuid4(),
        started_at=datetime.now(UTC),
        prompt_tokens=2,
        completion_tokens=3,
    )
    assert accounting.completion_tokens == 3 and not accounting.settled
    with pytest.raises(ValidationError):
        ComparisonAccounting.model_validate({**accounting.model_dump(), "prompt": "private"})
