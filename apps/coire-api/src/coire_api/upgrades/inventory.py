"""Read-only inventories retain private erasure and renewal obligations during drain."""

import asyncio
from datetime import UTC, datetime
from typing import Any, Literal

import httpx
from opentelemetry import trace
from pydantic import ValidationError
from sqlalchemy import Select, and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.db import (
    ChatAttachmentRow,
    ChatConversationRow,
    ChatFeedbackProvenanceRow,
    ChatFileProcessingRow,
    ComparisonPairRow,
    FeedbackRow,
    ImageExecutionLeaseRow,
    ImageInputRow,
    PreferenceExportRow,
    RequestLeaseRow,
    TrainingParticipantRow,
)
from coire_api.upgrades.admission import CoreAdmissions
from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrades import (
    COHORT_ORDER,
    CoreUpgradeInventory,
    UpgradeExecutionPlan,
    UpgradeInventoryEntry,
    UpgradeKind,
    UpgradeMaintenance,
    UpgradeOperation,
    UpgradeOwnerInventory,
    core_upgrade_required_owners,
)
from coire_core.settings import Settings


async def _owner(
    session: AsyncSession,
    owner: str,
    queries: tuple[tuple[str, str, Select[Any]], ...],
) -> UpgradeOwnerInventory:
    entries: list[UpgradeInventoryEntry] = []
    for kind, state, query in queries:
        identities = (await session.scalars(query.limit(4097))).all()
        if len(identities) + len(entries) > 4096:
            raise UpgradeUnavailable("Strict Core inventory exceeds its bound")
        for identity in identities:
            entries.append(
                UpgradeInventoryEntry.model_validate(
                    {
                        "owner": owner,
                        "kind": kind,
                        "work_id": str(identity),
                        "state": state,
                    }
                )
            )
    return UpgradeOwnerInventory(owner=owner, status="complete", entries=tuple(entries))


async def privacy_inventory(session: AsyncSession) -> tuple[UpgradeOwnerInventory, ...]:
    """Neither expired leases nor terminal work labels erase outstanding ownership."""
    now = datetime.now(UTC)
    feedback = await _owner(
        session,
        "feedback",
        (
            (
                "export",
                "active",
                select(PreferenceExportRow.id).where(
                    or_(
                        PreferenceExportRow.state.not_in(
                            ("queued", "succeeded", "failed", "cancelled")
                        ),
                        PreferenceExportRow.cleanup_pending.is_(True),
                    )
                ),
            ),
            (
                "comparison",
                "active",
                select(ComparisonPairRow.id).where(
                    ComparisonPairRow.generation_state.not_in(
                        (
                            "queued",
                            "ready",
                            "failed",
                            "cancelled",
                            "identical",
                            "expired",
                            "withdrawn",
                        )
                    )
                ),
            ),
            (
                "comparison-purge",
                "cleaning",
                select(ComparisonPairRow.id).where(
                    ComparisonPairRow.withdrawn_at.is_not(None),
                    ComparisonPairRow.purged_at.is_(None),
                ),
            ),
            (
                "provenance-purge",
                "cleaning",
                select(ChatFeedbackProvenanceRow.id).where(
                    ChatFeedbackProvenanceRow.withdrawn_at.is_not(None),
                    ChatFeedbackProvenanceRow.purged_at.is_(None),
                ),
            ),
            (
                "feedback-purge",
                "cleaning",
                select(FeedbackRow.id).where(
                    FeedbackRow.withdrawn_at.is_not(None), FeedbackRow.purged_at.is_(None)
                ),
            ),
        ),
    )
    files = await _owner(
        session,
        "files",
        (
            (
                "file-processing",
                "cleaning",
                select(ChatFileProcessingRow.id).where(
                    or_(
                        ChatFileProcessingRow.state.not_in(
                            ("queued", "ready", "failed", "cancelled", "purged")
                        ),
                        and_(
                            ChatFileProcessingRow.expires_at <= now,
                            ChatFileProcessingRow.state != "purged",
                        ),
                        and_(
                            ChatFileProcessingRow.state == "failed",
                            or_(
                                ChatFileProcessingRow.output_manifest.is_(None),
                                ChatFileProcessingRow.output_manifest["output_purged"]
                                .as_boolean()
                                .is_distinct_from(True),
                            ),
                        ),
                    )
                ),
            ),
            (
                "image-input",
                "cleaning",
                select(ImageInputRow.id).where(
                    or_(
                        ImageInputRow.state.not_in(("ready", "failed", "deleted", "purged")),
                        and_(
                            ImageInputRow.deleted_at.is_not(None), ImageInputRow.purged_at.is_(None)
                        ),
                    )
                ),
            ),
            (
                "conversation-erasure",
                "cleaning",
                select(ChatConversationRow.id).where(
                    ChatConversationRow.deleted_at.is_not(None),
                    ChatConversationRow.purged_at.is_(None),
                ),
            ),
            (
                "attachment-erasure",
                "cleaning",
                select(ChatAttachmentRow.id)
                .join(
                    ChatConversationRow, ChatConversationRow.id == ChatAttachmentRow.conversation_id
                )
                .where(
                    or_(
                        ChatAttachmentRow.deleted_at.is_not(None),
                        ChatConversationRow.deleted_at.is_not(None),
                    )
                ),
            ),
        ),
    )
    renewals = await _owner(
        session,
        "renewals",
        (
            (
                "inference-lease",
                "active",
                select(RequestLeaseRow.id).where(RequestLeaseRow.released_at.is_(None)),
            ),
            (
                "image-lease",
                "active",
                select(ImageExecutionLeaseRow.id).where(
                    ImageExecutionLeaseRow.released_at.is_(None)
                ),
            ),
            (
                "trainer",
                "active",
                select(TrainingParticipantRow.id).where(
                    TrainingParticipantRow.stopped_at.is_(None)
                ),
            ),
        ),
    )
    return feedback, files, renewals


async def job_inventory(session: AsyncSession) -> tuple[UpgradeOwnerInventory, ...]:
    """Durable active work and unproven cleanup, excluding immutable published data."""
    from coire_api import db
    from coire_core.models.acquisition import AcquisitionState
    from coire_core.models.images import ImageJobState
    from coire_core.models.instance import InstanceState
    from coire_core.models.mcp import McpCallState
    from coire_core.models.ops import OpsProposalState
    from coire_core.models.runs import (
        TERMINAL_RUN_STATES,
        AgentRunState,
        RunCommandState,
        RunOperation,
    )
    from coire_core.models.sharding import BenchmarkRunState

    # A pending renewal cannot execute against a terminal job and positively
    # stopped participants. Keep its immutable history, but do not mistake it
    # for an outstanding renewal obligation. Uncertain current dispatches block.
    obsolete_renewal = and_(
        db.TrainingCommandRow.operation == "node.training.lease",
        db.TrainingCommandRow.state == "pending",
        select(db.TrainingJobRow.id)
        .where(
            db.TrainingJobRow.id == db.TrainingCommandRow.job_id,
            db.TrainingJobRow.state.in_(("succeeded", "failed", "cancelled")),
        )
        .exists(),
        select(db.TrainingAttemptRow.id)
        .where(
            db.TrainingAttemptRow.id == db.TrainingCommandRow.attempt_id,
            db.TrainingAttemptRow.state == "stopped",
            db.TrainingAttemptRow.stopped_at.is_not(None),
        )
        .exists(),
        ~select(db.TrainingParticipantRow.id)
        .where(
            db.TrainingParticipantRow.attempt_id == db.TrainingCommandRow.attempt_id,
            db.TrainingParticipantRow.stopped_at.is_(None),
        )
        .exists(),
    )
    obsolete_import = and_(
        db.TrainingCommandRow.operation == "node.artifact.import",
        db.TrainingCommandRow.state == "dispatching",
        select(db.TrainingAttemptRow.id)
        .join(db.TrainingJobRow, db.TrainingJobRow.id == db.TrainingAttemptRow.job_id)
        .where(
            db.TrainingAttemptRow.id == db.TrainingCommandRow.attempt_id,
            db.TrainingJobRow.id == db.TrainingCommandRow.job_id,
            db.TrainingJobRow.state.in_(("succeeded", "failed", "cancelled")),
            db.TrainingAttemptRow.fence < db.TrainingJobRow.fence,
            db.TrainingAttemptRow.state == "stopped",
            db.TrainingAttemptRow.stopped_at.is_not(None),
        )
        .exists(),
        ~select(db.TrainingParticipantRow.id)
        .where(
            db.TrainingParticipantRow.attempt_id == db.TrainingCommandRow.attempt_id,
            db.TrainingParticipantRow.stopped_at.is_(None),
        )
        .exists(),
    )

    scheduler = await _owner(
        session,
        "scheduler",
        (
            (
                "acquisition",
                "active",
                select(db.AcquisitionWorkflowRow.id).where(
                    db.AcquisitionWorkflowRow.state.not_in(
                        (
                            AcquisitionState.QUEUED,
                            AcquisitionState.SUCCEEDED,
                            AcquisitionState.FAILED,
                            AcquisitionState.CANCELLED,
                        )
                    )
                ),
            ),
            (
                "acquisition-command",
                "active",
                select(db.AcquisitionCommandRow.id).where(
                    db.AcquisitionCommandRow.state.in_(("pending", "running"))
                ),
            ),
            (
                "instance-transition",
                "active",
                select(db.ModelInstanceRow.id).where(
                    db.ModelInstanceRow.state.not_in(
                        (
                            InstanceState.REQUESTED,
                            InstanceState.READY,
                            InstanceState.STOPPED,
                            InstanceState.FAILED,
                        )
                    )
                ),
            ),
            (
                "training",
                "active",
                select(db.TrainingJobRow.id).where(
                    db.TrainingJobRow.state.not_in(
                        ("queued", "paused", "succeeded", "failed", "cancelled")
                    )
                ),
            ),
            (
                "training-command",
                "active",
                select(db.TrainingCommandRow.id).where(
                    db.TrainingCommandRow.state.in_(("pending", "running", "dispatching")),
                    ~or_(obsolete_renewal, obsolete_import),
                ),
            ),
            (
                "analysis",
                "active",
                select(db.TrainingDatasetAnalysisRow.id).where(
                    db.TrainingDatasetAnalysisRow.state.not_in(
                        ("queued", "succeeded", "failed", "cancelled")
                    )
                ),
            ),
            (
                "evaluation",
                "active",
                select(db.EvaluationRunRow.id).where(
                    db.EvaluationRunRow.state.not_in(
                        ("queued", "succeeded", "failed", "timed_out", "cancelled")
                    )
                ),
            ),
            (
                "image",
                "active",
                select(db.ImageJobRow.id).where(
                    db.ImageJobRow.state.not_in(
                        (
                            ImageJobState.QUEUED,
                            ImageJobState.SUCCEEDED,
                            ImageJobState.FAILED,
                            ImageJobState.CANCELLED,
                        )
                    )
                ),
            ),
            (
                "benchmark",
                "active",
                select(db.BenchmarkRunRow.id).where(
                    db.BenchmarkRunRow.state.not_in(
                        (
                            BenchmarkRunState.QUEUED,
                            BenchmarkRunState.COMPLETED,
                            BenchmarkRunState.FAILED,
                        )
                    )
                ),
            ),
        ),
    )
    removed = (
        select(db.RunCommandRow.id)
        .where(
            db.RunCommandRow.run_id == db.AgentRunRow.id,
            # KILL returns only after the node removes the runner, relay and
            # private network, through the same cleanup path as REMOVE.
            db.RunCommandRow.operation.in_((RunOperation.REMOVE, RunOperation.KILL)),
            db.RunCommandRow.state == RunCommandState.SUCCEEDED,
        )
        .exists()
    )
    runs = await _owner(
        session,
        "runs",
        (
            (
                "run",
                "active",
                select(db.AgentRunRow.id).where(
                    db.AgentRunRow.state.not_in((AgentRunState.QUEUED, *TERMINAL_RUN_STATES))
                ),
            ),
            (
                "run-cleanup",
                "cleaning",
                select(db.AgentRunRow.id).where(
                    db.AgentRunRow.state.in_(TERMINAL_RUN_STATES),
                    db.AgentRunRow.container_id.is_not(None),
                    ~removed,
                ),
            ),
            (
                "run-command",
                "active",
                select(db.RunCommandRow.id).where(
                    db.RunCommandRow.state.in_((RunCommandState.PENDING, RunCommandState.RUNNING))
                ),
            ),
        ),
    )
    terminal_mcp = (
        McpCallState.SUCCEEDED,
        McpCallState.FAILED,
        McpCallState.TIMED_OUT,
        McpCallState.CANCELLED,
    )
    mcp = await _owner(
        session,
        "mcp",
        (
            (
                "call",
                "active",
                select(db.McpCallRow.id).where(db.McpCallRow.state.not_in(terminal_mcp)),
            ),
            (
                "workspace-cleanup",
                "cleaning",
                select(db.McpCallRow.id).where(
                    db.McpCallRow.state.in_(terminal_mcp),
                    db.McpCallRow.workspace_cleaned_at.is_(None),
                ),
            ),
        ),
    )
    ops = await _owner(
        session,
        "ops",
        (
            (
                "confirmed-action",
                "active",
                select(db.OpsProposalRow.id).where(
                    db.OpsProposalRow.state.in_(
                        (OpsProposalState.CONFIRMED, OpsProposalState.EXECUTING)
                    )
                ),
            ),
        ),
    )
    return scheduler, runs, mcp, ops


def failover_inventory(settings: Settings) -> UpgradeOwnerInventory:
    """Observe the existing election; never campaign, fence or demote from inventory."""
    from coire_api.routes.failover import get_participant
    from coire_core.failover_election import ServiceRole

    if not settings.failover_peer_key.get_secret_value().strip():
        return UpgradeOwnerInventory(owner="failover", status="complete")
    participant = get_participant()
    if participant is None or settings.failover_member_name != "coire-core":
        return UpgradeOwnerInventory(
            owner="failover", status="unknown", reason="core_election_unavailable"
        )
    now = datetime.now(UTC)
    if participant.role is ServiceRole.ELECTED and not participant.frontend_ready(now):
        return UpgradeOwnerInventory(
            owner="failover", status="unknown", reason="core_lease_uncertain"
        )
    active = participant.in_flight > 0 or participant.role in {
        ServiceRole.CANDIDATE,
        ServiceRole.DRAINING,
    }
    return UpgradeOwnerInventory(
        owner="failover",
        status="complete",
        entries=(
            UpgradeInventoryEntry(
                owner="failover",
                kind="core-election",
                work_id="coire-core",
                state="active" if active else "standing",
            ),
        ),
    )


def core_required_owners(plan: UpgradeExecutionPlan) -> frozenset[str]:
    if plan.operation.kind is not UpgradeKind.CONTROL_PLANE:
        raise UpgradeConflict("Core inventory requires a Core execution plan")
    return core_upgrade_required_owners(plan)


async def require_inventory_authority(session: AsyncSession, plan: UpgradeExecutionPlan) -> None:
    from coire_api.db import UpgradeMaintenanceRow
    from coire_api.upgrades.persistence import get_operation

    row = await get_operation(session, plan.operation.job_id)
    operation = UpgradeOperation.model_validate(row.payload)
    if (
        row.execution_plan is None
        or operation.authority_sha256() != plan.operation.authority_sha256()
        or UpgradeExecutionPlan.model_validate(row.execution_plan).authority_sha256()
        != plan.authority_sha256()
    ):
        raise UpgradeConflict("Core inventory authority differs")
    barriers = (
        await session.scalars(
            select(UpgradeMaintenanceRow).where(
                UpgradeMaintenanceRow.target.in_(plan.maintenance.targets),
            )
        )
    ).all()
    values = []
    for barrier in barriers:
        if barrier.generation > operation.generation:
            raise UpgradeConflict("Core inventory maintenance owner is stale")
        if (barrier.job_id, barrier.generation) != (operation.job_id, operation.generation):
            if not barrier.released:
                raise UpgradeConflict("Another operation retains Core inventory maintenance")
            continue
        if barrier.released:
            raise UpgradeConflict("Core inventory maintenance was already released")
        values.append(barrier.payload)
    observed: set[str] = set()
    for payload in values:
        maintenance = UpgradeMaintenance.model_validate(payload)
        if (
            maintenance.job_id != operation.job_id
            or maintenance.generation != operation.generation
            or set(maintenance.targets) != set(plan.maintenance.targets)
            or maintenance.state == "released"
            or maintenance.drain_deadline_at != plan.maintenance.drain_deadline_at
            or maintenance.initiator_user_id != plan.maintenance.initiator_user_id
        ):
            raise UpgradeConflict("Core inventory maintenance changed")
        observed.update(maintenance.targets)
    # One payload per affected target is required; a single row listing the whole
    # scope is not sufficient evidence that all barriers were persisted.
    if len(values) != len(plan.maintenance.targets) or observed != set(plan.maintenance.targets):
        # The independent executor can reach preflight before the scheduler
        # persists every barrier. This remains a refusal, but permits retry
        # within the operation's original drain budget without starting effects.
        raise UpgradeUnavailable("Core inventory maintenance is incomplete")


async def core_inventory(
    session: AsyncSession,
    settings: Settings,
    plan: UpgradeExecutionPlan,
    admissions: CoreAdmissions,
) -> CoreUpgradeInventory:
    """Collect only affected owners, retaining uncertainty across external reads."""
    required = core_required_owners(plan)
    if (
        not settings.upgrades_enabled
        or not admissions.enabled
        or settings.upgrade_project != plan.source.manifest.project
    ):
        raise UpgradeUnavailable("Managed Core inventory is not configured")
    await require_inventory_authority(session, plan)
    request_scope = COHORT_ORDER if "core" in plan.maintenance.targets else plan.operation.cohort
    inventories = [await admissions.inventory(request_scope)]
    if required.intersection({"scheduler", "runs", "mcp", "ops"}):
        inventories.extend(
            owner for owner in await job_inventory(session) if owner.owner in required
        )
    if required.intersection({"feedback", "files", "renewals"}):
        inventories.extend(
            owner for owner in await privacy_inventory(session) if owner.owner in required
        )
    if "failover" in required:
        inventories.append(failover_inventory(settings))
    services: list[Literal["coire-mcp", "coire-ops", "coire-file-worker"]] = []
    if "mcp" in required and (settings.mcp_enabled or "coire-mcp" in plan.operation.cohort):
        services.append("coire-mcp")
    if "ops" in required and (settings.ops_enabled or "coire-ops" in plan.operation.cohort):
        services.append("coire-ops")
    if "files" in required:
        services.append("coire-file-worker")
    for service in services:
        owner_name = {"coire-mcp": "mcp", "coire-ops": "ops", "coire-file-worker": "files"}[service]
        try:
            inventories.append(await service_inventory(settings, service))
        except UpgradeUnavailable:
            inventories.append(
                UpgradeOwnerInventory(
                    owner=owner_name, status="unknown", reason="service_inventory_unavailable"
                )
            )
    await require_inventory_authority(session, plan)
    merged: list[UpgradeOwnerInventory] = []
    for owner in sorted(required):
        parts = [item for item in inventories if item.owner == owner]
        status: Literal["complete", "blocked", "unknown"] = (
            "unknown"
            if not parts or any(item.status == "unknown" for item in parts)
            else "blocked"
            if any(item.status == "blocked" for item in parts)
            else "complete"
        )
        entries = tuple(entry for part in parts for entry in part.entries)
        if len(entries) > 4096:
            raise UpgradeUnavailable("Strict Core owner inventory exceeds its bound")
        merged.append(
            UpgradeOwnerInventory(
                owner=owner,
                status=status,
                entries=entries,
                reason="owner_inventory_incomplete" if status != "complete" else None,
            )
        )
    return CoreUpgradeInventory(
        job_id=plan.operation.job_id,
        generation=plan.operation.generation,
        project=settings.upgrade_project,
        release_sha256=plan.operation.source_sha256,
        observed_at=datetime.now(UTC),
        owners=tuple(merged),
    )


async def service_inventory(
    settings: Settings,
    service: Literal["coire-mcp", "coire-ops", "coire-file-worker"],
    *,
    client: httpx.AsyncClient | None = None,
) -> UpgradeOwnerInventory:
    """Authenticated bounded reads from the independent service's actual request owner."""
    if service == "coire-mcp":
        owner, url, secret = "mcp", settings.upgrade_mcp_url, settings.upgrade_executor_token
    elif service == "coire-ops":
        owner, url, secret = "ops", settings.ops_service_url, settings.ops_service_token
    elif service == "coire-file-worker":
        owner, url, secret = "files", settings.file_worker_url, settings.file_worker_service_token
    else:
        raise UpgradeUnavailable("Strict service inventory target is unsupported")
    if not secret.get_secret_value():
        raise UpgradeUnavailable("Strict service inventory credential is unavailable")
    owned = client is None
    transport = client or httpx.AsyncClient(timeout=5, follow_redirects=False, trust_env=False)
    with trace.get_tracer("coire.api.upgrades").start_as_current_span(
        "coire.api.upgrade_service_inventory", attributes={"coire.service": service}
    ):
        try:
            async with (
                asyncio.timeout(5),
                transport.stream(
                    "GET",
                    url.rstrip("/") + "/internal/upgrades/inventory",
                    headers={"Authorization": "Bearer " + secret.get_secret_value()},
                    timeout=5,
                    follow_redirects=False,
                ) as response,
            ):
                response.raise_for_status()
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(body) + len(chunk) > 512 * 1024:
                        raise UpgradeUnavailable("Strict service inventory exceeds its bound")
                    body.extend(chunk)
                result = UpgradeOwnerInventory.model_validate_json(bytes(body))
                if result.owner != owner:
                    raise UpgradeUnavailable("Strict service inventory owner differs")
                return result
        except (httpx.HTTPError, ValidationError, TimeoutError):
            raise UpgradeUnavailable("Strict service inventory is unavailable or invalid") from None
        finally:
            if owned:
                await transport.aclose()
