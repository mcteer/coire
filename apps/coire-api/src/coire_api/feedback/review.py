"""Bounded human review, with sorted owner locks and no chat-context mutation."""

import hashlib
import uuid
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import and_, delete, exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.audit import write_principal_audit
from coire_api.auth import Principal
from coire_api.db import (
    ChatConversationRow,
    ComparisonPairRow,
    FeedbackMutationRow,
    FeedbackPreferenceRow,
    FeedbackReviewSkipRow,
    FeedbackRow,
    UserRow,
)
from coire_api.feedback.comparisons import judgement_receipt, pair_receipt
from coire_api.feedback.eligibility import (
    authorize_admin,
    lock_conversation,
    lock_owners,
    require_generation,
)
from coire_api.training.authorization import preflight_training_action
from coire_api.training.telemetry import observed
from coire_core.errors import FeedbackConflict, FeedbackForbidden, FeedbackNotFound
from coire_core.models.adapters import InferenceTarget
from coire_core.models.feedback import (
    AdminPairJudgement,
    FeedbackReviewDetail,
    FeedbackReviewPage,
    FeedbackReviewReceipt,
)
from coire_core.models.preference import PreferenceMessage, canonical_bytes


def actor(principal: Principal) -> uuid.UUID:
    return preflight_training_action(principal, method="GET", origin=None, browser_origin="")


async def locked_review_pair(
    session: AsyncSession, principal: Principal, identity: str
) -> ComparisonPairRow:
    reviewer = actor(principal)
    owner = await session.scalar(
        select(ComparisonPairRow.owner_user_id).where(ComparisonPairRow.id == identity)
    )
    if owner is None:
        await authorize_admin(session, principal)
        raise FeedbackNotFound()
    await lock_owners(session, {owner, reviewer})
    await authorize_admin(session, principal)
    snapshot = await session.get(ComparisonPairRow, identity, populate_existing=True)
    assert snapshot is not None
    await require_generation(session, owner, snapshot.capture_generation)
    await lock_conversation(session, owner, snapshot.conversation_id)
    pair = await session.get(
        ComparisonPairRow, identity, with_for_update=True, populate_existing=True
    )
    if pair is None or pair.withdrawn_at is not None:
        raise FeedbackForbidden("Comparison was withdrawn")
    validate_review_pair(pair)
    return pair


def validate_review_pair(pair: ComparisonPairRow) -> None:
    if (
        pair.generation_state != "ready"
        or pair.selection_state not in {"pending", "chosen"}
        or (pair.selection_state == "pending" and pair.expires_at <= datetime.now(UTC))
        or not pair.prompt
        or not pair.original
        or not pair.candidate
        or pair.target is None
    ):
        raise FeedbackNotFound()


async def project_review(
    session: AsyncSession, pair: ComparisonPairRow, *, labels: list[FeedbackRow] | None = None
) -> FeedbackReviewDetail:
    if labels is None:
        labels = list(
            await session.scalars(
                select(FeedbackRow).where(
                    FeedbackRow.kind == "pair",
                    FeedbackRow.pair_id == pair.id,
                    FeedbackRow.withdrawn_at.is_(None),
                    FeedbackRow.capture_generation == pair.capture_generation,
                )
            )
        )
    by_source = {row.source: row for row in labels}
    return FeedbackReviewDetail.model_validate(
        {
            **pair_receipt(pair).model_dump(mode="json"),
            "conversation_id": pair.conversation_id,
            "source_message_id": pair.source_message_id,
            "target": InferenceTarget.model_validate(pair.target),
            "original": pair.original,
            "candidate": pair.candidate,
            "selected": pair.selected_candidate,
            "owner_judgement": judgement_receipt(by_source["owner"])
            if "owner" in by_source
            else None,
            "admin_judgement": judgement_receipt(by_source["admin"])
            if "admin" in by_source
            else None,
            "owner_tags": by_source["owner"].tags if "owner" in by_source else [],
            "admin_tags": by_source["admin"].tags if "admin" in by_source else [],
            "eligibility": "eligible",
            "expires_at": pair.expires_at,
            "created_at": pair.created_at,
            "owner_id": pair.owner_user_id,
            "prompt": [PreferenceMessage.model_validate(message) for message in pair.prompt or []],
        }
    )


@observed("coire.api.feedback.review.read")
async def review_detail(
    session: AsyncSession, principal: Principal, identity: str
) -> FeedbackReviewDetail:
    return await project_review(session, await locked_review_pair(session, principal, identity))


@observed("coire.api.feedback.review.list")
async def review_page(
    session: AsyncSession,
    principal: Principal,
    *,
    state: Literal["unreviewed", "reviewed", "skipped"] = "unreviewed",
    cursor: str | None = None,
    limit: int = 25,
) -> FeedbackReviewPage:
    reviewer = actor(principal)
    if not 1 <= limit <= 100:
        raise ValueError("Review page is out of bounds")
    pair = ComparisonPairRow
    labelled = exists(
        select(FeedbackRow.id).where(
            FeedbackRow.kind == "pair",
            FeedbackRow.pair_id == pair.id,
            FeedbackRow.source == "admin",
            FeedbackRow.withdrawn_at.is_(None),
            FeedbackRow.capture_generation == pair.capture_generation,
        )
    )
    skipped = exists(
        select(FeedbackReviewSkipRow.pair_id).where(
            FeedbackReviewSkipRow.pair_id == pair.id,
            FeedbackReviewSkipRow.actor_user_id == reviewer,
        )
    )
    statement = (
        select(
            pair.id,
            pair.owner_user_id,
            pair.created_at,
            pair.conversation_id,
            pair.capture_generation,
        )
        .join(FeedbackPreferenceRow, FeedbackPreferenceRow.owner_user_id == pair.owner_user_id)
        .join(UserRow, UserRow.id == pair.owner_user_id)
        .join(ChatConversationRow, ChatConversationRow.id == pair.conversation_id)
        .where(
            UserRow.active.is_(True),
            FeedbackPreferenceRow.enabled.is_(True),
            FeedbackPreferenceRow.capture_generation == pair.capture_generation,
            ChatConversationRow.owner_user_id == pair.owner_user_id,
            ChatConversationRow.deleted_at.is_(None),
            pair.withdrawn_at.is_(None),
            pair.generation_state == "ready",
            or_(
                pair.selection_state == "chosen",
                and_(pair.selection_state == "pending", pair.expires_at > datetime.now(UTC)),
            ),
        )
    )
    statement = statement.where(
        labelled
        if state == "reviewed"
        else skipped
        if state == "skipped"
        else and_(~labelled, ~skipped)
    )
    if cursor is not None:
        from coire_api.training.service import decode_page_cursor

        created_at, identity = decode_page_cursor(cursor, "feedback-review")
        statement = statement.where(
            or_(
                pair.created_at > created_at,
                and_(pair.created_at == created_at, pair.id > identity),
            )
        )
    rows = list(
        (await session.execute(statement.order_by(pair.created_at, pair.id).limit(limit + 1))).all()
    )
    await lock_owners(session, {reviewer} | {row.owner_user_id for row in rows})
    await authorize_admin(session, principal)
    # Owner locks fence all withdrawal and label writers for this bounded page.
    # Revalidate each unique generation/conversation once before loading bodies.
    eligible_owners: set[uuid.UUID] = set()
    for owner, generation in sorted(
        {(row.owner_user_id, row.capture_generation) for row in rows[:limit]}
    ):
        try:
            await require_generation(session, owner, generation)
            eligible_owners.add(owner)
        except FeedbackForbidden:
            continue
    eligible_conversations: set[uuid.UUID] = set()
    for owner, conversation in sorted(
        {
            (row.owner_user_id, row.conversation_id)
            for row in rows[:limit]
            if row.owner_user_id in eligible_owners
        }
    ):
        try:
            await lock_conversation(session, owner, conversation)
            eligible_conversations.add(conversation)
        except FeedbackNotFound:
            continue
    identities = [
        row.id
        for row in rows[:limit]
        if row.owner_user_id in eligible_owners and row.conversation_id in eligible_conversations
    ]
    pairs = (
        list(
            await session.scalars(
                select(pair)
                .where(pair.id.in_(identities), pair.withdrawn_at.is_(None))
                .order_by(pair.created_at, pair.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        if identities
        else []
    )
    labels = (
        list(
            await session.scalars(
                select(FeedbackRow).where(
                    FeedbackRow.kind == "pair",
                    FeedbackRow.pair_id.in_(identities),
                    FeedbackRow.withdrawn_at.is_(None),
                )
            )
        )
        if identities
        else []
    )
    by_pair: dict[str, list[FeedbackRow]] = {}
    for label in labels:
        assert label.pair_id is not None
        by_pair.setdefault(label.pair_id, []).append(label)
    items = []
    for value in pairs:
        try:
            validate_review_pair(value)
        except FeedbackNotFound:
            continue
        items.append(
            await project_review(
                session,
                value,
                labels=[
                    label
                    for label in by_pair.get(value.id, [])
                    if label.capture_generation == value.capture_generation
                ],
            )
        )
    from coire_api.training.service import encode_page_cursor

    return FeedbackReviewPage(
        items=items,
        next_cursor=encode_page_cursor(
            "feedback-review", rows[limit - 1].created_at, rows[limit - 1].id
        )
        if len(rows) > limit
        else None,
    )


async def judge_pair(
    session: AsyncSession, principal: Principal, identity: str, body: AdminPairJudgement, key: str
) -> FeedbackReviewReceipt:
    pair = await locked_review_pair(session, principal, identity)
    reviewer = actor(principal)
    if not 1 <= len(key) <= 128:
        raise FeedbackConflict("Review request identity is out of bounds")
    intent = hashlib.sha256(
        canonical_bytes({"pair_id": identity, "body": body.model_dump(mode="json")})
    ).hexdigest()
    command = await session.scalar(
        select(FeedbackMutationRow).where(
            FeedbackMutationRow.actor_user_id == reviewer,
            FeedbackMutationRow.operation == "review",
            FeedbackMutationRow.request_id == key,
        )
    )
    if command is not None:
        if command.intent_sha256 != intent:
            raise FeedbackConflict("Review request identity was reused with different input")
        return FeedbackReviewReceipt.model_validate(command.receipt)
    label = await session.scalar(
        select(FeedbackRow)
        .where(FeedbackRow.pair_id == pair.id, FeedbackRow.source == "admin")
        .with_for_update()
    )
    if body.expected_version != (label.version if label is not None else 0):
        raise FeedbackConflict("Admin judgement changed; reload before deciding")
    if body.choice == "skip":
        if await session.get(FeedbackReviewSkipRow, (reviewer, pair.id)) is None:
            session.add(FeedbackReviewSkipRow(actor_user_id=reviewer, pair_id=pair.id))
    else:
        await session.execute(
            delete(FeedbackReviewSkipRow).where(
                FeedbackReviewSkipRow.actor_user_id == reviewer,
                FeedbackReviewSkipRow.pair_id == pair.id,
            )
        )
        if label is None:
            label = FeedbackRow(
                owner_user_id=pair.owner_user_id,
                actor_user_id=reviewer,
                conversation_id=pair.conversation_id,
                pair_id=pair.id,
                kind="pair",
                source="admin",
                judgement=body.choice,
                tags=list(body.tags),
                capture_generation=pair.capture_generation,
                version=1,
            )
            session.add(label)
        else:
            label.actor_user_id = reviewer
            label.judgement = body.choice
            label.tags = list(body.tags)
            label.version += 1
            label.updated_at = datetime.now(UTC)
    await write_principal_audit(
        session,
        principal=principal,
        action="feedback.review",
        target_type="comparison_pair",
        target_id=pair.id,
        context={"choice": body.choice, "version": label.version if label else 0},
    )
    await session.flush()
    receipt = FeedbackReviewReceipt(
        comparison_id=pair.id,
        judgement=judgement_receipt(label) if label else None,
        skipped=body.choice == "skip",
    )
    session.add(
        FeedbackMutationRow(
            actor_user_id=reviewer,
            operation="review",
            request_id=key,
            intent_sha256=intent,
            receipt=receipt.model_dump(mode="json"),
        )
    )
    await session.flush()
    return receipt
