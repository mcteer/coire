"""New preference work is disabled until explicitly admitted; cleanup is independent."""

import pytest
from pydantic import ValidationError

from coire_core.settings import Settings


def test_preference_defaults_are_bounded_and_disabled() -> None:
    value = Settings(_secrets_dir="/nonexistent")  # type: ignore[call-arg]
    assert value.preference_training_enabled is False
    assert value.feedback_storage_quota_bytes == 1024**3
    assert value.feedback_purge_batch_size == 100


@pytest.mark.parametrize(
    "updates", [{"feedback_storage_quota_bytes": 0}, {"feedback_purge_batch_size": 101}]
)
def test_feedback_limits_refuse_unbounded_values(updates: dict[str, int]) -> None:
    with pytest.raises(ValidationError):
        Settings(_secrets_dir="/nonexistent", **updates)  # type: ignore[call-arg,arg-type]
