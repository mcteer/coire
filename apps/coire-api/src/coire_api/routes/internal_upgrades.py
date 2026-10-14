"""Executor-only fixed authority, receipt and retained migration-proof transport."""

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, Request, Response
from opentelemetry import trace

from coire_api.db import session_factory, session_scope
from coire_api.images.transfer import require_node_credential
from coire_api.nodes_client import NodeClient
from coire_api.upgrades import executor, grants
from coire_api.upgrades.authorization import UpgradeExecutor
from coire_api.upgrades.peer_inventory import guard_peer_inventory
from coire_api.upgrades.service import ingest_receipt
from coire_core.errors import CoireError, UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrade_collective import NodeUpgradeCollectiveResult
from coire_core.models.upgrade_native_terminal import UpgradeNativeTerminalState
from coire_core.models.upgrade_recovery import (
    UpgradeExpiredRecoveryColdCheck,
    UpgradeExpiredRecoveryColdClaim,
    UpgradeExpiredRecoveryColdHandbackIntent,
    UpgradeExpiredRecoveryColdHandbackReceipt,
    UpgradeExpiredRecoveryColdResolution,
    UpgradeExpiredRecoveryColdResolutionRead,
    UpgradeExpiredRecoveryColdResolutionRequest,
    UpgradeExpiredRecoveryColdResolutionState,
    UpgradeExpiredRecoveryColdSmokeCommand,
    UpgradeExpiredRecoveryColdState,
    UpgradeExpiredRecoveryColdVerification,
    UpgradeExpiredRecoveryColdVerificationRead,
    UpgradeExpiredRecoveryColdVerificationRequest,
    UpgradeExpiredRecoveryConsumptionCheck,
    UpgradeExpiredRecoveryConsumptionDeliveryRead,
    UpgradeExpiredRecoveryConsumptionState,
    UpgradeExpiredRecoveryIntent,
    UpgradeExpiredRecoveryObservation,
    UpgradeExpiredRecoveryOwnerCheck,
    UpgradeExpiredRecoveryOwnerClaim,
    UpgradeExpiredRecoveryOwnerState,
    UpgradeExpiredRecoverySuccessionDeliveryRead,
    UpgradeExpiredRecoverySuccessionDeliveryState,
    UpgradeExpiredRecoverySuccessionObservation,
    UpgradeExpiredRecoverySuccessorCheck,
    UpgradeExpiredRecoverySuccessorClaim,
    UpgradeExpiredRecoverySuccessorState,
    UpgradeExpiredRecoverySuccessorStateCheck,
    UpgradeExpiredRecoverySupportContextRead,
    UpgradeExpiredRecoveryTransferConsumption,
    UpgradeExpiredRecoveryUnstartedObservation,
    UpgradeExpiredRecoveryUnstartedRead,
)
from coire_core.models.upgrade_recovery_continuation import (
    UpgradeColdContinuationBindingRead,
    UpgradeColdContinuationBindingState,
    UpgradeColdContinuationContext,
    UpgradeColdContinuationObservation,
    UpgradeColdContinuationRead,
)
from coire_core.models.upgrade_unclaimed import UpgradeUnclaimedCancellationState
from coire_core.models.upgrades import (
    CoreBootstrapHeartbeat,
    CoreBootstrapSeed,
    CoreHandoverCommitIntent,
    CoreHandoverCommitRequest,
    CoreHandoverCommittedRequest,
    CoreHandoverPreparation,
    CoreHandoverRecoveryRequest,
    CoreHandoverRestoreRequest,
    CoreHandoverSummary,
    CoreHandoverTransferIntent,
    CoreHandoverTransferRequest,
    CoreHandoverUnchangedRequest,
    CoreUpgradeInventory,
    NodeUpgradeCollectiveCleanup,
    NodeUpgradeCollectiveRead,
    NodeUpgradeInventory,
    NodeUpgradePeerRead,
    Sha256,
    StudioName,
    UpgradeActivationConsume,
    UpgradeActivationGrant,
    UpgradeActivationRequest,
    UpgradeClaimIdle,
    UpgradeClaimRequest,
    UpgradeExecutionPlan,
    UpgradeId,
    UpgradeMigrationExecution,
    UpgradeMigrationNotStarted,
    UpgradeOperation,
    UpgradeReceiptAck,
    UpgradeReceiptBatch,
    UpgradeSchemaObservation,
    UpgradeSmokeRequest,
    UpgradeSmokeResult,
)

router = APIRouter(prefix="/api/v1/internal/upgrades", tags=["internal:upgrades"])
tracer = trace.get_tracer("coire.api.upgrades.executor")


def enabled(request: Request, response: Response) -> None:
    if not request.app.state.settings.upgrades_enabled:
        raise UpgradeUnavailable("Managed upgrades are disabled")
    response.headers["Cache-Control"] = "no-store"


@router.get("/bootstrap/handover/{preparation_sha256}", response_model=CoreHandoverSummary)
async def handover_status(
    preparation_sha256: Sha256,
    request: Request,
    response: Response,
    principal: UpgradeExecutor,
) -> CoreHandoverSummary:
    from sqlalchemy import select

    from coire_api.db import UpgradeHandoverRow
    from coire_api.upgrades.handover_authority import handover_summary

    enabled(request, response)
    async with session_scope() as session:
        row = await session.scalar(
            select(UpgradeHandoverRow).where(
                UpgradeHandoverRow.project == request.app.state.settings.upgrade_project,
                UpgradeHandoverRow.preparation_sha256 == preparation_sha256,
            )
        )
        if row is None:
            raise UpgradeConflict("Executor handover status has no retained accepted preparation")
        return handover_summary(row)


@router.post("/bootstrap/handover-claim", response_model=CoreHandoverPreparation | UpgradeClaimIdle)
async def handover_claim(
    body: CoreBootstrapSeed,
    request: Request,
    response: Response,
    principal: UpgradeExecutor,
) -> CoreHandoverPreparation | UpgradeClaimIdle:
    from coire_api.upgrades.handover_authority import claim_handover

    enabled(request, response)
    async with session_scope() as session:
        return await claim_handover(session, principal, body, request.app.state.settings)


@router.post(
    "/bootstrap/handover-recovery-claim", response_model=CoreHandoverPreparation | UpgradeClaimIdle
)
async def handover_recovery_claim(
    body: CoreBootstrapSeed, request: Request, response: Response, principal: UpgradeExecutor
) -> CoreHandoverPreparation | UpgradeClaimIdle:
    from coire_api.upgrades.handover_authority import recovery_claim_handover

    enabled(request, response)
    async with session_scope() as session:
        return await recovery_claim_handover(session, principal, body, request.app.state.settings)


@router.post("/bootstrap/handover-transfer", response_model=CoreHandoverTransferIntent)
async def handover_transfer(
    body: CoreHandoverTransferRequest,
    request: Request,
    response: Response,
    principal: UpgradeExecutor,
) -> CoreHandoverTransferIntent:
    from coire_api.upgrades.handover_transfer import consume_handover_transfer

    enabled(request, response)
    async with session_scope() as session:
        return await consume_handover_transfer(session, principal, body, request.app.state.settings)


@router.post("/bootstrap/handover-commit", response_model=CoreHandoverCommitIntent)
async def handover_commit(
    body: CoreHandoverCommitRequest,
    request: Request,
    response: Response,
    principal: UpgradeExecutor,
) -> CoreHandoverCommitIntent:
    from coire_api.upgrades.handover_commit import decide_handover_commit

    enabled(request, response)
    async with session_scope() as session:
        return await decide_handover_commit(session, principal, body, request.app.state.settings)


@router.post("/bootstrap/handover-committed", response_model=CoreHandoverSummary)
async def handover_committed(
    body: CoreHandoverCommittedRequest,
    request: Request,
    response: Response,
    principal: UpgradeExecutor,
) -> CoreHandoverSummary:
    from coire_api.upgrades.handover_completion import acknowledge_handover_commit

    enabled(request, response)
    async with session_scope() as session:
        return await acknowledge_handover_commit(
            session, principal, body, request.app.state.settings
        )


@router.post("/bootstrap/handover-unchanged", response_model=CoreHandoverSummary)
async def handover_unchanged(
    body: CoreHandoverUnchangedRequest,
    request: Request,
    response: Response,
    principal: UpgradeExecutor,
) -> CoreHandoverSummary:
    from coire_api.upgrades.handover_unchanged import acknowledge_unchanged

    enabled(request, response)
    async with session_scope() as session:
        return await acknowledge_unchanged(session, principal, body, request.app.state.settings)


@router.post("/bootstrap/handover-restoring", response_model=CoreHandoverSummary)
async def handover_restoring(
    body: CoreHandoverRecoveryRequest,
    request: Request,
    response: Response,
    principal: UpgradeExecutor,
) -> CoreHandoverSummary:
    from coire_api.upgrades.handover_restoration import begin_restoration

    enabled(request, response)
    async with session_scope() as session:
        return await begin_restoration(session, principal, body, request.app.state.settings)


@router.post("/bootstrap/handover-restored", response_model=CoreHandoverSummary)
async def handover_restored(
    body: CoreHandoverRestoreRequest,
    request: Request,
    response: Response,
    principal: UpgradeExecutor,
) -> CoreHandoverSummary:
    from coire_api.upgrades.handover_restoration import acknowledge_restoration

    enabled(request, response)
    async with session_scope() as session:
        return await acknowledge_restoration(session, principal, body, request.app.state.settings)


@router.post("/bootstrap/heartbeat", response_model=CoreBootstrapHeartbeat)
async def bootstrap_heartbeat(
    body: CoreBootstrapHeartbeat, request: Request, response: Response, principal: UpgradeExecutor
) -> CoreBootstrapHeartbeat:
    from coire_api.upgrades.bootstrap_heartbeat import retain_bootstrap_heartbeat

    enabled(request, response)
    async with session_scope() as session:
        return await retain_bootstrap_heartbeat(
            session, principal, body, request.app.state.settings
        )


@router.get("/{job_id}/inventory", response_model=CoreUpgradeInventory)
@tracer.start_as_current_span(
    "coire.api.upgrade_executor_inventory", record_exception=False, set_status_on_exception=False
)
async def inventory(
    job_id: UpgradeId, request: Request, response: Response, principal: UpgradeExecutor
) -> CoreUpgradeInventory:
    from coire_api.upgrades.inventory import core_inventory

    enabled(request, response)
    async with session_scope() as session:
        _, plan = await executor.scoped_operation(
            session, job_id, principal, request.app.state.settings
        )
        return await core_inventory(
            session, request.app.state.settings, plan, request.app.state.upgrade_admissions
        )


@router.post("/{job_id}/peer-inventory", response_model=NodeUpgradeInventory)
async def peer_inventory(
    job_id: UpgradeId,
    body: NodeUpgradePeerRead,
    request: Request,
    response: Response,
    x_coire_node: Annotated[StudioName | None, Header()] = None,
    authorization: Annotated[str | None, Header(max_length=4096)] = None,
) -> NodeUpgradeInventory:
    if x_coire_node is None or authorization is None or not authorization.startswith("Bearer "):
        raise HTTPException(401, "node credential required", headers={"WWW-Authenticate": "Bearer"})
    try:
        require_node_credential(
            x_coire_node, authorization.removeprefix("Bearer "), request.app.state.settings
        )
    except CoireError:
        raise HTTPException(
            401, "node credential required", headers={"WWW-Authenticate": "Bearer"}
        ) from None
    enabled(request, response)
    async with (
        session_scope() as session,
        NodeClient(request.app.state.settings, timeout=5) as client,
    ):
        return await guard_peer_inventory(session, x_coire_node, job_id, body, client)


@router.post("/{job_id}/collective-cleanup-peer", response_model=NodeUpgradeInventory)
async def cleanup_peer_inventory(
    job_id: UpgradeId,
    body: NodeUpgradeCollectiveCleanup,
    request: Request,
    response: Response,
    x_coire_node: Annotated[StudioName | None, Header()] = None,
    authorization: Annotated[str | None, Header(max_length=4096)] = None,
) -> NodeUpgradeInventory:
    from coire_api.upgrades.collective_cleanup import collective_cleanup_peer

    if x_coire_node is None or authorization is None or not authorization.startswith("Bearer "):
        raise HTTPException(401, "node credential required", headers={"WWW-Authenticate": "Bearer"})
    try:
        require_node_credential(
            x_coire_node, authorization.removeprefix("Bearer "), request.app.state.settings
        )
    except CoireError:
        raise HTTPException(
            401, "node credential required", headers={"WWW-Authenticate": "Bearer"}
        ) from None
    enabled(request, response)
    async with (
        session_scope() as session,
        NodeClient(request.app.state.settings, timeout=5) as client,
    ):
        return await collective_cleanup_peer(session, x_coire_node, job_id, body, client)


@router.post(
    "/{recovery_id}/recovery-consumption", response_model=UpgradeExpiredRecoveryTransferConsumption
)
async def recovery_consumption_gate(
    recovery_id: UpgradeId,
    body: UpgradeExpiredRecoveryConsumptionCheck,
    request: Request,
    response: Response,
    x_coire_node: Annotated[StudioName | None, Header()] = None,
    authorization: Annotated[str | None, Header(max_length=4096)] = None,
) -> UpgradeExpiredRecoveryTransferConsumption:
    from coire_api.upgrades.expired_recovery_consumption import verify_live_consumption

    if x_coire_node is None or authorization is None or not authorization.startswith("Bearer "):
        raise HTTPException(401, "node credential required", headers={"WWW-Authenticate": "Bearer"})
    try:
        require_node_credential(
            x_coire_node, authorization.removeprefix("Bearer "), request.app.state.settings
        )
    except CoireError:
        raise HTTPException(
            401, "node credential required", headers={"WWW-Authenticate": "Bearer"}
        ) from None
    enabled(request, response)
    async with session_scope() as session:
        return await verify_live_consumption(
            session, recovery_id, body.consumption_sha256, body.consumer, x_coire_node
        )


def require_successor_node(
    request: Request, response: Response, node: StudioName | None, authorization: str | None
) -> StudioName:
    if node is None or authorization is None or not authorization.startswith("Bearer "):
        raise HTTPException(401, "node credential required", headers={"WWW-Authenticate": "Bearer"})
    try:
        require_node_credential(
            node, authorization.removeprefix("Bearer "), request.app.state.settings
        )
    except CoireError:
        raise HTTPException(
            401, "node credential required", headers={"WWW-Authenticate": "Bearer"}
        ) from None
    enabled(request, response)
    return node


@router.post("/{recovery_id}/recovery-cold-claim", response_model=UpgradeExpiredRecoveryColdClaim)
async def cold_recovery_accept(
    recovery_id: UpgradeId,
    body: UpgradeExpiredRecoveryColdClaim,
    request: Request,
    response: Response,
    x_coire_node: Annotated[StudioName | None, Header()] = None,
    authorization: Annotated[str | None, Header(max_length=4096)] = None,
) -> UpgradeExpiredRecoveryColdClaim:
    from coire_api.upgrades.expired_recovery_cold import accept_cold_claim, record_cold_claim

    node = require_successor_node(request, response, x_coire_node, authorization)
    if body.intent.recovery_id != recovery_id:
        raise UpgradeConflict("Cold recovery path differs from retained intent")
    async with session_scope() as session:
        result, created = await accept_cold_claim(session, body, node)
    record_cold_claim(result, created)
    return result


@router.post(
    "/{recovery_id}/recovery-cold-smoke", response_model=UpgradeExpiredRecoveryColdSmokeCommand
)
async def cold_recovery_smoke(
    recovery_id: UpgradeId,
    body: UpgradeExpiredRecoveryColdSmokeCommand,
    request: Request,
    response: Response,
    x_coire_node: Annotated[StudioName | None, Header()] = None,
    authorization: Annotated[str | None, Header(max_length=4096)] = None,
) -> UpgradeExpiredRecoveryColdSmokeCommand:
    from coire_api.upgrades.expired_recovery_cold import authorize_cold_smoke

    node = require_successor_node(request, response, x_coire_node, authorization)
    if body.source.launch.source.projection.owner.intent.recovery_id != recovery_id:
        raise UpgradeConflict("Cold Source smoke path differs from captured intent")
    async with session_scope() as session:
        result, _ = await authorize_cold_smoke(session, body, node)
    return result


@router.post("/{recovery_id}/recovery-cold-check", response_model=UpgradeExpiredRecoveryColdClaim)
async def cold_recovery_check(
    recovery_id: UpgradeId,
    body: UpgradeExpiredRecoveryColdCheck,
    request: Request,
    response: Response,
    x_coire_node: Annotated[StudioName | None, Header()] = None,
    authorization: Annotated[str | None, Header(max_length=4096)] = None,
) -> UpgradeExpiredRecoveryColdClaim:
    from coire_api.upgrades.expired_recovery_cold import verify_cold_owner

    node = require_successor_node(request, response, x_coire_node, authorization)
    async with session_scope() as session:
        return await verify_cold_owner(session, recovery_id, body.claim_sha256, body.consumer, node)


@router.post("/{recovery_id}/recovery-cold-state", response_model=UpgradeExpiredRecoveryColdState)
async def cold_recovery_state(
    recovery_id: UpgradeId,
    body: UpgradeExpiredRecoveryColdCheck,
    request: Request,
    response: Response,
    x_coire_node: Annotated[StudioName | None, Header()] = None,
    authorization: Annotated[str | None, Header(max_length=4096)] = None,
) -> UpgradeExpiredRecoveryColdState:
    from coire_api.upgrades.expired_recovery_cold import probe_cold_owner

    node = require_successor_node(request, response, x_coire_node, authorization)
    async with session_scope() as session:
        return await probe_cold_owner(session, recovery_id, body.claim_sha256, body.consumer, node)


@router.post(
    "/{job_id}/recovery-initial-observation", response_model=UpgradeExpiredRecoveryObservation
)
@tracer.start_as_current_span("coire.api.recovery_initial_observation")
async def initial_recovery_observation(
    job_id: UpgradeId,
    body: UpgradeExpiredRecoveryObservation,
    request: Request,
    response: Response,
    x_coire_node: Annotated[StudioName | None, Header()] = None,
    authorization: Annotated[str | None, Header(max_length=4096)] = None,
) -> UpgradeExpiredRecoveryObservation:
    from coire_api.upgrades.expired_recovery_initial_relay import verify_observation_context

    node = require_successor_node(request, response, x_coire_node, authorization)
    if job_id != body.request.original_job_id:
        raise UpgradeConflict("Initial recovery observation path differs")
    async with session_scope() as session:
        await verify_observation_context(session, body, node)
    request.app.state.upgrade_initial_recovery_observations.publish(body)
    return body


@router.post(
    "/{recovery_id}/recovery-succession-observation",
    response_model=UpgradeExpiredRecoverySuccessionObservation,
)
@tracer.start_as_current_span("coire.api.recovery_succession_observation")
async def succession_observation(
    recovery_id: UpgradeId,
    body: UpgradeExpiredRecoverySuccessionObservation,
    request: Request,
    response: Response,
    x_coire_node: Annotated[StudioName | None, Header()] = None,
    authorization: Annotated[str | None, Header(max_length=4096)] = None,
) -> UpgradeExpiredRecoverySuccessionObservation:
    from coire_api.upgrades.expired_recovery_owner import verify_live_owner

    node = require_successor_node(request, response, x_coire_node, authorization)
    previous = body.previous_owner
    if previous.consumption.transfer.intent.recovery_id != recovery_id:
        raise HTTPException(403, "recovery observation scope differs")
    async with session_scope() as session:
        await verify_live_owner(session, recovery_id, previous.sha256(), previous.process, node)
    request.app.state.upgrade_succession_observations.publish(body)
    return body


@router.post(
    "/{recovery_id}/recovery-succession-delivery",
    response_model=UpgradeExpiredRecoverySuccessionDeliveryState,
)
@tracer.start_as_current_span("coire.api.recovery_succession_delivery")
async def succession_delivery(
    recovery_id: UpgradeId,
    body: UpgradeExpiredRecoverySuccessionDeliveryRead,
    request: Request,
    response: Response,
    x_coire_node: Annotated[StudioName | None, Header()] = None,
    authorization: Annotated[str | None, Header(max_length=4096)] = None,
) -> UpgradeExpiredRecoverySuccessionDeliveryState:
    from coire_api.upgrades.expired_recovery_succession_delivery import read_succession_delivery

    node = require_successor_node(request, response, x_coire_node, authorization)
    async with session_scope() as session:
        return await read_succession_delivery(session, recovery_id, body, node)


@router.post(
    "/{succession_id}/recovery-successor-claim", response_model=UpgradeExpiredRecoverySuccessorClaim
)
async def successor_accept(
    succession_id: UpgradeId,
    body: UpgradeExpiredRecoverySuccessorClaim,
    request: Request,
    response: Response,
    x_coire_node: Annotated[StudioName | None, Header()] = None,
    authorization: Annotated[str | None, Header(max_length=4096)] = None,
) -> UpgradeExpiredRecoverySuccessorClaim:
    from coire_api.upgrades.expired_recovery_successor import accept_successor_claim

    node = require_successor_node(request, response, x_coire_node, authorization)
    if body.grant.observation.succession_id != succession_id:
        raise HTTPException(403, "succession scope differs")
    async with session_scope() as session:
        claim, _ = await accept_successor_claim(session, body, node)
        return claim


@router.post(
    "/{succession_id}/recovery-successor-check", response_model=UpgradeExpiredRecoverySuccessorClaim
)
async def successor_gate(
    succession_id: UpgradeId,
    body: UpgradeExpiredRecoverySuccessorCheck,
    request: Request,
    response: Response,
    x_coire_node: Annotated[StudioName | None, Header()] = None,
    authorization: Annotated[str | None, Header(max_length=4096)] = None,
) -> UpgradeExpiredRecoverySuccessorClaim:
    from coire_api.upgrades.expired_recovery_successor import verify_live_successor

    node = require_successor_node(request, response, x_coire_node, authorization)
    async with session_scope() as session:
        return await verify_live_successor(
            session, succession_id, body.claim_sha256, body.consumer, node
        )


@router.post(
    "/{succession_id}/recovery-successor-state", response_model=UpgradeExpiredRecoverySuccessorState
)
async def successor_state(
    succession_id: UpgradeId,
    body: UpgradeExpiredRecoverySuccessorStateCheck,
    request: Request,
    response: Response,
    x_coire_node: Annotated[StudioName | None, Header()] = None,
    authorization: Annotated[str | None, Header(max_length=4096)] = None,
) -> UpgradeExpiredRecoverySuccessorState:
    from coire_api.upgrades.expired_recovery_successor import probe_successor_acceptance

    node = require_successor_node(request, response, x_coire_node, authorization)
    async with session_scope() as session:
        return await probe_successor_acceptance(
            session,
            succession_id,
            body.claim_sha256,
            body.consumer,
            node,
            grant_sha256=body.grant_sha256,
        )


@router.post("/{recovery_id}/recovery-owner-check", response_model=UpgradeExpiredRecoveryOwnerClaim)
async def recovery_owner_gate(
    recovery_id: UpgradeId,
    body: UpgradeExpiredRecoveryOwnerCheck,
    request: Request,
    response: Response,
    x_coire_node: Annotated[StudioName | None, Header()] = None,
    authorization: Annotated[str | None, Header(max_length=4096)] = None,
) -> UpgradeExpiredRecoveryOwnerClaim:
    from coire_api.upgrades.expired_recovery_owner import verify_live_owner

    if x_coire_node is None or authorization is None or not authorization.startswith("Bearer "):
        raise HTTPException(401, "node credential required", headers={"WWW-Authenticate": "Bearer"})
    try:
        require_node_credential(
            x_coire_node, authorization.removeprefix("Bearer "), request.app.state.settings
        )
    except CoireError:
        raise HTTPException(
            401, "node credential required", headers={"WWW-Authenticate": "Bearer"}
        ) from None
    enabled(request, response)
    async with session_scope() as session:
        return await verify_live_owner(
            session, recovery_id, body.owner_sha256, body.consumer, x_coire_node
        )


@router.post("/{recovery_id}/recovery-owner-claim", response_model=UpgradeExpiredRecoveryOwnerClaim)
async def recovery_owner_accept(
    recovery_id: UpgradeId,
    body: UpgradeExpiredRecoveryOwnerClaim,
    request: Request,
    response: Response,
    x_coire_node: Annotated[StudioName | None, Header()] = None,
    authorization: Annotated[str | None, Header(max_length=4096)] = None,
) -> UpgradeExpiredRecoveryOwnerClaim:
    from coire_api.upgrades.expired_recovery_owner import accept_owner_claim

    if x_coire_node is None or authorization is None or not authorization.startswith("Bearer "):
        raise HTTPException(401, "node credential required", headers={"WWW-Authenticate": "Bearer"})
    try:
        require_node_credential(
            x_coire_node, authorization.removeprefix("Bearer "), request.app.state.settings
        )
    except CoireError:
        raise HTTPException(
            401, "node credential required", headers={"WWW-Authenticate": "Bearer"}
        ) from None
    enabled(request, response)
    if body.consumption.transfer.intent.recovery_id != recovery_id:
        raise HTTPException(403, "recovery scope differs")
    async with session_scope() as session:
        claim, _ = await accept_owner_claim(session, body, x_coire_node)
        return claim


@router.post("/{recovery_id}/recovery-owner-state", response_model=UpgradeExpiredRecoveryOwnerState)
async def recovery_owner_state(
    recovery_id: UpgradeId,
    body: UpgradeExpiredRecoveryOwnerCheck,
    request: Request,
    response: Response,
    x_coire_node: Annotated[StudioName | None, Header()] = None,
    authorization: Annotated[str | None, Header(max_length=4096)] = None,
) -> UpgradeExpiredRecoveryOwnerState:
    from coire_api.upgrades.expired_recovery_owner import probe_owner_acceptance

    if x_coire_node is None or authorization is None or not authorization.startswith("Bearer "):
        raise HTTPException(401, "node credential required", headers={"WWW-Authenticate": "Bearer"})
    try:
        require_node_credential(
            x_coire_node, authorization.removeprefix("Bearer "), request.app.state.settings
        )
    except CoireError:
        raise HTTPException(
            401, "node credential required", headers={"WWW-Authenticate": "Bearer"}
        ) from None
    enabled(request, response)
    async with session_scope() as session:
        return await probe_owner_acceptance(
            session, recovery_id, body.owner_sha256, body.consumer, x_coire_node
        )


@router.post(
    "/{recovery_id}/recovery-unstarted-context", response_model=UpgradeExpiredRecoveryIntent
)
async def recovery_unstarted_context(
    recovery_id: UpgradeId,
    body: UpgradeExpiredRecoveryUnstartedRead,
    request: Request,
    response: Response,
    x_coire_node: Annotated[StudioName | None, Header()] = None,
    authorization: Annotated[str | None, Header(max_length=4096)] = None,
) -> UpgradeExpiredRecoveryIntent:
    from coire_api.upgrades.expired_recovery_unstarted import unstarted_context

    node = require_successor_node(request, response, x_coire_node, authorization)
    async with session_scope() as session:
        _, intent = await unstarted_context(session, recovery_id, body.intent_sha256, node)
    return intent


@router.post(
    "/{recovery_id}/recovery-unstarted-observation",
    response_model=UpgradeExpiredRecoveryUnstartedObservation,
)
async def recovery_unstarted_observation(
    recovery_id: UpgradeId,
    body: UpgradeExpiredRecoveryUnstartedObservation,
    request: Request,
    response: Response,
    x_coire_node: Annotated[StudioName | None, Header()] = None,
    authorization: Annotated[str | None, Header(max_length=4096)] = None,
) -> UpgradeExpiredRecoveryUnstartedObservation:
    from coire_api.upgrades.expired_recovery_unstarted import verify_unstarted_observation

    node = require_successor_node(request, response, x_coire_node, authorization)
    if body.intent.recovery_id != recovery_id:
        raise UpgradeConflict("Unstarted recovery observation path differs")
    async with session_scope() as session:
        await verify_unstarted_observation(session, body, node)
    request.app.state.upgrade_unstarted_recovery_observations.publish(body)
    return body


@router.post("/{recovery_id}/recovery-support-context", response_model=UpgradeExpiredRecoveryIntent)
async def recovery_support_context(
    recovery_id: UpgradeId,
    body: UpgradeExpiredRecoverySupportContextRead,
    request: Request,
    response: Response,
    x_coire_node: Annotated[StudioName | None, Header()] = None,
    authorization: Annotated[str | None, Header(max_length=4096)] = None,
) -> UpgradeExpiredRecoveryIntent:
    from coire_api.upgrades.expired_recovery_owner import read_live_support_context

    if x_coire_node is None or authorization is None or not authorization.startswith("Bearer "):
        raise HTTPException(401, "node credential required", headers={"WWW-Authenticate": "Bearer"})
    try:
        require_node_credential(
            x_coire_node, authorization.removeprefix("Bearer "), request.app.state.settings
        )
    except CoireError:
        raise HTTPException(
            401, "node credential required", headers={"WWW-Authenticate": "Bearer"}
        ) from None
    enabled(request, response)
    async with session_scope() as session:
        return await read_live_support_context(session, recovery_id, x_coire_node)


@router.post(
    "/{recovery_id}/recovery-consumption-delivery",
    response_model=UpgradeExpiredRecoveryConsumptionState,
)
async def recovery_consumption_delivery(
    recovery_id: UpgradeId,
    body: UpgradeExpiredRecoveryConsumptionDeliveryRead,
    request: Request,
    response: Response,
    x_coire_node: Annotated[StudioName | None, Header()] = None,
    authorization: Annotated[str | None, Header(max_length=4096)] = None,
) -> UpgradeExpiredRecoveryConsumptionState:
    from coire_api.upgrades.expired_recovery_delivery import read_consumption_state

    if x_coire_node is None or authorization is None or not authorization.startswith("Bearer "):
        raise HTTPException(401, "node credential required", headers={"WWW-Authenticate": "Bearer"})
    try:
        require_node_credential(
            x_coire_node, authorization.removeprefix("Bearer "), request.app.state.settings
        )
    except CoireError:
        raise HTTPException(
            401, "node credential required", headers={"WWW-Authenticate": "Bearer"}
        ) from None
    enabled(request, response)
    async with session_scope() as session:
        return await read_consumption_state(
            session, recovery_id, body.intent_sha256, body.consumer, x_coire_node
        )


@router.post("/{job_id}/collective", response_model=NodeUpgradeCollectiveResult)
async def collective(
    job_id: UpgradeId,
    body: NodeUpgradeCollectiveRead,
    request: Request,
    response: Response,
    x_coire_node: Annotated[StudioName | None, Header()] = None,
    authorization: Annotated[str | None, Header(max_length=4096)] = None,
) -> NodeUpgradeCollectiveResult:
    from coire_api.upgrades.collective import qualify_collective

    if x_coire_node is None or authorization is None or not authorization.startswith("Bearer "):
        raise HTTPException(401, "node credential required", headers={"WWW-Authenticate": "Bearer"})
    try:
        require_node_credential(
            x_coire_node, authorization.removeprefix("Bearer "), request.app.state.settings
        )
    except CoireError:
        raise HTTPException(
            401, "node credential required", headers={"WWW-Authenticate": "Bearer"}
        ) from None
    enabled(request, response)
    async with session_scope() as session, NodeClient(request.app.state.settings) as client:
        return await qualify_collective(
            session,
            x_coire_node,
            job_id,
            body,
            client,
            freshness_s=request.app.state.settings.link_probe_freshness_s,
        )


@router.post("/claim", response_model=UpgradeExecutionPlan | UpgradeClaimIdle)
@tracer.start_as_current_span(
    "coire.api.upgrade_executor_claim", record_exception=False, set_status_on_exception=False
)
async def claim(
    body: UpgradeClaimRequest, request: Request, response: Response, principal: UpgradeExecutor
) -> UpgradeExecutionPlan | UpgradeClaimIdle:
    enabled(request, response)
    async with session_scope() as session:
        return await executor.claim(session, principal, body, request.app.state.settings)


@router.post("/{job_id}/receipts", response_model=UpgradeReceiptAck)
@tracer.start_as_current_span(
    "coire.api.upgrade_executor_receipts", record_exception=False, set_status_on_exception=False
)
async def receipts(
    job_id: UpgradeId,
    body: UpgradeReceiptBatch,
    request: Request,
    response: Response,
    principal: UpgradeExecutor,
) -> UpgradeReceiptAck:
    enabled(request, response)
    async with session_scope() as session:
        row, plan = await executor.scoped_operation(
            session, job_id, principal, request.app.state.settings, lock=True
        )
        for receipt in body.receipts:
            if (receipt.job_id, receipt.generation) != (job_id, plan.operation.generation):
                raise UpgradeConflict("Executor receipt belongs to another accepted operation")
            await ingest_receipt(session, receipt, principal, settings=request.app.state.settings)
        operation = UpgradeOperation.model_validate(row.payload)
        return UpgradeReceiptAck(
            job_id=job_id, generation=operation.generation, sequence=operation.receipt_sequence
        )


@router.post("/{job_id}/ready-to-activate", response_model=UpgradeActivationGrant)
async def ready_to_activate(
    job_id: UpgradeId,
    body: UpgradeActivationRequest,
    request: Request,
    response: Response,
    principal: UpgradeExecutor,
) -> UpgradeActivationGrant:
    enabled(request, response)
    async with session_scope() as session:
        await executor.scoped_operation(session, job_id, principal, request.app.state.settings)
        return await grants.issue(
            session,
            job_id,
            body,
            settings=request.app.state.settings,
            admissions=request.app.state.upgrade_admissions,
        )


@router.post("/{job_id}/consume-activation", response_model=UpgradeReceiptAck)
async def consume_activation(
    job_id: UpgradeId,
    body: UpgradeActivationConsume,
    request: Request,
    response: Response,
    principal: UpgradeExecutor,
) -> UpgradeReceiptAck:
    enabled(request, response)
    if body.grant.job_id != job_id:
        raise UpgradeConflict("Executor activation path and accepted job differ")
    async with session_scope() as session:
        row, plan = await executor.scoped_operation(
            session, job_id, principal, request.app.state.settings
        )
        if body.grant.generation != plan.operation.generation:
            raise UpgradeConflict("Executor activation generation differs")
        await grants.consume(
            session,
            body.grant,
            body.intent,
            principal,
            settings=request.app.state.settings,
            admissions=request.app.state.upgrade_admissions,
        )
        operation = UpgradeOperation.model_validate(row.payload)
        return UpgradeReceiptAck(
            job_id=job_id, generation=operation.generation, sequence=operation.receipt_sequence
        )


@router.get("/{job_id}/schema", response_model=UpgradeSchemaObservation)
@tracer.start_as_current_span(
    "coire.api.upgrade_executor_schema", record_exception=False, set_status_on_exception=False
)
async def schema_observation(
    job_id: UpgradeId, request: Request, response: Response, principal: UpgradeExecutor
) -> UpgradeSchemaObservation:
    from coire_api.upgrades.schema_identity import inspect_schema

    enabled(request, response)
    async with session_scope() as session:
        _, plan = await executor.scoped_operation(
            session, job_id, principal, request.app.state.settings
        )
        started = datetime.now(UTC)
        head, digest = await inspect_schema(session)
        return UpgradeSchemaObservation(
            job_id=job_id,
            generation=plan.operation.generation,
            schema_head=head,
            schema_sha256=digest,
            observed_at=started,
        )


@router.get("/{job_id}/migration", response_model=UpgradeMigrationExecution)
async def migration_proof(
    job_id: UpgradeId, request: Request, response: Response, principal: UpgradeExecutor
) -> UpgradeMigrationExecution:
    enabled(request, response)
    async with session_scope() as session:
        row, plan = await executor.scoped_operation(
            session, job_id, principal, request.app.state.settings
        )
        if row.migration_execution is None or row.migration_execution.get("state") == "not_started":
            raise UpgradeUnavailable("Accepted migration has no retained execution observation")
        proof = UpgradeMigrationExecution.model_validate(row.migration_execution)
        if (proof.job_id, proof.generation, proof.plan_sha256) != (
            job_id,
            plan.operation.generation,
            plan.authority_sha256(),
        ):
            raise UpgradeConflict("Retained migration proof authority differs")
        return proof


@router.post("/{job_id}/migration-not-started", response_model=UpgradeMigrationNotStarted)
async def migration_not_started(
    job_id: UpgradeId, request: Request, response: Response, principal: UpgradeExecutor
) -> UpgradeMigrationNotStarted:
    from coire_api.upgrades.migration import fence_not_started

    enabled(request, response)
    async with session_scope() as session:
        _, plan = await executor.scoped_operation(
            session, job_id, principal, request.app.state.settings
        )
        return await fence_not_started(session, plan, principal, request.app.state.settings)


@router.get("/{job_id}", response_model=UpgradeOperation)
async def operation_status(
    job_id: UpgradeId, request: Request, response: Response, principal: UpgradeExecutor
) -> UpgradeOperation:
    enabled(request, response)
    async with session_scope() as session:
        row, _ = await executor.scoped_operation(
            session, job_id, principal, request.app.state.settings
        )
        return UpgradeOperation.model_validate(row.payload)


@router.post("/{job_id}/smoke", response_model=UpgradeSmokeResult)
@tracer.start_as_current_span(
    "coire.api.upgrade_gateway_smoke", record_exception=False, set_status_on_exception=False
)
async def gateway_smoke(
    job_id: UpgradeId,
    body: UpgradeSmokeRequest,
    request: Request,
    response: Response,
    principal: UpgradeExecutor,
) -> UpgradeSmokeResult:
    from coire_api.upgrades.gateway_smoke import run_gateway_smoke

    enabled(request, response)
    return await run_gateway_smoke(job_id, body, principal, request.app.state.settings)


@router.post(
    "/{recovery_id}/recovery-cold-verification",
    response_model=UpgradeExpiredRecoveryColdVerification,
)
async def cold_recovery_verification(
    recovery_id: UpgradeId,
    body: UpgradeExpiredRecoveryColdVerificationRequest,
    request: Request,
    response: Response,
    x_coire_node: Annotated[StudioName | None, Header()] = None,
    authorization: Annotated[str | None, Header(max_length=4096)] = None,
) -> UpgradeExpiredRecoveryColdVerification:
    from coire_api.nodes_client import NodeError
    from coire_api.upgrades.expired_recovery_cold_verification import prepare_verification

    node = require_successor_node(request, response, x_coire_node, authorization)
    owner = body.smoke.smoke.source.launch.source.projection.owner
    if owner.intent.recovery_id != recovery_id:
        raise UpgradeConflict("Cold verification path differs from captured intent")

    async def observe() -> tuple[NodeUpgradeInventory, NodeUpgradeInventory | None]:
        try:
            async with NodeClient(request.app.state.settings, timeout=5) as client:
                plan = owner.intent.original_plan
                local = await client.upgrade_inventory(
                    node,
                    owner.intent.local_maintenance().job_id,
                    owner.intent.local_maintenance().generation,
                )
                peer = (
                    await client.upgrade_inventory(
                        "coire-edge-b" if node == "coire-edge-a" else "coire-edge-a",
                        plan.operation.job_id,
                        plan.operation.generation,
                    )
                    if plan.peer_runtime_sha256 is not None
                    else None
                )
                return local, peer
        except NodeError:
            raise UpgradeUnavailable(
                "Cold verification cannot observe declared native owners"
            ) from None

    from coire_api.upgrades.expired_recovery_cold import record_cold_transition

    result, created = await prepare_verification(session_factory(), node, body, observe)
    owner = result.request.smoke.smoke.source.launch.source.projection.owner
    record_cold_transition(owner, "source_verified", created)
    return result


@router.post(
    "/{recovery_id}/recovery-cold-verification-check",
    response_model=UpgradeExpiredRecoveryColdVerification,
)
async def cold_recovery_verification_check(
    recovery_id: UpgradeId,
    body: UpgradeExpiredRecoveryColdVerificationRead,
    request: Request,
    response: Response,
    x_coire_node: Annotated[StudioName | None, Header()] = None,
    authorization: Annotated[str | None, Header(max_length=4096)] = None,
) -> UpgradeExpiredRecoveryColdVerification:
    from coire_api.upgrades.expired_recovery_cold_verification import verify_live_verification

    node = require_successor_node(request, response, x_coire_node, authorization)
    async with session_scope() as session:
        return await verify_live_verification(
            session, recovery_id, body.verification_sha256, body.consumer, node
        )


@router.post(
    "/{recovery_id}/recovery-cold-handback", response_model=UpgradeExpiredRecoveryColdHandbackIntent
)
async def cold_recovery_handback(
    recovery_id: UpgradeId,
    body: UpgradeExpiredRecoveryColdHandbackIntent,
    request: Request,
    response: Response,
    x_coire_node: Annotated[StudioName | None, Header()] = None,
    authorization: Annotated[str | None, Header(max_length=4096)] = None,
) -> UpgradeExpiredRecoveryColdHandbackIntent:
    from coire_api.upgrades.expired_recovery_cold import record_cold_transition
    from coire_api.upgrades.expired_recovery_cold_handback import accept_handback_intent

    node = require_successor_node(request, response, x_coire_node, authorization)
    if (
        body.verification.request.smoke.smoke.source.launch.source.projection.owner.intent.recovery_id
        != recovery_id
    ):
        raise UpgradeConflict("Cold Source handback path differs")
    async with session_scope() as session:
        result, created = await accept_handback_intent(session, body, node)
    owner = result.verification.request.smoke.smoke.source.launch.source.projection.owner
    record_cold_transition(owner, "handback_prepared", created)
    return result


@router.post(
    "/{recovery_id}/recovery-cold-handback-check",
    response_model=UpgradeExpiredRecoveryColdHandbackIntent,
)
async def cold_recovery_handback_check(
    recovery_id: UpgradeId,
    body: UpgradeExpiredRecoveryColdHandbackIntent,
    request: Request,
    response: Response,
    x_coire_node: Annotated[StudioName | None, Header()] = None,
    authorization: Annotated[str | None, Header(max_length=4096)] = None,
) -> UpgradeExpiredRecoveryColdHandbackIntent:
    from coire_api.upgrades.expired_recovery_cold_handback import verify_handback_intent

    node = require_successor_node(request, response, x_coire_node, authorization)
    if (
        body.verification.request.smoke.smoke.source.launch.source.projection.owner.intent.recovery_id
        != recovery_id
    ):
        raise UpgradeConflict("Cold Source handback check path differs")
    async with session_scope() as session:
        return await verify_handback_intent(session, body, node)


@router.post(
    "/{recovery_id}/recovery-cold-handback-receipt",
    response_model=UpgradeExpiredRecoveryColdHandbackReceipt,
)
async def cold_recovery_handback_receipt(
    recovery_id: UpgradeId,
    body: UpgradeExpiredRecoveryColdHandbackReceipt,
    request: Request,
    response: Response,
    x_coire_node: Annotated[StudioName | None, Header()] = None,
    authorization: Annotated[str | None, Header(max_length=4096)] = None,
) -> UpgradeExpiredRecoveryColdHandbackReceipt:
    from coire_api.upgrades.expired_recovery_cold import record_cold_transition
    from coire_api.upgrades.expired_recovery_cold_handback import accept_handback_receipt

    node = require_successor_node(request, response, x_coire_node, authorization)
    owner = (
        body.helper.handback.verification.request.smoke.smoke.source.launch.source.projection.owner
    )
    if owner.intent.recovery_id != recovery_id:
        raise UpgradeConflict("Cold Source handback receipt path differs")
    async with session_scope() as session:
        result, created = await accept_handback_receipt(session, body, node)
    record_cold_transition(owner, "handed_back", created)
    return result


@router.post(
    "/{recovery_id}/recovery-cold-resolve", response_model=UpgradeExpiredRecoveryColdResolution
)
async def cold_recovery_resolve(
    recovery_id: UpgradeId,
    body: UpgradeExpiredRecoveryColdResolutionRequest,
    request: Request,
    response: Response,
    x_coire_node: Annotated[StudioName | None, Header()] = None,
    authorization: Annotated[str | None, Header(max_length=4096)] = None,
) -> UpgradeExpiredRecoveryColdResolution:
    from coire_api.upgrades.expired_recovery_cold import record_cold_transition
    from coire_api.upgrades.expired_recovery_cold_resolution_orchestration import (
        resolve_with_observation,
    )

    node = require_successor_node(request, response, x_coire_node, authorization)
    owner = body.closure.handback.helper.handback.verification.request.smoke.smoke.source.launch.source.projection.owner
    if owner.intent.recovery_id != recovery_id:
        raise UpgradeConflict("Cold resolution path differs")
    settings = request.app.state.settings
    async with NodeClient(settings, timeout=5.0) as nodes:
        result, created = await resolve_with_observation(session_factory(), body, node, nodes)
    record_cold_transition(owner, "recovered", created)
    return result


@router.post(
    "/{recovery_id}/recovery-cold-resolution-state",
    response_model=UpgradeExpiredRecoveryColdResolutionState,
)
async def cold_recovery_resolution_state(
    recovery_id: UpgradeId,
    body: UpgradeExpiredRecoveryColdResolutionRead,
    request: Request,
    response: Response,
    x_coire_node: Annotated[StudioName | None, Header()] = None,
    authorization: Annotated[str | None, Header(max_length=4096)] = None,
) -> UpgradeExpiredRecoveryColdResolutionState:
    from coire_api.upgrades.expired_recovery_cold_resolution import read_resolution_state

    node = require_successor_node(request, response, x_coire_node, authorization)
    async with session_scope() as session:
        return await read_resolution_state(session, recovery_id, body, node)


@router.post(
    "/{recovery_id}/recovery-cold-continuation-context",
    response_model=UpgradeColdContinuationContext,
)
async def cold_continuation_context(
    recovery_id: UpgradeId,
    body: UpgradeColdContinuationRead,
    request: Request,
    response: Response,
    x_coire_node: Annotated[StudioName | None, Header()] = None,
    authorization: Annotated[str | None, Header(max_length=4096)] = None,
) -> UpgradeColdContinuationContext:
    from coire_api.upgrades.cold_continuation_context import read_cold_predecessor_context

    node = require_successor_node(request, response, x_coire_node, authorization)
    async with session_scope() as session:
        return await read_cold_predecessor_context(session, recovery_id, body, node)


@router.post(
    "/{recovery_id}/recovery-cold-continuation-observation",
    response_model=UpgradeColdContinuationObservation,
)
async def cold_continuation_observation(
    recovery_id: UpgradeId,
    body: UpgradeColdContinuationObservation,
    request: Request,
    response: Response,
    x_coire_node: Annotated[StudioName | None, Header()] = None,
    authorization: Annotated[str | None, Header(max_length=4096)] = None,
) -> UpgradeColdContinuationObservation:
    from coire_api.upgrades.cold_continuation_relay import verify_observation_context

    node = require_successor_node(request, response, x_coire_node, authorization)
    if body.binding.previous_recovery_id != recovery_id:
        raise UpgradeConflict("Cold continuation observation path differs")
    async with session_scope() as session:
        await verify_observation_context(session, body, node)
    request.app.state.upgrade_cold_continuation_observations.publish(body)
    return body


@router.post(
    "/{recovery_id}/recovery-cold-continuation-binding",
    response_model=UpgradeColdContinuationBindingState,
)
async def cold_continuation_binding(
    recovery_id: UpgradeId,
    body: UpgradeColdContinuationBindingRead,
    request: Request,
    response: Response,
    x_coire_node: Annotated[StudioName | None, Header()] = None,
    authorization: Annotated[str | None, Header(max_length=4096)] = None,
) -> UpgradeColdContinuationBindingState:
    from coire_api.upgrades.cold_continuation_binding import retained_binding
    from coire_api.upgrades.expired_recovery_cold import pending_binding

    node = require_successor_node(request, response, x_coire_node, authorization)
    async with session_scope() as session:
        _row, intent = await pending_binding(session, recovery_id, node)
        if intent.sha256() != body.intent_sha256:
            raise UpgradeConflict("Cold continuation binding intent differs")
        return UpgradeColdContinuationBindingState(
            intent_sha256=intent.sha256(),
            grant=await retained_binding(session, intent),
            observed_at=datetime.now(UTC),
        )


@router.get("/{job_id}/unclaimed-cancellation", response_model=UpgradeUnclaimedCancellationState)
async def unclaimed_cancellation_state(
    job_id: UpgradeId,
    request: Request,
    response: Response,
    x_coire_node: Annotated[StudioName | None, Header()] = None,
    authorization: Annotated[str | None, Header(max_length=4096)] = None,
) -> UpgradeUnclaimedCancellationState:
    from coire_api.upgrades.unclaimed_cancellation import read_unclaimed_cancellation

    if x_coire_node is None or authorization is None or not authorization.startswith("Bearer "):
        raise HTTPException(401, "node credential required", headers={"WWW-Authenticate": "Bearer"})
    try:
        require_node_credential(
            x_coire_node, authorization.removeprefix("Bearer "), request.app.state.settings
        )
    except CoireError:
        raise HTTPException(
            401, "node credential required", headers={"WWW-Authenticate": "Bearer"}
        ) from None
    enabled(request, response)
    async with session_scope() as session:
        return await read_unclaimed_cancellation(session, job_id, x_coire_node)


@router.get("/{recovery_id}/native-terminal", response_model=UpgradeNativeTerminalState)
async def read_legacy_native_terminal(
    recovery_id: UpgradeId,
    request: Request,
    response: Response,
    x_coire_node: Annotated[StudioName | None, Header()] = None,
    authorization: Annotated[str | None, Header(max_length=4096)] = None,
) -> UpgradeNativeTerminalState:
    from coire_api.upgrades.native_terminal import native_terminal_state

    if x_coire_node is None or authorization is None or not authorization.startswith("Bearer "):
        raise HTTPException(401, "node credential required", headers={"WWW-Authenticate": "Bearer"})
    try:
        require_node_credential(
            x_coire_node, authorization.removeprefix("Bearer "), request.app.state.settings
        )
    except CoireError:
        raise HTTPException(
            401, "node credential required", headers={"WWW-Authenticate": "Bearer"}
        ) from None
    enabled(request, response)
    async with session_scope() as session:
        return await native_terminal_state(session, recovery_id, x_coire_node)
