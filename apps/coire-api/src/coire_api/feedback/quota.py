"""Serialize counted database copies and unreleased private export staging."""

from sqlalchemy import String, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.db import (
    ChatFeedbackProvenanceRow,
    ComparisonPairRow,
    PreferenceExportRow,
    TrainingStorageReservationRow,
)
from coire_core.errors import FeedbackQuotaExceeded
from coire_core.settings import Settings


async def require_feedback_capacity(
    session: AsyncSession, additional_bytes: int, settings: Settings
) -> None:
    if type(additional_bytes) is not int or additional_bytes < 0:
        raise ValueError("feedback allocation must be a nonnegative integer")
    await session.execute(
        select(func.pg_advisory_xact_lock(func.hashtextextended("coire.feedback.disk", 0)))
    )
    counted = await feedback_counted_bytes(session)
    if counted + additional_bytes > settings.feedback_storage_quota_bytes:
        raise FeedbackQuotaExceeded("Feedback storage capacity is unavailable")


async def feedback_counted_bytes(session: AsyncSession) -> int:
    counted = 0
    for model in (ChatFeedbackProvenanceRow, ComparisonPairRow):
        counted += int(
            await session.scalar(select(func.coalesce(func.sum(model.counted_bytes), 0))) or 0
        )
    counted += int(
        await session.scalar(
            select(func.coalesce(func.sum(TrainingStorageReservationRow.bytes), 0)).where(
                TrainingStorageReservationRow.id.in_(
                    select(TrainingStorageReservationRow.id).join(
                        PreferenceExportRow,
                        PreferenceExportRow.staging["hold_id"].as_string()
                        == cast(TrainingStorageReservationRow.id, String),
                    )
                ),
                TrainingStorageReservationRow.state.in_(("held", "retained", "releasing")),
            )
        )
        or 0
    )
    return counted


def feedback_copy_bytes(
    prompt: list[dict[str, object]],
    original: str,
    candidate: str | None = None,
    *,
    allow_identical: bool = False,
) -> int:
    """Validate prospective copied content before allocating a database body."""
    from pydantic import ValidationError

    from coire_core.errors import FeedbackValidationError
    from coire_core.models.preference import PreferenceRow, canonical_bytes

    try:
        validated_candidate = candidate
        if allow_identical and candidate and (candidate == original or not candidate.strip()):
            validated_candidate = ("a" if candidate[0] != "a" else "b") + candidate[1:]
        PreferenceRow.model_validate(
            {
                "prompt": prompt,
                "chosen": original,
                "rejected": validated_candidate
                if validated_candidate is not None
                else ("pending" if original != "pending" else "alternative"),
            }
        )
        return (
            len(canonical_bytes(prompt)) + len(original.encode()) + len((candidate or "").encode())
        )
    except (ValidationError, UnicodeError, ValueError):
        raise FeedbackValidationError(
            "Feedback contribution exceeds text bounds or is ineligible"
        ) from None
