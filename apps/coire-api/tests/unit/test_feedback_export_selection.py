"""Effective judgement precedence is resolved before export filters."""

import uuid
from datetime import UTC, datetime, timedelta

from coire_api.db import FeedbackRow
from coire_api.feedback.export_selection import effective_judgement, matches_judgement
from coire_core.models.feedback import PreferenceExportFilters


def label(source: str, judgement: str = "original") -> FeedbackRow:
    return FeedbackRow(
        id=uuid.uuid4(),
        owner_user_id=uuid.uuid4(),
        actor_user_id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
        pair_id="01AAAAAAAAAAAAAAAAAAAAAAAA",
        kind="pair",
        source=source,
        judgement=judgement,
        tags=["clear"],
        capture_generation=3,
        version=1,
        updated_at=datetime.now(UTC),
    )


def test_owner_precedence_does_not_fall_back_after_filtering() -> None:
    owner, admin = label("owner"), label("admin", "candidate")
    owner.tags = []
    selected = effective_judgement([admin, owner], "owner_preferred", 3)
    assert selected is owner
    assert not matches_judgement(selected, PreferenceExportFilters(tag="clear"))
    assert effective_judgement([admin, owner], "admin", 3) is admin


def test_withdrawn_and_old_generation_labels_never_export() -> None:
    owner, admin = label("owner"), label("admin")
    owner.withdrawn_at = datetime.now(UTC)
    assert effective_judgement([owner, admin], "owner_preferred", 3) is admin
    assert effective_judgement([owner, admin], "owner", 3) is None
    admin.capture_generation = 2
    assert effective_judgement([owner, admin], "owner_preferred", 3) is None


def test_date_filter_is_half_open_on_effective_label() -> None:
    owner = label("owner")
    assert matches_judgement(
        owner, PreferenceExportFilters.model_validate({"from": owner.updated_at})
    )
    assert not matches_judgement(owner, PreferenceExportFilters(until=owner.updated_at))
    assert not matches_judgement(
        owner,
        PreferenceExportFilters.model_validate(
            {"from": owner.updated_at + timedelta(microseconds=1)}
        ),
    )


def test_thumbs_and_cleared_labels_are_not_pairs() -> None:
    owner = label("owner")
    owner.kind = "thumb"
    assert effective_judgement([owner], "owner", 3) is None
    owner.kind = "pair"
    owner.judgement = None
    assert effective_judgement([owner], "owner", 3) is None
