"""Bounded metadata selection; source bytes are read only after admission."""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import and_, case, false, func, or_, select, true
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased
from sqlalchemy.sql.elements import ColumnElement

from coire_api.db import (
    ChatConversationRow,
    ComparisonPairRow,
    FeedbackPreferenceRow,
    FeedbackRow,
    UserRow,
)
from coire_core.errors import FeedbackConflict
from coire_core.models.feedback import PreferenceExportCreate, PreferenceExportFilters
from coire_core.models.preference import PreferenceRow


def effective_judgement(
    labels: Sequence[FeedbackRow],
    source: Literal["owner_preferred", "owner", "admin"],
    generation: int,
) -> FeedbackRow | None:
    valid = {
        label.source: label
        for label in labels
        if label.kind == "pair"
        and label.judgement in {"original", "candidate"}
        and label.withdrawn_at is None
        and label.capture_generation == generation
    }
    if source == "owner_preferred":
        return valid.get("owner") or valid.get("admin")
    return valid.get(source)


def matches_judgement(label: FeedbackRow, filters: PreferenceExportFilters) -> bool:
    return (
        (filters.tag is None or filters.tag in label.tags)
        and (filters.from_time is None or label.updated_at >= filters.from_time)
        and (filters.until is None or label.updated_at < filters.until)
    )


@dataclass(frozen=True)
class ExportSource:
    pair_id: str
    owner_id: uuid.UUID
    conversation_id: uuid.UUID
    capture_generation: int
    pair_version: int
    judgement_id: uuid.UUID
    judgement_version: int
    judgement: str
    judgement_source: str
    actor_id: uuid.UUID


@dataclass(frozen=True)
class MaterializedSource:
    """Private transient row; never a workflow argument or result."""

    row: PreferenceRow
    source_message_id: uuid.UUID
    target: dict[str, object]
    prompt_sha256: str
    content_sha256: str


async def materialize_source(session: AsyncSession, source: ExportSource) -> MaterializedSource:
    pair = await session.get(ComparisonPairRow, source.pair_id, populate_existing=True)
    label = await session.get(FeedbackRow, source.judgement_id, populate_existing=True)
    preference = await session.get(FeedbackPreferenceRow, source.owner_id, populate_existing=True)
    owner = await session.get(UserRow, source.owner_id, populate_existing=True)
    conversation = await session.get(
        ChatConversationRow, source.conversation_id, populate_existing=True
    )
    if (
        pair is None
        or label is None
        or preference is None
        or owner is None
        or conversation is None
        or not owner.active
        or conversation.deleted_at is not None
        or conversation.owner_user_id != source.owner_id
        or not preference.enabled
        or preference.capture_generation != source.capture_generation
        or pair.withdrawn_at is not None
        or pair.version != source.pair_version
        or pair.generation_state != "ready"
        or pair.target is None
        or pair.selection_state not in {"pending", "chosen"}
        or (pair.selection_state == "pending" and pair.expires_at <= datetime.now(UTC))
        or label.withdrawn_at is not None
        or label.version != source.judgement_version
        or label.capture_generation != source.capture_generation
        or label.judgement != source.judgement
        or label.pair_id != source.pair_id
        or label.actor_user_id != source.actor_id
        or label.source != source.judgement_source
    ):
        raise FeedbackConflict("Export source changed during staging")
    chosen, rejected = (
        (pair.original, pair.candidate)
        if source.judgement == "original"
        else (pair.candidate, pair.original)
    )
    row = PreferenceRow.model_validate(
        {"prompt": pair.prompt, "chosen": chosen, "rejected": rejected}
    )
    return MaterializedSource(
        row, pair.source_message_id, pair.target, row.prompt_sha256(), row.content_sha256()
    )


async def select_export_sources(
    session: AsyncSession,
    request: PreferenceExportCreate,
) -> tuple[list[ExportSource], int]:
    """Count in SQL before fetching at most 10,000 content-free source identities.

    A larger selection returns its actual count and no partial snapshot. Bodies
    are deliberately absent from this query, including when selection is huge.
    """
    pair = ComparisonPairRow
    owner, admin = aliased(FeedbackRow), aliased(FeedbackRow)

    def current(label: type[FeedbackRow]) -> ColumnElement[bool]:
        return and_(
            label.pair_id == pair.id,
            label.kind == "pair",
            label.judgement.in_(["original", "candidate"]),
            label.withdrawn_at.is_(None),
            label.capture_generation == pair.capture_generation,
        )

    use_owner: ColumnElement[bool] = owner.id.is_not(None)
    if request.source != "owner_preferred":
        use_owner = true() if request.source == "owner" else false()
    chosen_id = case((use_owner, owner.id), else_=admin.id)
    chosen_version = case((use_owner, owner.version), else_=admin.version)
    chosen_value = case((use_owner, owner.judgement), else_=admin.judgement)
    chosen_actor = case((use_owner, owner.actor_user_id), else_=admin.actor_user_id)
    chosen_source = case((use_owner, owner.source), else_=admin.source)
    chosen_tags = case((use_owner, owner.tags), else_=admin.tags)
    chosen_date = case((use_owner, owner.updated_at), else_=admin.updated_at)
    statement = (
        select(
            pair.id,
            pair.owner_user_id,
            pair.conversation_id,
            pair.capture_generation,
            pair.version,
            chosen_id,
            chosen_version,
            chosen_value,
            chosen_source,
            chosen_actor,
        )
        .select_from(pair)
        .join(UserRow, UserRow.id == pair.owner_user_id)
        .join(FeedbackPreferenceRow, FeedbackPreferenceRow.owner_user_id == pair.owner_user_id)
        .join(ChatConversationRow, ChatConversationRow.id == pair.conversation_id)
        .outerjoin(owner, and_(current(owner), owner.source == "owner"))
        .outerjoin(admin, and_(current(admin), admin.source == "admin"))
        .where(
            UserRow.active.is_(True),
            FeedbackPreferenceRow.enabled.is_(True),
            FeedbackPreferenceRow.capture_generation == pair.capture_generation,
            ChatConversationRow.owner_user_id == pair.owner_user_id,
            ChatConversationRow.deleted_at.is_(None),
            pair.withdrawn_at.is_(None),
            pair.generation_state == "ready",
            chosen_id.is_not(None),
            or_(
                pair.selection_state == "chosen",
                and_(pair.selection_state == "pending", pair.expires_at > datetime.now(UTC)),
            ),
        )
    )
    filters = request.filters
    for field, value in (
        ("model_id", filters.model_id),
        ("variant_id", filters.variant_id),
        ("adapter_id", filters.adapter_id),
    ):
        if value is not None:
            statement = statement.where(pair.target[field].astext == str(value))
    if filters.owner_id is not None:
        statement = statement.where(pair.owner_user_id == filters.owner_id)
    if filters.tag is not None:
        statement = statement.where(chosen_tags.op("@>")([filters.tag]))
    if filters.from_time is not None:
        statement = statement.where(chosen_date >= filters.from_time)
    if filters.until is not None:
        statement = statement.where(chosen_date < filters.until)
    count = await session.scalar(select(func.count()).select_from(statement.subquery())) or 0
    if count > 10000:
        return [], count
    records = (await session.execute(statement.order_by(pair.id).limit(10001))).all()
    # A concurrent insertion between count and read cannot silently truncate.
    if len(records) > 10000:
        return [], len(records)
    return [ExportSource(*record) for record in records], len(records)
