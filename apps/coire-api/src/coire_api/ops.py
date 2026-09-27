"""Durable ops conversations and atomic human-confirmation state machine."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta

from opentelemetry import metrics, trace
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.audit import write_audit, write_principal_audit
from coire_api.auth import Principal
from coire_api.db import (
    OpsConfirmationTokenRow,
    OpsConversationRow,
    OpsMessageRow,
    OpsProposalRow,
    OpsSessionRow,
)
from coire_api.ops_tokens import (
    InvalidConfirmation,
    canonical_action_digest,
    hash_secret,
    parse_token,
    token_material,
    verify_secret,
)
from coire_core.models.audit import AuditOutcome
from coire_core.models.auth import ActorType
from coire_core.models.ops import (
    OpsConversation,
    OpsConversationDetail,
    OpsConversationState,
    OpsMessage,
    OpsMessageRole,
    OpsProposal,
    OpsProposalIssued,
    OpsProposalState,
    OpsProposalSubmission,
    OpsSession,
    OpsSessionRegistration,
    OpsSessionState,
    ResolvedOpsAction,
    resolved_ops_action_adapter,
)


class OpsNotFound(LookupError):
    pass


logger = logging.getLogger(__name__)
tracer = trace.get_tracer("coire.api.ops")
meter = metrics.get_meter("coire.api.ops")
proposal_events = meter.create_counter("coire_ops_proposals_total", unit="1")
confirmation_events = meter.create_counter("coire_ops_confirmations_total", unit="1")


def record_confirmation_refusal(*, proposal_id: uuid.UUID, reason: str) -> None:
    """Record only the reviewed bounded reason vocabulary; never token/model content."""

    bounded = reason if reason in InvalidConfirmation.ALLOWED_REASONS else "unknown"
    confirmation_events.add(1, {"outcome": "refused", "reason": bounded})
    logger.warning(
        "ops confirmation refused",
        extra={"proposal_id": str(proposal_id), "reason": bounded},
    )


def project_session(row: OpsSessionRow) -> OpsSession:
    return OpsSession(
        id=row.id,
        service_instance=row.service_instance,
        state=row.state,
        started_at=row.started_at,
        last_seen_at=row.last_seen_at,
        ended_at=row.ended_at,
    )


def project_conversation(row: OpsConversationRow) -> OpsConversation:
    return OpsConversation(
        id=row.id,
        admin_user_id=row.admin_user_id,
        ops_session_id=row.ops_session_id,
        state=row.state,
        degraded=row.degraded,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def project_message(row: OpsMessageRow) -> OpsMessage:
    return OpsMessage(
        id=row.id,
        conversation_id=row.conversation_id,
        role=row.role,
        content=row.content,
        degraded=row.degraded,
        created_at=row.created_at,
    )


def project_proposal(row: OpsProposalRow) -> OpsProposal:
    return OpsProposal(
        id=row.id,
        conversation_id=row.conversation_id,
        ops_session_id=row.ops_session_id,
        proposer=row.proposer,
        action=resolved_ops_action_adapter.validate_python(row.action),
        rationale=row.rationale,
        state=row.state,
        created_at=row.created_at,
        expires_at=row.expires_at,
        decided_at=row.decided_at,
        executed_at=row.executed_at,
        confirmed_by_user_id=row.confirmed_by_user_id,
        result=row.result,
        failure_code=row.failure_code,
    )


async def register_session(
    session: AsyncSession, registration: OpsSessionRegistration
) -> OpsSessionRow:
    now = datetime.now(UTC)
    active = list(
        (
            await session.scalars(
                select(OpsSessionRow)
                .where(OpsSessionRow.state == OpsSessionState.ACTIVE)
                .with_for_update()
            )
        ).all()
    )
    for previous in active:
        if previous.id == registration.session_id:
            previous.last_seen_at = now
            return previous
        previous.state = OpsSessionState.SUPERSEDED
        previous.ended_at = now
        pending = list(
            (
                await session.scalars(
                    select(OpsProposalRow)
                    .where(
                        OpsProposalRow.ops_session_id == previous.id,
                        OpsProposalRow.state == OpsProposalState.PENDING,
                    )
                    .with_for_update()
                )
            ).all()
        )
        for proposal in pending:
            proposal.state = OpsProposalState.EXPIRED
            proposal.decided_at = now
            token = await session.scalar(
                select(OpsConfirmationTokenRow)
                .where(OpsConfirmationTokenRow.proposal_id == proposal.id)
                .with_for_update()
            )
            if token is not None:
                token.revoked_at = now
    row = OpsSessionRow(
        id=registration.session_id,
        service_instance=registration.service_instance,
        state=OpsSessionState.ACTIVE,
        started_at=now,
        last_seen_at=now,
    )
    session.add(row)
    await session.flush()
    await write_audit(
        session,
        actor=f"coire-ops:{row.id}",
        actor_type=ActorType.SERVICE,
        action="ops.session.register",
        target_type="ops_session",
        target_id=str(row.id),
        detail={"invalidated_sessions": len(active)},
    )
    return row


async def heartbeat_session(
    session: AsyncSession, session_id: uuid.UUID, *, stale_seconds: float
) -> OpsSessionRow:
    row = await session.scalar(
        select(OpsSessionRow).where(OpsSessionRow.id == session_id).with_for_update()
    )
    if row is None or row.state is not OpsSessionState.ACTIVE:
        raise OpsNotFound("active ops session not found")
    if row.last_seen_at <= datetime.now(UTC) - timedelta(seconds=stale_seconds):
        await current_session(session, stale_seconds=stale_seconds)
        raise OpsNotFound("active ops session is stale")
    row.last_seen_at = datetime.now(UTC)
    await session.flush()
    return row


async def current_session(session: AsyncSession, *, stale_seconds: float) -> OpsSessionRow | None:
    row = await session.scalar(
        select(OpsSessionRow)
        .where(OpsSessionRow.state == OpsSessionState.ACTIVE)
        .order_by(OpsSessionRow.started_at.desc())
        .limit(1)
    )
    if not isinstance(row, OpsSessionRow):
        return None
    now = datetime.now(UTC)
    if row.last_seen_at > now - timedelta(seconds=stale_seconds):
        return row
    row.state = OpsSessionState.SUPERSEDED
    row.ended_at = now
    pending = list(
        (
            await session.scalars(
                select(OpsProposalRow)
                .where(
                    OpsProposalRow.ops_session_id == row.id,
                    OpsProposalRow.state == OpsProposalState.PENDING,
                )
                .with_for_update()
            )
        ).all()
    )
    for proposal in pending:
        proposal.state = OpsProposalState.EXPIRED
        proposal.decided_at = now
        token = await session.scalar(
            select(OpsConfirmationTokenRow)
            .where(OpsConfirmationTokenRow.proposal_id == proposal.id)
            .with_for_update()
        )
        if token is not None:
            token.revoked_at = now
    await session.flush()
    return None


async def create_conversation(
    session: AsyncSession, *, admin_user_id: uuid.UUID, stale_seconds: float
) -> OpsConversationRow:
    active = await current_session(session, stale_seconds=stale_seconds)
    row = OpsConversationRow(
        admin_user_id=admin_user_id,
        ops_session_id=active.id if active else None,
        state=OpsConversationState.ACTIVE,
        degraded=active is None,
    )
    session.add(row)
    await session.flush()
    return row


async def get_conversation(session: AsyncSession, conversation_id: uuid.UUID) -> OpsConversationRow:
    row = await session.get(OpsConversationRow, conversation_id)
    if row is None:
        raise OpsNotFound("ops conversation not found")
    return row


async def conversation_detail(
    session: AsyncSession, conversation_id: uuid.UUID
) -> OpsConversationDetail:
    conversation = await get_conversation(session, conversation_id)
    messages = list(
        (
            await session.scalars(
                select(OpsMessageRow)
                .where(OpsMessageRow.conversation_id == conversation_id)
                .order_by(OpsMessageRow.created_at, OpsMessageRow.id)
            )
        ).all()
    )
    proposals = list(
        (
            await session.scalars(
                select(OpsProposalRow)
                .where(OpsProposalRow.conversation_id == conversation_id)
                .order_by(OpsProposalRow.created_at, OpsProposalRow.id)
            )
        ).all()
    )
    return OpsConversationDetail(
        conversation=project_conversation(conversation),
        messages=[project_message(row) for row in messages],
        proposals=[project_proposal(row) for row in proposals],
    )


async def append_message(
    session: AsyncSession,
    *,
    conversation_id: uuid.UUID,
    role: OpsMessageRole,
    content: str,
    degraded: bool = False,
) -> OpsMessageRow:
    await get_conversation(session, conversation_id)
    row = OpsMessageRow(
        conversation_id=conversation_id,
        role=role,
        content=content,
        degraded=degraded,
    )
    session.add(row)
    await session.flush()
    return row


async def create_proposal(
    session: AsyncSession,
    submission: OpsProposalSubmission,
    *,
    ttl_seconds: int,
    stale_seconds: float,
) -> OpsProposalIssued:
    conversation = await get_conversation(session, submission.conversation_id)
    ops_session = await session.scalar(
        select(OpsSessionRow).where(OpsSessionRow.id == submission.session_id).with_for_update()
    )
    if ops_session is None or ops_session.state is not OpsSessionState.ACTIVE:
        raise InvalidConfirmation("session_restarted")
    if ops_session.last_seen_at <= datetime.now(UTC) - timedelta(seconds=stale_seconds):
        await current_session(session, stale_seconds=stale_seconds)
        raise InvalidConfirmation("session_restarted")
    if conversation.ops_session_id not in {None, ops_session.id}:
        raise InvalidConfirmation("session_restarted")
    conversation.ops_session_id = ops_session.id
    conversation.degraded = False
    conversation.updated_at = datetime.now(UTC)
    proposal_id = uuid.uuid4()
    digest = canonical_action_digest(
        proposal_id=proposal_id,
        conversation_id=conversation.id,
        session_id=ops_session.id,
        action=submission.action,
    )
    now = datetime.now(UTC)
    expires_at = now + timedelta(seconds=ttl_seconds)
    row = OpsProposalRow(
        id=proposal_id,
        conversation_id=conversation.id,
        ops_session_id=ops_session.id,
        proposer=f"coire-ops:{ops_session.id}",
        action=submission.action.model_dump(mode="json"),
        action_digest=digest,
        rationale=submission.rationale,
        state=OpsProposalState.PENDING,
        created_at=now,
        expires_at=expires_at,
    )
    prefix, secret, presented = token_material()
    token = OpsConfirmationTokenRow(
        proposal_id=proposal_id,
        prefix=prefix,
        secret_hash=hash_secret(secret),
        action_digest=digest,
        issued_to_user_id=conversation.admin_user_id,
        expires_at=expires_at,
    )
    session.add(row)
    await session.flush()
    session.add(token)
    await session.flush()
    await write_audit(
        session,
        actor=row.proposer,
        actor_type=ActorType.SERVICE,
        action="ops.proposal.create",
        target_type="ops_proposal",
        target_id=str(row.id),
        detail={"operation": submission.action.operation},
    )
    proposal_events.add(1, {"operation": submission.action.operation, "outcome": "created"})
    logger.info(
        "ops proposal created",
        extra={
            "proposal_id": str(row.id),
            "conversation_id": str(row.conversation_id),
            "operation": submission.action.operation,
        },
    )
    return OpsProposalIssued(proposal=project_proposal(row), confirm_token=presented)


async def consume_confirmation(
    session: AsyncSession,
    *,
    proposal_id: uuid.UUID,
    presented_token: str,
    presented_action: ResolvedOpsAction,
    principal: Principal,
    stale_seconds: float,
) -> OpsProposalRow:
    prefix, secret = parse_token(presented_token)
    token = await session.scalar(
        select(OpsConfirmationTokenRow)
        .where(OpsConfirmationTokenRow.prefix == prefix)
        .with_for_update()
    )
    if token is None or token.proposal_id != proposal_id:
        raise InvalidConfirmation("unknown")
    proposal = await session.scalar(
        select(OpsProposalRow).where(OpsProposalRow.id == proposal_id).with_for_update()
    )
    if proposal is None:
        raise InvalidConfirmation("unknown")
    if not verify_secret(token.secret_hash, secret):
        raise InvalidConfirmation("secret_mismatch")
    now = datetime.now(UTC)
    if token.used_at is not None:
        raise InvalidConfirmation("used")
    if token.revoked_at is not None:
        raise InvalidConfirmation("revoked")
    if token.expires_at <= now or proposal.expires_at <= now:
        proposal.state = OpsProposalState.EXPIRED
        proposal.decided_at = now
        raise InvalidConfirmation("expired")
    if proposal.state is not OpsProposalState.PENDING:
        raise InvalidConfirmation("not_pending")
    active_session = await current_session(session, stale_seconds=stale_seconds)
    if active_session is None or active_session.id != proposal.ops_session_id:
        raise InvalidConfirmation("session_restarted")
    digest = canonical_action_digest(
        proposal_id=proposal.id,
        conversation_id=proposal.conversation_id,
        session_id=proposal.ops_session_id,
        action=presented_action,
    )
    if digest != token.action_digest or digest != proposal.action_digest:
        raise InvalidConfirmation("action_mismatch")
    token.used_at = now
    proposal.state = OpsProposalState.CONFIRMED
    proposal.decided_at = now
    proposal.confirmed_by_user_id = principal.user_id
    await write_principal_audit(
        session,
        principal=principal,
        action="ops.proposal.confirm",
        target_type="ops_proposal",
        target_id=str(proposal.id),
        detail={"proposer": proposal.proposer, "operation": presented_action.operation},
    )
    await session.flush()
    confirmation_events.add(1, {"outcome": "accepted", "reason": "none"})
    logger.info(
        "ops confirmation accepted",
        extra={"proposal_id": str(proposal.id), "user_id": str(principal.user_id)},
    )
    return proposal


async def decline_proposal(
    session: AsyncSession,
    *,
    proposal_id: uuid.UUID,
    principal: Principal,
    reason: str | None,
) -> OpsProposalRow:
    row = await session.scalar(
        select(OpsProposalRow).where(OpsProposalRow.id == proposal_id).with_for_update()
    )
    if row is None:
        raise OpsNotFound("ops proposal not found")
    if row.state is not OpsProposalState.PENDING:
        raise InvalidConfirmation("not_pending")
    now = datetime.now(UTC)
    row.state = OpsProposalState.DECLINED
    row.decided_at = now
    token = await session.scalar(
        select(OpsConfirmationTokenRow)
        .where(OpsConfirmationTokenRow.proposal_id == row.id)
        .with_for_update()
    )
    if token is not None:
        token.revoked_at = now
    await write_principal_audit(
        session,
        principal=principal,
        action="ops.proposal.decline",
        target_type="ops_proposal",
        target_id=str(row.id),
        outcome=AuditOutcome.OK,
        detail={"proposer": row.proposer, "reason": reason or "declined"},
    )
    await session.flush()
    return row


async def execute_confirmed_proposal(
    session: AsyncSession,
    *,
    proposal_id: uuid.UUID,
    principal: Principal,
    settings: object,
) -> OpsProposalRow:
    """Dispatch one already-consumed proposal through the fixed domain registry."""
    from coire_api.ops_actions import OpsActionError, execute_action
    from coire_core.settings import Settings

    if not isinstance(settings, Settings):
        raise TypeError("settings must be Settings")
    with tracer.start_as_current_span("coire.api.ops.execute") as span:
        span.set_attribute("proposal_id", str(proposal_id))
        row = await session.scalar(
            select(OpsProposalRow).where(OpsProposalRow.id == proposal_id).with_for_update()
        )
    if row is None:
        raise OpsNotFound("ops proposal not found")
    if row.state is not OpsProposalState.CONFIRMED:
        raise InvalidConfirmation("not_pending")
    row.state = OpsProposalState.EXECUTING
    await session.flush()
    action = resolved_ops_action_adapter.validate_python(row.action)
    try:
        result = await execute_action(session, action, principal, settings)
    except OpsActionError as exc:
        row.state = OpsProposalState.STALE if exc.stale else OpsProposalState.FAILED
        row.failure_code = exc.code
        row.result = {"detail": exc.detail}
        await write_principal_audit(
            session,
            principal=principal,
            action="ops.action.refused" if exc.stale else "ops.action.failed",
            target_type=action.target_type,
            target_id=str(action.target_id),
            outcome=AuditOutcome.REFUSED if exc.stale else AuditOutcome.ERROR,
            detail={"code": exc.code, "proposer": row.proposer},
        )
    else:
        row.state = OpsProposalState.EXECUTED
        row.result = result
        row.executed_at = datetime.now(UTC)
        await write_principal_audit(
            session,
            principal=principal,
            action="ops.action.executed",
            target_type=action.target_type,
            target_id=str(action.target_id),
            detail={
                "operation": action.operation,
                "proposal_id": str(row.id),
                "proposer": row.proposer,
            },
        )
    await session.flush()
    return row
