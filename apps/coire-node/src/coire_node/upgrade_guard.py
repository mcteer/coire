"""Durable native recovery authority, independent of the selected node process."""

import logging
import os
import platform
import selectors
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, TypeVar

import httpx
import psutil
from opentelemetry import metrics, trace

from coire_core.errors import CoireError, UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrade_collective import NodeUpgradeCollectiveResult
from coire_core.models.upgrade_native_progress import NativeRecoveryFailureReplay
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
)
from coire_core.models.upgrade_recovery_continuation import UpgradeColdContinuationGrant
from coire_core.models.upgrades import (
    TERMINAL_UPGRADE_PHASES,
    NativeAbortIntent,
    NativeBootstrapRefresh,
    NativeGuardStatus,
    NativeNodeProcess,
    NativeRecoveryIntent,
    NativeRuntimeSelection,
    NativeUpgradeClaim,
    NodeAgentProcessIdentity,
    NodeUpgradeCollectiveCleanup,
    NodeUpgradeCollectiveRead,
    NodeUpgradeCommand,
    NodeUpgradeInventory,
    NodeUpgradePeerRead,
    NodeUpgradeStatus,
    PreparedNativeRuntime,
    UpgradeEvidence,
    UpgradeExecutionPlan,
    UpgradeKind,
    UpgradeMaintenance,
    UpgradePhase,
    UpgradeReleaseRegister,
    UpgradeWire,
)
from coire_core.settings import Settings
from coire_core.upgrade_journal import PrivateUpgradeStore, UpgradeReceiptJournal
from coire_node.native_platform import is_darwin

logger = logging.getLogger(__name__)
tracer = trace.get_tracer("coire.node.upgrade_guard")
intent_count = metrics.get_meter("coire.node.upgrade_guard").create_counter(
    "coire_upgrade_guard_intents_total"
)

GuardDocument = TypeVar("GuardDocument", bound=UpgradeWire)


def exchange_smoke_child(
    child: subprocess.Popen[bytes],
    payload: bytes,
    deadline_at: datetime,
    *,
    authority: Callable[[], None] | None = None,
) -> bytes:
    """Bound pipes and wait by the captured deadline, retaining uncertain processes."""
    incoming, outgoing = child.stdout, child.stdin
    if incoming is None or outgoing is None or len(payload) > 512 * 1024:
        raise UpgradeUnavailable("Native smoke child exchange is unavailable")
    remaining = (deadline_at - datetime.now(UTC)).total_seconds()
    if remaining <= 0:
        raise UpgradeUnavailable("Native smoke child deadline elapsed")
    deadline = time.monotonic() + remaining
    result = bytearray()
    offset = 0
    next_check = 0.0
    try:
        os.set_blocking(incoming.fileno(), False)
        os.set_blocking(outgoing.fileno(), False)
        with selectors.DefaultSelector() as selector:
            selector.register(incoming, selectors.EVENT_READ, "output")
            if payload:
                selector.register(outgoing, selectors.EVENT_WRITE, "input")
            else:
                outgoing.close()
            while selector.get_map():
                if authority is not None and time.monotonic() >= next_check:
                    authority()
                    next_check = time.monotonic() + 1.0
                available = deadline - time.monotonic()
                if available <= 0:
                    raise UpgradeUnavailable("Native smoke child exceeded its captured deadline")
                for selected, _ in selector.select(min(0.05, available)):
                    if time.monotonic() >= deadline:
                        raise UpgradeUnavailable(
                            "Native smoke child exceeded its captured deadline"
                        )
                    try:
                        if selected.data == "input":
                            offset += os.write(outgoing.fileno(), payload[offset : offset + 32768])
                            if offset == len(payload):
                                selector.unregister(outgoing)
                                outgoing.close()
                        else:
                            chunk = os.read(incoming.fileno(), 4098 - len(result))
                            if not chunk:
                                selector.unregister(incoming)
                            elif len(result) + len(chunk) > 4097:
                                raise UpgradeUnavailable(
                                    "Native smoke child evidence exceeds its bound"
                                )
                            else:
                                result.extend(chunk)
                    except BlockingIOError:
                        continue
            available = deadline - time.monotonic()
            if authority is not None:
                authority()
            if available <= 0 or child.wait(timeout=available) != 0:
                raise UpgradeUnavailable("Native smoke child did not complete serving verification")
        return bytes(result)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        raise UpgradeUnavailable("Native smoke child exchange remains unverified") from None
    finally:
        incoming.close()
        outgoing.close()


class NativeGuardTransport:
    """Fixed same-node control traffic; no caller-supplied URL or peer credential."""

    def __init__(self, settings: Settings, client: httpx.Client) -> None:
        if (
            settings.node_name not in {"coire-edge-a", "coire-edge-b"}
            or not settings.node_control_host
            or not settings.node_token.get_secret_value()
            or client.follow_redirects
            or client._trust_env
        ):
            raise UpgradeUnavailable("Native recovery control transport is unavailable")
        self.settings = settings
        self.client = client
        self.base = httpx.URL(
            scheme="http", host=settings.node_control_host, port=settings.node_listen_port
        )
        self.core = httpx.URL(
            scheme="http",
            host=settings.core_control_host,
            port=settings.upgrade_recovery_core_api_port or settings.core_api_port,
        )

    def _read(
        self,
        path: str,
        model: type[GuardDocument],
        deadline_at: datetime,
        params: dict[str, int] | None = None,
        *,
        core: bool = False,
        method: Literal["GET", "POST"] = "GET",
        payload: UpgradeWire | None = None,
        timeout_s: float = 5.0,
    ) -> GuardDocument:
        remaining = (deadline_at - datetime.now(UTC)).total_seconds()
        if remaining <= 0:
            raise UpgradeUnavailable("Native recovery observation deadline elapsed")
        deadline = time.monotonic() + min(timeout_s, remaining)
        try:
            with self.client.stream(
                method,
                (self.core if core else self.base).copy_with(path=path),
                params=params,
                json=payload.model_dump(mode="json") if payload is not None else None,
                headers={
                    "Authorization": "Bearer " + self.settings.node_token.get_secret_value(),
                    **({"X-Coire-Node": self.settings.node_name} if core else {}),
                },
                timeout=min(timeout_s, remaining),
            ) as response:
                if response.status_code != 200:
                    raise UpgradeUnavailable("Native recovery owner observation is unavailable")
                data = bytearray()
                for chunk in response.iter_bytes():
                    if time.monotonic() >= deadline or len(data) + len(chunk) > 512 * 1024:
                        raise UpgradeUnavailable("Native recovery observation exceeds its bound")
                    data.extend(chunk)
                return model.model_validate_json(data)
        except (httpx.HTTPError, OSError, ValueError):
            raise UpgradeUnavailable("Native recovery observation cannot be verified") from None

    def verify_recovery_consumption(
        self, receipt: UpgradeExpiredRecoveryTransferConsumption
    ) -> UpgradeExpiredRecoveryTransferConsumption:
        """Fresh fixed-Core gate; never renew/recapture the consumed authority."""
        receipt = UpgradeExpiredRecoveryTransferConsumption.model_validate_json(
            receipt.model_dump_json()
        )
        if receipt.transfer.intent.original_plan.operation.node != self.settings.node_name:
            raise UpgradeUnavailable("Recovery consumption belongs to another node")
        body = UpgradeExpiredRecoveryConsumptionCheck(
            consumption_sha256=receipt.sha256(), consumer=receipt.consumer
        )
        observed = self._read(
            f"/api/v1/internal/upgrades/{receipt.transfer.intent.recovery_id}/recovery-consumption",
            UpgradeExpiredRecoveryTransferConsumption,
            receipt.transfer.expires_at,
            core=True,
            method="POST",
            payload=body,
        )
        if (
            observed.sha256() != receipt.sha256()
            or not observed.consumed_at <= datetime.now(UTC) < observed.transfer.expires_at
        ):
            raise UpgradeUnavailable("Recovery consumption response is stale or differs")
        return observed

    def verify_support_context(
        self, intent: UpgradeExpiredRecoveryIntent
    ) -> UpgradeExpiredRecoveryIntent:
        """Fresh startup scope, never renewed epoch or native effect authority."""
        intent = UpgradeExpiredRecoveryIntent.model_validate_json(intent.model_dump_json())
        if intent.original_plan.operation.node != self.settings.node_name:
            raise UpgradeUnavailable("Recovery support context belongs to another node")
        observed = self._read(
            f"/api/v1/internal/upgrades/{intent.recovery_id}/recovery-support-context",
            UpgradeExpiredRecoveryIntent,
            intent.deadline_at,
            core=True,
            method="POST",
            payload=UpgradeExpiredRecoverySupportContextRead(),
        )
        if (
            observed.sha256() != intent.sha256()
            or not intent.accepted_at <= datetime.now(UTC) < intent.deadline_at
        ):
            raise UpgradeUnavailable("Recovery support context is stale or differs")
        return observed

    def probe_recovery_consumption(
        self,
        intent: UpgradeExpiredRecoveryIntent,
        process: NodeAgentProcessIdentity,
    ) -> UpgradeExpiredRecoveryConsumptionState:
        """Fixed fresh delivery state; errors never become pending authority."""
        intent = UpgradeExpiredRecoveryIntent.model_validate_json(intent.model_dump_json())
        if (
            intent.original_plan.operation.node != self.settings.node_name
            or process.environment_sha256 != intent.support_release.manifest.environment_sha256
        ):
            raise UpgradeUnavailable("Recovery delivery actor or node differs")
        state = self._read(
            f"/api/v1/internal/upgrades/{intent.recovery_id}/recovery-consumption-delivery",
            UpgradeExpiredRecoveryConsumptionState,
            intent.deadline_at,
            core=True,
            method="POST",
            payload=UpgradeExpiredRecoveryConsumptionDeliveryRead(
                intent_sha256=intent.sha256(), consumer=process
            ),
        )
        now = datetime.now(UTC)
        if (
            state.intent_sha256 != intent.sha256()
            or not 0 <= (now - state.checked_at).total_seconds() <= 5
            or not intent.accepted_at <= now < intent.deadline_at
        ):
            raise UpgradeUnavailable("Recovery delivery state differs or is stale")
        if state.consumption is not None:
            receipt = state.consumption
            if (
                receipt.consumer != process
                or not receipt.consumed_at <= now < receipt.transfer.expires_at
            ):
                raise UpgradeUnavailable("Recovery delivery consumer or window differs")
        return state

    def probe_recovery_owner(
        self, claim: UpgradeExpiredRecoveryOwnerClaim
    ) -> UpgradeExpiredRecoveryOwnerState:
        """Explicit authenticated state; failure never permits initial fallback."""
        claim = UpgradeExpiredRecoveryOwnerClaim.model_validate_json(claim.model_dump_json())
        transfer = claim.consumption.transfer
        if transfer.intent.original_plan.operation.node != self.settings.node_name:
            raise UpgradeUnavailable("Recovery owner state belongs to another node")
        state = self._read(
            f"/api/v1/internal/upgrades/{transfer.intent.recovery_id}/recovery-owner-state",
            UpgradeExpiredRecoveryOwnerState,
            transfer.intent.deadline_at,
            core=True,
            method="POST",
            payload=UpgradeExpiredRecoveryOwnerCheck(
                owner_sha256=claim.sha256(), consumer=claim.process
            ),
        )
        now = datetime.now(UTC)
        if (
            state.owner_sha256 != claim.sha256()
            or not 0 <= (now - state.checked_at).total_seconds() <= 5
            or now >= transfer.intent.deadline_at
            or (state.owner is None and now >= transfer.expires_at)
        ):
            raise UpgradeUnavailable("Recovery owner state is stale or differs")
        return state

    def accept_recovery_owner(
        self,
        claim: UpgradeExpiredRecoveryOwnerClaim,
    ) -> UpgradeExpiredRecoveryOwnerClaim:
        """Retain one audited Core claim while caller holds physical/kernel lease."""
        claim = UpgradeExpiredRecoveryOwnerClaim.model_validate_json(claim.model_dump_json())
        transfer = claim.consumption.transfer
        if transfer.intent.original_plan.operation.node != self.settings.node_name:
            raise UpgradeUnavailable("Recovery owner belongs to another node")
        observed = self._read(
            f"/api/v1/internal/upgrades/{transfer.intent.recovery_id}/recovery-owner-claim",
            UpgradeExpiredRecoveryOwnerClaim,
            transfer.expires_at,
            core=True,
            method="POST",
            payload=claim,
        )
        if observed.sha256() != claim.sha256() or datetime.now(UTC) >= transfer.expires_at:
            raise UpgradeUnavailable("Recovery owner acceptance is stale or differs")
        return observed

    def verify_recovery_owner(
        self,
        claim: UpgradeExpiredRecoveryOwnerClaim,
    ) -> UpgradeExpiredRecoveryOwnerClaim:
        """Fixed authenticated Core owner gate; not local kernel lease proof."""
        claim = UpgradeExpiredRecoveryOwnerClaim.model_validate_json(claim.model_dump_json())
        intent = claim.consumption.transfer.intent
        if intent.original_plan.operation.node != self.settings.node_name:
            raise UpgradeUnavailable("Recovery owner belongs to another node")
        observed = self._read(
            f"/api/v1/internal/upgrades/{intent.recovery_id}/recovery-owner-check",
            UpgradeExpiredRecoveryOwnerClaim,
            intent.deadline_at,
            core=True,
            method="POST",
            payload=UpgradeExpiredRecoveryOwnerCheck(
                owner_sha256=claim.sha256(), consumer=claim.process
            ),
        )
        if (
            observed.sha256() != claim.sha256()
            or not claim.claimed_at <= datetime.now(UTC) < intent.deadline_at
        ):
            raise UpgradeUnavailable("Recovery owner response is stale or differs")
        return observed

    def accept_recovery_successor(
        self, claim: UpgradeExpiredRecoverySuccessorClaim
    ) -> UpgradeExpiredRecoverySuccessorClaim:
        """Initial fixed credentialed acceptance only; lost response uses state probe.

        Trusted native producer must hold actual lifetime exclusion/physical gates.
        A delayed acknowledgement does not renew this initial transfer window.
        """
        claim = UpgradeExpiredRecoverySuccessorClaim.model_validate_json(claim.model_dump_json())
        intent = claim.grant.observation.previous_owner.consumption.transfer.intent
        if intent.original_plan.operation.node != self.settings.node_name:
            raise UpgradeUnavailable("Successor claim belongs to another node")
        observed = self._read(
            f"/api/v1/internal/upgrades/{claim.grant.observation.succession_id}/recovery-successor-claim",
            UpgradeExpiredRecoverySuccessorClaim,
            claim.grant.expires_at,
            core=True,
            method="POST",
            payload=claim,
        )
        if (
            observed.sha256() != claim.sha256()
            or not claim.claimed_at <= datetime.now(UTC) < claim.grant.expires_at
        ):
            raise UpgradeUnavailable("Successor acceptance is stale or differs")
        return observed

    def verify_recovery_successor(
        self, claim: UpgradeExpiredRecoverySuccessorClaim
    ) -> UpgradeExpiredRecoverySuccessorClaim:
        """Fixed live Core metadata gate, not physical lifetime ownership."""
        claim = UpgradeExpiredRecoverySuccessorClaim.model_validate_json(claim.model_dump_json())
        intent = claim.grant.observation.previous_owner.consumption.transfer.intent
        if intent.original_plan.operation.node != self.settings.node_name:
            raise UpgradeUnavailable("Successor belongs to another node")
        observed = self._read(
            f"/api/v1/internal/upgrades/{claim.grant.observation.succession_id}/recovery-successor-check",
            UpgradeExpiredRecoverySuccessorClaim,
            intent.deadline_at,
            core=True,
            method="POST",
            payload=UpgradeExpiredRecoverySuccessorCheck(
                claim_sha256=claim.sha256(), consumer=claim.process
            ),
        )
        if (
            observed.sha256() != claim.sha256()
            or not claim.claimed_at <= datetime.now(UTC) < intent.deadline_at
        ):
            raise UpgradeUnavailable("Successor response is stale or differs")
        return observed

    def accept_cold_recovery_owner(
        self, claim: UpgradeExpiredRecoveryColdClaim
    ) -> UpgradeExpiredRecoveryColdClaim:
        observed = self._read(
            f"/api/v1/internal/upgrades/{claim.intent.recovery_id}/recovery-cold-claim",
            UpgradeExpiredRecoveryColdClaim,
            claim.intent.deadline_at,
            core=True,
            method="POST",
            payload=claim,
        )
        if observed != claim or datetime.now(UTC) >= claim.intent.deadline_at:
            raise UpgradeUnavailable("Cold claim acknowledgement differs or expired")
        return observed

    def verify_cold_recovery_owner(
        self, claim: UpgradeExpiredRecoveryColdClaim
    ) -> UpgradeExpiredRecoveryColdClaim:
        observed = self._read(
            f"/api/v1/internal/upgrades/{claim.intent.recovery_id}/recovery-cold-check",
            UpgradeExpiredRecoveryColdClaim,
            claim.intent.deadline_at,
            core=True,
            method="POST",
            payload=UpgradeExpiredRecoveryColdCheck(
                claim_sha256=claim.sha256(), consumer=claim.process
            ),
        )
        if observed != claim or datetime.now(UTC) >= claim.intent.deadline_at:
            raise UpgradeUnavailable("Cold owner response differs or expired")
        return observed

    def probe_cold_recovery_owner(
        self, claim: UpgradeExpiredRecoveryColdClaim
    ) -> UpgradeExpiredRecoveryColdState:
        observed = self._read(
            f"/api/v1/internal/upgrades/{claim.intent.recovery_id}/recovery-cold-state",
            UpgradeExpiredRecoveryColdState,
            claim.intent.deadline_at,
            core=True,
            method="POST",
            payload=UpgradeExpiredRecoveryColdCheck(
                claim_sha256=claim.sha256(), consumer=claim.process
            ),
        )
        now = datetime.now(UTC)
        if (
            observed.claim_sha256 != claim.sha256()
            or (observed.owner is not None and observed.owner != claim)
            or not 0 <= (now - observed.checked_at).total_seconds() <= 5
            or now >= claim.intent.deadline_at
        ):
            raise UpgradeUnavailable("Cold owner state differs or is stale")
        return observed

    def relay_expired_failure(self, journal: "NativeGuardJournal") -> None:
        from coire_core.models.upgrades import UpgradeReceiptAck, UpgradeReceiptBatch

        native = journal.current()
        latest = journal.receipts.latest()
        if native.phase != "recovery_required" or latest is None:
            return
        if journal.receipts.acknowledged_sequence() >= latest.sequence:
            return
        receipts = journal.receipts.page(max(0, latest.sequence - 32))
        body = NativeRecoveryFailureReplay(
            native=native, batch=UpgradeReceiptBatch(receipts=receipts)
        )
        ack = self._read(
            f"/api/v1/internal/upgrades/{native.job_id}/native-failure-receipts",
            UpgradeReceiptAck,
            datetime.now(UTC) + timedelta(seconds=5),
            core=True,
            method="POST",
            payload=body,
        )
        journal.receipts.acknowledge(ack)

    def publish_initial_recovery_observation(
        self, proof: UpgradeExpiredRecoveryObservation
    ) -> UpgradeExpiredRecoveryObservation:
        """Fixed authenticated metadata, never acceptance or executable authority."""
        proof = UpgradeExpiredRecoveryObservation.model_validate_json(proof.model_dump_json())
        if (
            proof.plan.operation.node != self.settings.node_name
            or not 0 <= (datetime.now(UTC) - proof.observed_at).total_seconds() <= 5
        ):
            raise UpgradeUnavailable("Initial recovery observation scope or freshness differs")
        acknowledged = self._read(
            f"/api/v1/internal/upgrades/{proof.request.original_job_id}/recovery-initial-observation",
            UpgradeExpiredRecoveryObservation,
            proof.observed_at + timedelta(seconds=5),
            core=True,
            method="POST",
            payload=proof,
        )
        if (
            acknowledged != proof
            or not 0 <= (datetime.now(UTC) - proof.observed_at).total_seconds() <= 5
        ):
            raise UpgradeUnavailable("Initial recovery acknowledgement differs or expired")
        return acknowledged

    def publish_recovery_succession_observation(
        self, proof: UpgradeExpiredRecoverySuccessionObservation
    ) -> UpgradeExpiredRecoverySuccessionObservation:
        """Fixed authenticated passive metadata; acknowledgement is not a grant."""
        proof = UpgradeExpiredRecoverySuccessionObservation.model_validate_json(
            proof.model_dump_json()
        )
        intent = proof.previous_owner.consumption.transfer.intent
        now = datetime.now(UTC)
        if (
            intent.original_plan.operation.node != self.settings.node_name
            or not 0 <= (now - proof.observed_at).total_seconds() <= 5
            or now >= intent.deadline_at
        ):
            raise UpgradeUnavailable("Succession observation scope or sample is stale")
        observed = self._read(
            f"/api/v1/internal/upgrades/{intent.recovery_id}/recovery-succession-observation",
            UpgradeExpiredRecoverySuccessionObservation,
            min(intent.deadline_at, proof.observed_at + timedelta(seconds=5)),
            core=True,
            method="POST",
            payload=proof,
        )
        now = datetime.now(UTC)
        if (
            observed != proof
            or not 0 <= (now - proof.observed_at).total_seconds() <= 5
            or now >= intent.deadline_at
        ):
            raise UpgradeUnavailable("Succession observation acknowledgement differs or expired")
        return observed

    def read_recovery_succession(
        self,
        previous: UpgradeExpiredRecoveryOwnerClaim,
        succession_id: str,
        successor: NodeAgentProcessIdentity,
    ) -> UpgradeExpiredRecoverySuccessionDeliveryState:
        """Fixed fresh passive metadata only, never absence-based takeover."""
        previous = UpgradeExpiredRecoveryOwnerClaim.model_validate_json(previous.model_dump_json())
        intent = previous.consumption.transfer.intent
        if intent.original_plan.operation.node != self.settings.node_name:
            raise UpgradeUnavailable("Succession delivery belongs to another node")
        body = UpgradeExpiredRecoverySuccessionDeliveryRead(
            succession_id=succession_id, predecessor_sha256=previous.sha256(), successor=successor
        )
        state = self._read(
            f"/api/v1/internal/upgrades/{intent.recovery_id}/recovery-succession-delivery",
            UpgradeExpiredRecoverySuccessionDeliveryState,
            intent.deadline_at,
            core=True,
            method="POST",
            payload=body,
        )
        now = datetime.now(UTC)
        if (
            state.request != body
            or not 0 <= (now - state.checked_at).total_seconds() <= 5
            or not previous.claimed_at <= now < intent.deadline_at
            or (
                state.grant is not None
                and (
                    state.grant.observation.previous_owner != previous
                    or now >= state.grant.expires_at
                )
            )
        ):
            raise UpgradeUnavailable("Succession delivery is stale or differs")
        return state

    def probe_recovery_successor(
        self, claim: UpgradeExpiredRecoverySuccessorClaim
    ) -> UpgradeExpiredRecoverySuccessorState:
        """Explicit fresh state; errors never authorize initial fallback."""
        claim = UpgradeExpiredRecoverySuccessorClaim.model_validate_json(claim.model_dump_json())
        intent = claim.grant.observation.previous_owner.consumption.transfer.intent
        if intent.original_plan.operation.node != self.settings.node_name:
            raise UpgradeUnavailable("Successor state belongs to another node")
        state = self._read(
            f"/api/v1/internal/upgrades/{claim.grant.observation.succession_id}/recovery-successor-state",
            UpgradeExpiredRecoverySuccessorState,
            intent.deadline_at,
            core=True,
            method="POST",
            payload=UpgradeExpiredRecoverySuccessorStateCheck(
                claim_sha256=claim.sha256(),
                consumer=claim.process,
                grant_sha256=claim.grant.sha256(),
            ),
        )
        now = datetime.now(UTC)
        if (
            state.claim_sha256 != claim.sha256()
            or not 0 <= (now - state.checked_at).total_seconds() <= 5
            or not claim.claimed_at <= now < intent.deadline_at
            or (state.owner is None and now >= claim.grant.expires_at)
        ):
            raise UpgradeUnavailable("Successor state is stale or differs")
        return state

    def collective(
        self,
        plan: UpgradeExecutionPlan,
        runtime_sha256: str,
        *,
        deadline_at: datetime,
        stage: Literal["candidate", "source"],
    ) -> NodeUpgradeCollectiveResult:
        if plan.operation.node != self.settings.node_name or plan.peer_runtime_sha256 is None:
            raise UpgradeConflict("Native collective authority differs")
        body = NodeUpgradeCollectiveRead(
            generation=plan.operation.generation,
            plan_sha256=plan.authority_sha256(),
            stage=stage,
            runtime_sha256=runtime_sha256,
            deadline_at=deadline_at,
        )
        result = self._read(
            f"/api/v1/internal/upgrades/{plan.operation.job_id}/collective",
            NodeUpgradeCollectiveResult,
            deadline_at,
            core=True,
            method="POST",
            payload=body,
            timeout_s=120.0,
        )
        if (
            result.job_id,
            result.generation,
            result.plan_sha256,
            result.stage,
            result.deadline_at,
        ) != (
            plan.operation.job_id,
            body.generation,
            body.plan_sha256,
            stage,
            deadline_at,
        ):
            raise UpgradeConflict("Native collective result authority differs")
        pair = (
            (runtime_sha256, plan.peer_runtime_sha256)
            if self.settings.node_name == "coire-edge-a"
            else (plan.peer_runtime_sha256, runtime_sha256)
        )
        for observation in result.observations:
            if (
                (observation.node_a, observation.node_b) != ("coire-edge-a", "coire-edge-b")
                or (observation.runtime_sha256_a, observation.runtime_sha256_b) != pair
                or observation.transport.value != "jaccl"
                or observation.observed_at >= deadline_at
            ):
                raise UpgradeConflict("Native collective result pair differs")
        return result

    def inventory(
        self,
        plan: UpgradeExecutionPlan,
        runtime_sha256: str,
        *,
        deadline_at: datetime | None = None,
    ) -> NodeUpgradeInventory:
        if plan.operation.node != self.settings.node_name or runtime_sha256 not in {
            plan.source.manifest.environment_sha256,
            plan.target.manifest.environment_sha256,
        }:
            raise UpgradeConflict("Native recovery inventory authority differs")
        snapshot = self._read(
            f"/node/maintenance/{plan.operation.job_id}/inventory",
            NodeUpgradeInventory,
            deadline_at or plan.maintenance.drain_deadline_at,
            {"generation": plan.operation.generation},
        )
        if (snapshot.job_id, snapshot.generation, snapshot.node, snapshot.runtime_sha256) != (
            plan.operation.job_id,
            plan.operation.generation,
            plan.operation.node,
            runtime_sha256,
        ) or not 0 <= (datetime.now(UTC) - snapshot.observed_at).total_seconds() <= 5:
            raise UpgradeUnavailable("Native recovery inventory identity or freshness differs")
        return snapshot

    def cleanup_inventories(
        self, body: NodeUpgradeCollectiveCleanup
    ) -> tuple[NodeUpgradeInventory, NodeUpgradeInventory]:
        from coire_node.collective_cleanup import verify_cleanup_inventories

        body = NodeUpgradeCollectiveCleanup.model_validate(body.model_dump(mode="json"))
        if self.settings.node_name != "coire-edge-a":
            raise UpgradeConflict("Collective cleanup observation requires rank zero")
        # This is a bounded observation budget, NOT a replacement probe epoch.
        # The captured request/deadline is sent unchanged; no worker is launched.
        deadline = datetime.now(UTC) + timedelta(seconds=5)
        scope = body.scope
        local = self._read(
            f"/node/maintenance/{scope.plan.operation.job_id}/inventory",
            NodeUpgradeInventory,
            deadline,
            {"generation": scope.request.generation},
        )
        peer = self._read(
            f"/api/v1/internal/upgrades/{scope.plan.operation.job_id}/collective-cleanup-peer",
            NodeUpgradeInventory,
            deadline,
            core=True,
            method="POST",
            payload=body,
        )
        verify_cleanup_inventories(scope, local, peer)
        return local, peer

    def status(self, plan: UpgradeExecutionPlan, *, deadline_at: datetime) -> NodeUpgradeStatus:
        if plan.operation.node != self.settings.node_name:
            raise UpgradeConflict("Native recovery node authority differs")
        status = self._read("/node/upgrades/status", NodeUpgradeStatus, deadline_at)
        if (
            status.node != plan.operation.node
            or not 0 <= (datetime.now(UTC) - status.observed_at).total_seconds() <= 5
        ):
            raise UpgradeUnavailable("Native recovery node observation differs")
        return status

    def verify_cold_recovery_smoke(
        self, smoke: UpgradeExpiredRecoveryColdSmokeCommand
    ) -> UpgradeExpiredRecoveryColdSmokeCommand:
        claim = smoke.source.launch.source.projection.owner
        observed = self._read(
            f"/api/v1/internal/upgrades/{claim.intent.recovery_id}/recovery-cold-smoke",
            UpgradeExpiredRecoveryColdSmokeCommand,
            claim.intent.deadline_at,
            core=True,
            method="POST",
            payload=smoke,
        )
        if observed != smoke:
            raise UpgradeUnavailable("Cold Source smoke authorization differs")
        return observed

    def prepare_cold_recovery_verification(
        self, body: UpgradeExpiredRecoveryColdVerificationRequest
    ) -> UpgradeExpiredRecoveryColdVerification:
        owner = body.smoke.smoke.source.launch.source.projection.owner
        observed = self._read(
            f"/api/v1/internal/upgrades/{owner.intent.recovery_id}/recovery-cold-verification",
            UpgradeExpiredRecoveryColdVerification,
            owner.intent.deadline_at,
            core=True,
            method="POST",
            payload=body,
        )
        if observed.request != body:
            raise UpgradeUnavailable("Cold Source verification acknowledgment differs")
        return observed

    def accept_cold_recovery_handback(
        self, handback: UpgradeExpiredRecoveryColdHandbackIntent
    ) -> UpgradeExpiredRecoveryColdHandbackIntent:
        return self._cold_handback(handback, "recovery-cold-handback")

    def verify_cold_recovery_handback(
        self, handback: UpgradeExpiredRecoveryColdHandbackIntent
    ) -> UpgradeExpiredRecoveryColdHandbackIntent:
        return self._cold_handback(handback, "recovery-cold-handback-check")

    def accept_cold_recovery_handback_receipt(
        self, receipt: UpgradeExpiredRecoveryColdHandbackReceipt
    ) -> UpgradeExpiredRecoveryColdHandbackReceipt:
        owner = receipt.helper.handback.verification.request.smoke.smoke.source.launch.source.projection.owner
        observed = self._read(
            f"/api/v1/internal/upgrades/{owner.intent.recovery_id}/recovery-cold-handback-receipt",
            UpgradeExpiredRecoveryColdHandbackReceipt,
            owner.intent.deadline_at,
            core=True,
            method="POST",
            payload=receipt,
        )
        if observed != receipt:
            raise UpgradeUnavailable("Cold Source handback receipt acknowledgment differs")
        return observed

    def resolve_cold_recovery(
        self, body: UpgradeExpiredRecoveryColdResolutionRequest
    ) -> UpgradeExpiredRecoveryColdResolution:
        closure = body.closure
        owner = closure.handback.helper.handback.verification.request.smoke.smoke.source.launch.source.projection.owner
        observed = self._read(
            f"/api/v1/internal/upgrades/{owner.intent.recovery_id}/recovery-cold-resolve",
            UpgradeExpiredRecoveryColdResolution,
            owner.intent.deadline_at,
            core=True,
            method="POST",
            payload=body,
        )
        if observed.closure != closure:
            raise UpgradeUnavailable("Cold resolution acknowledgment changes retained closure")
        return observed

    def cold_resolution_state(
        self, recovery_id: str, body: UpgradeExpiredRecoveryColdResolutionRead
    ) -> UpgradeExpiredRecoveryColdResolutionState:
        # Five seconds for metadata transport only; never change the effect
        # intent or reauthorize an expired pending recovery.
        observed = self._read(
            f"/api/v1/internal/upgrades/{recovery_id}/recovery-cold-resolution-state",
            UpgradeExpiredRecoveryColdResolutionState,
            datetime.now(UTC) + timedelta(seconds=5),
            core=True,
            method="POST",
            payload=body,
        )
        if (
            observed.intent_sha256 != body.intent_sha256
            or observed.closure_sha256 != body.closure_sha256
            or not 0 <= (datetime.now(UTC) - observed.checked_at).total_seconds() <= 5
        ):
            raise UpgradeUnavailable("Cold resolution metadata state is stale or foreign")
        return observed

    def _cold_handback(
        self,
        handback: UpgradeExpiredRecoveryColdHandbackIntent,
        action: Literal["recovery-cold-handback", "recovery-cold-handback-check"],
    ) -> UpgradeExpiredRecoveryColdHandbackIntent:
        owner = handback.verification.request.smoke.smoke.source.launch.source.projection.owner
        observed = self._read(
            f"/api/v1/internal/upgrades/{owner.intent.recovery_id}/{action}",
            UpgradeExpiredRecoveryColdHandbackIntent,
            owner.intent.deadline_at,
            core=True,
            method="POST",
            payload=handback,
        )
        if observed != handback:
            raise UpgradeUnavailable("Cold Source handback authority differs")
        return observed

    def verify_cold_recovery_verification(
        self, proof: UpgradeExpiredRecoveryColdVerification
    ) -> UpgradeExpiredRecoveryColdVerification:
        owner = proof.request.smoke.smoke.source.launch.source.projection.owner
        observed = self._read(
            f"/api/v1/internal/upgrades/{owner.intent.recovery_id}/recovery-cold-verification-check",
            UpgradeExpiredRecoveryColdVerification,
            owner.intent.deadline_at,
            core=True,
            method="POST",
            payload=UpgradeExpiredRecoveryColdVerificationRead(
                verification_sha256=proof.sha256(), consumer=owner.process
            ),
        )
        if observed != proof:
            raise UpgradeUnavailable("Cold Source verification live authority differs")
        return observed

    def read_cold_recovery_continuation(
        self, intent: UpgradeExpiredRecoveryIntent, *, deadline_at: datetime | None = None
    ) -> UpgradeColdContinuationGrant | None:
        from coire_core.models.upgrade_recovery_continuation import (
            UpgradeColdContinuationBindingRead,
            UpgradeColdContinuationBindingState,
        )

        state = self._read(
            f"/api/v1/internal/upgrades/{intent.recovery_id}/recovery-cold-continuation-binding",
            UpgradeColdContinuationBindingState,
            min(intent.deadline_at, deadline_at) if deadline_at is not None else intent.deadline_at,
            core=True,
            method="POST",
            payload=UpgradeColdContinuationBindingRead(intent_sha256=intent.sha256()),
        )
        if (
            state.intent_sha256 != intent.sha256()
            or not 0 <= (datetime.now(UTC) - state.observed_at).total_seconds() <= 5
        ):
            raise UpgradeUnavailable("Cold continuation binding is stale or differs")
        if state.grant is not None and state.grant.intent != intent:
            raise UpgradeConflict("Cold continuation binding changes captured intent")
        return state.grant

    def local_inventory(
        self, plan: UpgradeExecutionPlan, *, deadline_at: datetime
    ) -> NodeUpgradeInventory:
        snapshot = self._read(
            f"/node/maintenance/{plan.operation.job_id}/inventory",
            NodeUpgradeInventory,
            deadline_at,
            params={"generation": plan.operation.generation},
        )
        if (snapshot.job_id, snapshot.generation, snapshot.node) != (
            plan.operation.job_id,
            plan.operation.generation,
            self.settings.node_name,
        ) or not 0 <= (datetime.now(UTC) - snapshot.observed_at).total_seconds() <= 5:
            raise UpgradeUnavailable("Local recovery inventory is stale or foreign")
        return snapshot

    def peer_inventory(
        self, plan: UpgradeExecutionPlan, *, deadline_at: datetime
    ) -> NodeUpgradeInventory:
        if plan.operation.node != self.settings.node_name or plan.peer_runtime_sha256 is None:
            raise UpgradeConflict("Native recovery peer authority differs")
        peer = "coire-edge-b" if self.settings.node_name == "coire-edge-a" else "coire-edge-a"
        if peer not in plan.maintenance.targets:
            raise UpgradeConflict("Native recovery peer is outside accepted maintenance")
        snapshot = self._read(
            f"/api/v1/internal/upgrades/{plan.operation.job_id}/peer-inventory",
            NodeUpgradeInventory,
            deadline_at,
            core=True,
            method="POST",
            payload=NodeUpgradePeerRead(
                generation=plan.operation.generation, plan_sha256=plan.authority_sha256()
            ),
        )
        if (snapshot.job_id, snapshot.generation, snapshot.node, snapshot.runtime_sha256) != (
            plan.operation.job_id,
            plan.operation.generation,
            peer,
            plan.peer_runtime_sha256,
        ) or not 0 <= (datetime.now(UTC) - snapshot.observed_at).total_seconds() <= 5:
            raise UpgradeUnavailable("Native recovery peer identity or freshness differs")
        return snapshot


def publish_guard_status(
    settings: Settings, process: NodeAgentProcessIdentity
) -> NativeGuardStatus:
    """Publish only a durable, fully verified selection from the independent owner."""
    from coire_node.upgrade_control import NativeUpgradeControl
    from coire_node.upgrade_runtime import verify_prepared_runtime

    if (
        not settings.upgrades_enabled
        or settings.node_name not in {"coire-edge-a", "coire-edge-b"}
        or not is_darwin(sys.platform)
        or platform.machine() != "arm64"
        or process.pid != os.getpid()
        or psutil.Process(os.getpid()).create_time() != process.create_time_s
    ):
        raise UpgradeUnavailable("Independent recovery owner identity differs")
    root = PrivateUpgradeStore(Path(settings.upgrade_bootstrap_root))
    bootstrap = root.read("runtime.json", PreparedNativeRuntime)
    baseline = root.read("selection.json", NativeRuntimeSelection)
    if bootstrap is None or baseline is None or baseline.node != settings.node_name:
        raise UpgradeUnavailable("Independent recovery bootstrap is incomplete")
    envs = Path(settings.upgrade_environment_root)
    python = verify_prepared_runtime(envs, envs.parent / "runtimes", bootstrap)
    if (
        bootstrap.environment_sha256 != process.environment_sha256
        or Path(sys.executable).resolve(strict=True) != python.resolve(strict=True)
        or Path(sys.prefix).resolve(strict=True)
        != (envs / bootstrap.environment_id).resolve(strict=True)
    ):
        raise UpgradeUnavailable("Independent recovery interpreter differs")
    claim = root.read("claim.json", NativeUpgradeClaim)
    selected = baseline
    ready = True
    intent: NativeRecoveryIntent | None = None
    journal: NativeGuardJournal | None = None
    if claim is not None and claim.state == "active":
        journal = NativeUpgradeControl(settings).journal(claim.job_id)
        intent = journal.current()
        if intent.phase in {"activation_intent", "restoring", "recovery_required"}:
            ready = False
        elif intent.phase in {
            "candidate_selected",
            "candidate_verified",
            "committed",
            "source_selected",
            "source_verified",
        }:
            pointer = journal.root.root / "pointer"
            if not pointer.exists():
                raise UpgradeUnavailable("Native recovery selection is incomplete")
            selected_value = PrivateUpgradeStore(pointer).read(
                "selection.json", NativeRuntimeSelection
            )
            candidate = intent.phase in {"candidate_selected", "candidate_verified", "committed"}
            expected = intent.candidate if candidate else intent.source.runtime
            expected_release = (
                journal.plan.target.manifest_sha256 if candidate else intent.source.release_sha256
            )
            if selected_value is None or (
                selected_value.node,
                selected_value.job_id,
                selected_value.generation,
                selected_value.runtime,
                selected_value.release_sha256,
            ) != (settings.node_name, claim.job_id, claim.generation, expected, expected_release):
                raise UpgradeUnavailable("Native recovery selected authority differs")
            selected = selected_value
        else:
            selected = intent.source
    if selected.runtime != bootstrap:
        verify_prepared_runtime(envs, envs.parent / "runtimes", selected.runtime)
    if ready and (envs / "current").resolve(strict=True) != (
        envs / selected.runtime.environment_id
    ).resolve(strict=True):
        raise UpgradeUnavailable("Native recovery pointer differs from durable selection")
    if (
        root.read("runtime.json", PreparedNativeRuntime) != bootstrap
        or root.read("selection.json", NativeRuntimeSelection) != baseline
        or root.read("claim.json", NativeUpgradeClaim) != claim
        or (journal is not None and journal.current() != intent)
    ):
        raise UpgradeUnavailable("Native recovery authority changed during verification")
    status = NativeGuardStatus(
        node=settings.node_name,
        bootstrap_runtime=bootstrap,
        process=process,
        selected=selected,
        ready=ready,
        observed_at=datetime.now(UTC),
    )
    root.write("status.json", status)
    return status


def _owned_node_process(
    identity: NodeAgentProcessIdentity, executable: Path
) -> psutil.Process | None:
    return _owned_native_process(identity, executable, "coire_node")


def _owned_native_process(
    identity: NodeAgentProcessIdentity, executable: Path, module: str
) -> psutil.Process | None:
    try:
        process = psutil.Process(identity.pid)
        if (
            process.create_time() != identity.create_time_s
            or not process.is_running()
            or process.status() == psutil.STATUS_ZOMBIE
        ):
            return None
        users = process.uids()
        if (
            users.real != os.getuid()
            or users.effective != os.getuid()
            or Path(process.exe()).resolve(strict=True) != executable.resolve(strict=True)
            or process.cmdline()[-2:] != ["-m", module]
        ):
            raise UpgradeUnavailable("Native node process identity is uncertain")
        return process
    except psutil.NoSuchProcess:
        return None
    except (psutil.Error, OSError, ValueError):
        raise UpgradeUnavailable("Native node process identity is unreadable") from None


def verify_node_startup(settings: Settings) -> NativeRuntimeSelection | None:
    """Fail before registration/workload resumption when native recovery is unresolved."""
    if not settings.upgrades_enabled:
        return None
    with tracer.start_as_current_span(
        "coire.node.upgrade_startup_verify", record_exception=False, set_status_on_exception=False
    ):
        try:
            return _verify_node_startup(settings)
        except CoireError:
            raise
        except (OSError, ValueError):
            raise UpgradeUnavailable("Native startup recovery data cannot be verified") from None


def _verify_node_startup(settings: Settings) -> NativeRuntimeSelection:
    if (
        settings.node_name not in {"coire-edge-a", "coire-edge-b"}
        or not is_darwin(sys.platform)
        or platform.machine() != "arm64"
    ):
        raise UpgradeUnavailable("Managed node startup requires the intended native Studio")
    from coire_node.upgrade_runtime import verify_prepared_runtime

    root = PrivateUpgradeStore(Path(settings.upgrade_bootstrap_root))
    status = root.read("status.json", NativeGuardStatus)
    bootstrap_runtime = root.read("runtime.json", PreparedNativeRuntime)
    now = datetime.now(UTC)
    if (
        status is None
        or bootstrap_runtime is None
        or status.bootstrap_runtime != bootstrap_runtime
        or not status.ready
        or status.node != settings.node_name
        or not 0 <= (now - status.observed_at).total_seconds() <= 5
    ):
        raise UpgradeUnavailable("Independent native recovery guard is not ready")
    envs = Path(settings.upgrade_environment_root)
    interpreters = envs.parent / "runtimes"
    guard_python = verify_prepared_runtime(envs, interpreters, status.bootstrap_runtime)
    if _owned_native_process(status.process, guard_python, "coire_node.upgrade_guard") is None:
        raise UpgradeUnavailable("Independent native recovery guard is absent")
    selected = status.selected
    fence = PrivateUpgradeStore(Path(settings.node_state_dir) / "upgrades").read(
        "maintenance.json", UpgradeMaintenance
    )
    job_id = fence.job_id if fence is not None and fence.state != "released" else selected.job_id
    operation_path = root.root / "operations" / job_id if job_id is not None else None
    has_local_operation = operation_path is not None and (
        operation_path.exists() or operation_path.is_symlink()
    )
    intent_store: PrivateUpgradeStore | None = None
    intent: NativeRecoveryIntent | None = None
    unclaimed_plan: UpgradeExecutionPlan | None = None
    unclaimed_owner: NativeUpgradeClaim | None = None
    if job_id is not None and has_local_operation:
        operations = PrivateUpgradeStore(root.root / "operations")
        operation_store = PrivateUpgradeStore(operations.path(job_id))
        plan = operation_store.read("plan.json", UpgradeExecutionPlan)
        intent_store = PrivateUpgradeStore(operation_store.root / "guard")
        intent = intent_store.read("intent.json", NativeRecoveryIntent)
        if plan is None:
            raise UpgradeUnavailable("Native startup operation authority is incomplete")
        if intent is None:
            # Preparation can crash after retaining the plan but before publishing
            # the guard intent. Restart the positively selected Source under the
            # existing fence so its authenticated route can reconcile preparation.
            # An accepted claim or any changed selection remains blocking.
            unclaimed_owner = root.read("claim.json", NativeUpgradeClaim)
            baseline = root.read("selection.json", NativeRuntimeSelection)
            if (
                (unclaimed_owner is not None and unclaimed_owner.state == "active")
                or operation_store.read("accepted.json", NativeUpgradeClaim) is not None
                or baseline != selected
                or plan.operation.node != settings.node_name
                or plan.operation.job_id != job_id
                or fence is None
                or fence.state == "released"
                or (fence.job_id, fence.generation)
                != (plan.operation.job_id, plan.operation.generation)
                or selected.release_sha256 != plan.source.manifest_sha256
                or (selected.runtime.environment_id, selected.runtime.environment_sha256)
                != (plan.source.manifest.environment_id, plan.source.manifest.environment_sha256)
            ):
                raise UpgradeUnavailable("Unclaimed native preparation cannot verify its Source")
            unclaimed_plan = plan
        elif (intent.job_id, intent.generation, intent.plan_sha256) != (
            plan.operation.job_id,
            plan.operation.generation,
            plan.authority_sha256(),
        ):
            raise UpgradeUnavailable("Native startup operation authority is incomplete")
        if (
            intent is not None
            and fence is not None
            and fence.state != "released"
            and (fence.job_id, fence.generation) != (intent.job_id, intent.generation)
        ):
            raise UpgradeUnavailable("Native startup maintenance epoch differs")
        if intent is not None and intent.phase in {
            "activation_intent",
            "restoring",
            "recovery_required",
        }:
            raise UpgradeUnavailable("Native selection effect is unresolved")
        if intent is not None:
            target = intent.phase in {"candidate_selected", "candidate_verified", "committed"}
            release = plan.target if target else plan.source
            expected = intent.candidate if target else intent.source.runtime
            if selected.runtime != expected or selected.release_sha256 != release.manifest_sha256:
                raise UpgradeUnavailable("Native startup selection differs from recovery intent")
    else:
        # A fenced peer has no local selection operation. Its independent source
        # receipt permits the agent to reconcile existing owners while the
        # persistent admission fence still prevents new work.
        retained = root.read("selection.json", NativeRuntimeSelection)
        if retained != selected:
            raise UpgradeUnavailable("Native bootstrap selection lacks an independent receipt")
    python = verify_prepared_runtime(envs, interpreters, selected.runtime)
    if (
        Path(sys.executable).resolve(strict=True) != python.resolve(strict=True)
        or Path(sys.prefix).resolve(strict=True)
        != (envs / selected.runtime.environment_id).resolve(strict=True)
        or (envs / "current").resolve(strict=True)
        != (envs / selected.runtime.environment_id).resolve(strict=True)
    ):
        raise UpgradeUnavailable("Node startup uses a different selected native runtime")
    # Recheck the independent owner after potentially expensive graph verification.
    latest = root.read("status.json", NativeGuardStatus)
    if (
        latest is None
        or not latest.ready
        or latest.selected != selected
        or latest.process != status.process
        or not 0 <= (datetime.now(UTC) - latest.observed_at).total_seconds() <= 5
    ):
        raise UpgradeUnavailable("Native startup recovery ownership changed during verification")
    latest_fence = PrivateUpgradeStore(Path(settings.node_state_dir) / "upgrades").read(
        "maintenance.json", UpgradeMaintenance
    )
    if latest_fence != fence or (
        intent_store is not None
        and intent_store.read("intent.json", NativeRecoveryIntent) != intent
    ):
        raise UpgradeUnavailable("Native startup operation changed during verification")
    if unclaimed_plan is not None:
        assert operation_path is not None
        operation = PrivateUpgradeStore(operation_path)
        if (
            operation.read("plan.json", UpgradeExecutionPlan) != unclaimed_plan
            or operation.read("accepted.json", NativeUpgradeClaim) is not None
            or root.read("claim.json", NativeUpgradeClaim) != unclaimed_owner
            or root.read("selection.json", NativeRuntimeSelection) != selected
        ):
            raise UpgradeUnavailable("Native preparation was accepted during startup verification")
    if (
        not has_local_operation
        and operation_path is not None
        and (operation_path.exists() or operation_path.is_symlink())
    ):
        raise UpgradeUnavailable("Native startup operation appeared during verification")
    if _owned_native_process(latest.process, guard_python, "coire_node.upgrade_guard") is None:
        raise UpgradeUnavailable("Native startup independent recovery owner exited")
    return selected


def record_node_process(settings: Settings, selected: NativeRuntimeSelection) -> NativeNodeProcess:
    """Capture this node's own PID after startup verification, never a caller-supplied PID."""
    if settings.node_name != selected.node or selected.node not in {"coire-edge-a", "coire-edge-b"}:
        raise UpgradeUnavailable("Native node process registration identity differs")
    process = psutil.Process(os.getpid())
    value = NativeNodeProcess(
        selected=selected,
        process=NodeAgentProcessIdentity(
            pid=process.pid,
            create_time_s=process.create_time(),
            environment_sha256=selected.runtime.environment_sha256,
        ),
        recorded_at=datetime.now(UTC),
    )
    PrivateUpgradeStore(Path(settings.node_state_dir) / "upgrades").write(
        "node-process.json", value
    )
    return value


def stop_owned_node_process(
    identity: NodeAgentProcessIdentity,
    executable: Path,
    deadline_at: datetime,
) -> bool:
    """Stop only the exact node PID after its recovery owner proves fenced idle.

    No process group, engine, forced kill or resource-release effect is performed.
    An uncertain process or unconfirmed stop leaves recovery ownership intact.
    """
    with tracer.start_as_current_span(
        "coire.node.upgrade_guard_node_stop", record_exception=False, set_status_on_exception=False
    ):
        process = _owned_node_process(identity, executable)
        if process is None:
            return True
        remaining = min(30.0, (deadline_at - datetime.now(UTC)).total_seconds())
        if remaining <= 0:
            raise UpgradeUnavailable("Native node stop deadline has elapsed")
        deadline = time.monotonic() + remaining
        try:
            # psutil rechecks its cached PID birth identity before sending SIGTERM.
            process.terminate()
        except psutil.NoSuchProcess:
            return True
        except psutil.Error:
            raise UpgradeUnavailable("Native node stop remains uncertain") from None
        while time.monotonic() < deadline:
            if _owned_node_process(identity, executable) is None:
                return True
            time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
        return _owned_node_process(identity, executable) is None


class NativeGuardJournal:
    def __init__(
        self,
        root: Path,
        plan: UpgradeExecutionPlan,
        source: NativeRuntimeSelection,
        candidate: PreparedNativeRuntime,
        process: NodeAgentProcessIdentity,
    ) -> None:
        if (
            plan.operation.kind is not UpgradeKind.STUDIO
            or plan.operation.node != source.node
            or source.release_sha256 != plan.source.manifest_sha256
            or (source.runtime.environment_id, source.runtime.environment_sha256)
            != (plan.source.manifest.environment_id, plan.source.manifest.environment_sha256)
            or (candidate.environment_id, candidate.environment_sha256)
            != (plan.target.manifest.environment_id, plan.target.manifest.environment_sha256)
            or process.environment_sha256 != source.runtime.environment_sha256
        ):
            raise UpgradeConflict(
                "Native guard source or candidate differs from accepted authority"
            )
        self.root = PrivateUpgradeStore(root)
        self.plan = plan
        self.receipts = UpgradeReceiptJournal(self.root.root / "receipts", plan)
        self.store = PrivateUpgradeStore(self.root.root / "guard")
        with (
            tracer.start_as_current_span(
                "coire.node.upgrade_guard_prepare",
                record_exception=False,
                set_status_on_exception=False,
            ),
            self.store.ownership("intent.lock", wait_s=1),
        ):
            previous = self.store.read("intent.json", NativeRecoveryIntent)
            value = NativeRecoveryIntent(
                job_id=plan.operation.job_id,
                generation=plan.operation.generation,
                plan_sha256=plan.authority_sha256(),
                source=source,
                candidate=candidate,
                source_process=process,
                updated_at=datetime.now(UTC),
            )
            if previous is None:
                self.store.write("intent.json", value, replace=False)
                self._record("prepared")
            elif any(
                getattr(previous, field) != getattr(value, field)
                for field in (
                    "job_id",
                    "generation",
                    "plan_sha256",
                    "source",
                    "candidate",
                    "source_process",
                )
            ):
                raise UpgradeConflict("Native recovery journal retains different authority")

    def _record(self, phase: str) -> None:
        intent_count.add(1, {"node": self.plan.operation.node or "unknown", "phase": phase})
        logger.info(
            "native upgrade guard intent persisted",
            extra={
                "job_id": self.plan.operation.job_id,
                "generation": self.plan.operation.generation,
                "node": self.plan.operation.node,
                "phase": phase,
            },
        )

    def current(self) -> NativeRecoveryIntent:
        value = self.store.read("intent.json", NativeRecoveryIntent)
        if value is None or (value.job_id, value.generation, value.plan_sha256) != (
            self.plan.operation.job_id,
            self.plan.operation.generation,
            self.plan.authority_sha256(),
        ):
            raise UpgradeUnavailable("Native recovery authority is absent or changed")
        return value

    def refresh_source_process(
        self, process: NodeAgentProcessIdentity, python: Path
    ) -> NativeRecoveryIntent:
        """Reconcile an unclaimed preparation after its exact old node exited."""
        with self.store.ownership("intent.lock"):
            current = self.current()
            if current.source_process == process:
                return current
            if (
                current.phase != "prepared"
                or self.receipts.latest() is not None
                or self.store.read("activation.json", NodeUpgradeCommand) is not None
            ):
                raise UpgradeConflict("Native preparation process cannot change after effects")
            if (
                process.environment_sha256 != current.source.runtime.environment_sha256
                or _owned_node_process(current.source_process, python) is not None
            ):
                raise UpgradeUnavailable("Previous native preparation process remains owned")
            value = NativeRecoveryIntent.model_validate(
                current.model_dump(mode="json")
                | {
                    "source_process": process,
                    "updated_at": datetime.now(UTC),
                }
            )
            self.store.write("intent.json", value)
            self._record("prepared")
            return value

    def record_activation(
        self,
        command: NodeUpgradeCommand,
        *,
        now: datetime | None = None,
        recovery_only: bool = False,
    ) -> NativeRecoveryIntent:
        """Retain Core's consumed intent and immutable deadlines before any pointer effect.

        A matching replay observes the existing intent; it never renews a deadline or
        grants permission to repeat a process/selection effect.
        """
        command = NodeUpgradeCommand.model_validate(command.model_dump(mode="json"))
        grant, receipt = command.activation_grant, command.activation_receipt
        if (
            command.authority_sha256() != self.plan.authority_sha256()
            or grant is None
            or receipt is None
        ):
            raise UpgradeConflict("Native activation needs accepted consumed authority")
        with (
            tracer.start_as_current_span(
                "coire.node.upgrade_guard_activation",
                record_exception=False,
                set_status_on_exception=False,
            ),
            self.store.ownership("intent.lock"),
        ):
            current = self.current()
            saved = self.store.read("activation.json", NodeUpgradeCommand)
            if saved is not None and (
                saved.authority_sha256() != command.authority_sha256()
                or saved.activation_grant != grant
                or saved.activation_receipt != receipt
                or saved.peer_inventory != command.peer_inventory
            ):
                raise UpgradeConflict("Native activation replay differs")
            if current.activation_grant_id is not None:
                if current.activation_grant_id != grant.grant_id or saved is None:
                    raise UpgradeConflict("Native activation is already owned")
                self.receipts.append(receipt)
                return current
            time = now or datetime.now(UTC)
            deadline = receipt.occurred_at + timedelta(
                seconds=self.plan.operation.timing.readiness_s
            )
            if (
                current.phase != "prepared"
                or receipt.occurred_at > time
                or (
                    not recovery_only
                    and (
                        not time < grant.expires_at <= time + timedelta(seconds=30)
                        or time >= deadline
                    )
                )
            ):
                raise UpgradeConflict(
                    "Native activation is expired or outside its captured deadline"
                )
            latest = self.receipts.latest()
            if (
                latest is None
                or latest.phase is not UpgradePhase.SMOKE_BEFORE
                or latest.outcome != "complete"
                or not latest.evidence
            ):
                raise UpgradeConflict("Native activation requires durable candidate smoke")
            proof = UpgradeEvidence.model_validate_json(latest.evidence)
            if (
                latest.observed_sha256 != self.plan.operation.source_sha256
                or receipt.sequence != latest.sequence + 1
                or proof.runtime_sha256 != current.candidate.environment_sha256
                or proof.model_id != self.plan.model_id
                or proof.model_variant_id != self.plan.model_variant_id
                or proof.model_manifest_sha256 != self.plan.model_manifest_sha256
                or proof.generated_tokens is None
                or proof.generated_tokens < 10
                or proof.output_sha256 is None
                or proof.check != "node"
                or proof.cleanup_verified is not True
            ):
                raise UpgradeConflict("Native candidate smoke or cleanup authority differs")
            if saved is None:
                self.store.write("activation.json", command, replace=False)
            recovery_at = min(time, grant.expires_at) if recovery_only else None
            recovery_deadline = (
                recovery_at + timedelta(seconds=self.plan.operation.timing.recovery_s)
                if recovery_at is not None
                else None
            )
            aborted = self.store.read("abort.json", NativeAbortIntent)
            if recovery_only and aborted is not None:
                if recovery_at is None or recovery_deadline is None:
                    raise UpgradeUnavailable("Native recovery capture is incomplete")
                if (aborted.job_id, aborted.generation, aborted.plan_sha256) != (
                    current.job_id,
                    current.generation,
                    current.plan_sha256,
                ) or aborted.captured_at < receipt.occurred_at:
                    raise UpgradeConflict("Native consumed intent differs from captured abort")
                recovery_at = min(recovery_at, aborted.captured_at)
                recovery_deadline = min(recovery_deadline, aborted.deadline_at)
            value = NativeRecoveryIntent.model_validate(
                current.model_dump(mode="json")
                | {
                    "phase": "restoring" if recovery_only else "activation_intent",
                    "activation_grant_id": grant.grant_id,
                    "activation_started_at": receipt.occurred_at,
                    "readiness_deadline_at": deadline,
                    "recovery_started_at": recovery_at,
                    "recovery_deadline_at": recovery_deadline,
                    "updated_at": time,
                }
            )
            self.store.write("intent.json", value)
            self.receipts.append(receipt)
            self._record(value.phase)
            return value

    def begin_restoration(self, *, now: datetime | None = None) -> NativeRecoveryIntent:
        """Capture restoration once; restarts cannot extend the recovery budget."""
        with (
            tracer.start_as_current_span(
                "coire.node.upgrade_guard_restore",
                record_exception=False,
                set_status_on_exception=False,
            ),
            self.store.ownership("intent.lock"),
        ):
            current = self.current()
            if current.activation_started_at is None:
                raise UpgradeConflict("Unactivated source needs unchanged reconciliation")
            if current.phase in {"committed", "unchanged_verified"}:
                raise UpgradeConflict("Committed native ownership cannot restore a new target")
            if current.recovery_started_at is not None:
                return current
            time = now or datetime.now(UTC)
            if time < current.activation_started_at:
                raise UpgradeConflict("Native recovery clock precedes activation")
            latest = self.receipts.latest()
            verified = latest is not None and (
                (latest.phase is UpgradePhase.VERIFYING and latest.outcome == "complete")
                or latest.phase is UpgradePhase.COMMITTING
            )
            if not verified and current.readiness_deadline_at is not None:
                # A worker returning late from candidate I/O, or restarting after
                # expiry, cannot manufacture a new restoration budget. Positive
                # serving proof has a separate cancellation/reconciliation path.
                time = min(time, current.readiness_deadline_at)
            value = NativeRecoveryIntent.model_validate(
                current.model_dump(mode="json")
                | {
                    "phase": "restoring",
                    "recovery_started_at": time,
                    "recovery_deadline_at": time
                    + timedelta(seconds=self.plan.operation.timing.recovery_s),
                    "updated_at": time,
                }
            )
            self.store.write("intent.json", value)
            self._record("restoring")
            return value


@tracer.start_as_current_span(
    "coire.node.upgrade_guard_delegate", record_exception=False, set_status_on_exception=False
)
def retained_guard_delegate(settings: Settings) -> PreparedNativeRuntime | None:
    """Resolve a fixed launcher into its durably qualified Source recovery owner."""
    from coire_node.upgrade_control import NativeUpgradeControl
    from coire_node.upgrade_runtime import verify_prepared_runtime

    if not settings.upgrades_enabled or settings.node_name not in {"coire-edge-a", "coire-edge-b"}:
        raise UpgradeUnavailable("Native recovery delegation requires an enabled Studio")
    root = PrivateUpgradeStore(Path(settings.upgrade_bootstrap_root))
    with root.ownership("control.lock"), root.ownership("lifetime.lock"):
        runtime = root.read("runtime.json", PreparedNativeRuntime)
        launcher = root.read("launcher-runtime.json", PreparedNativeRuntime)
        if runtime is None:
            raise UpgradeUnavailable("Native recovery bootstrap is absent")
        envs = Path(settings.upgrade_environment_root)
        python = verify_prepared_runtime(envs, envs.parent / "runtimes", runtime)
        if Path(sys.prefix).resolve(strict=True) == (envs / runtime.environment_id).resolve(
            strict=True
        ) and Path(sys.executable).resolve(strict=True) == python.resolve(strict=True):
            return None
        if launcher is None:
            raise UpgradeUnavailable("Native recovery launcher is absent")
        launcher_python = verify_prepared_runtime(envs, envs.parent / "runtimes", launcher)
        if Path(sys.prefix).resolve(strict=True) != (envs / launcher.environment_id).resolve(
            strict=True
        ) or Path(sys.executable).resolve(strict=True) != launcher_python.resolve(strict=True):
            raise UpgradeUnavailable("Native recovery launcher interpreter differs")
        refresh = root.read("bootstrap-refresh.json", NativeBootstrapRefresh)
        if refresh is None or refresh.state != "committed" or refresh.selected.runtime != runtime:
            raise UpgradeUnavailable("Native recovery delegation has no committed Source proof")
        assert refresh.selected.job_id is not None
        journal = NativeUpgradeControl(settings).journal(refresh.selected.job_id)
        latest = journal.receipts.latest()
        if (
            latest is None
            or latest.phase not in TERMINAL_UPGRADE_PHASES
            or latest.outcome != "complete"
            or latest.observed_sha256 != refresh.selected.release_sha256
        ):
            raise UpgradeUnavailable("Native recovery delegation Source history differs")
        evidence = UpgradeEvidence.model_validate_json(latest.evidence)
        if evidence.runtime_sha256 != runtime.environment_sha256 or not evidence.cleanup_verified:
            raise UpgradeUnavailable("Native recovery delegation Source cleanup is unverified")
        claim = root.read("claim.json", NativeUpgradeClaim)
        if claim is not None and claim.state == "active":
            active = NativeUpgradeControl(settings).journal(claim.job_id)
            if active.plan.source.manifest.environment_sha256 != runtime.environment_sha256:
                raise UpgradeConflict("Native recovery delegation active Source differs")
        return runtime


@tracer.start_as_current_span(
    "coire.node.upgrade_guard_refresh", record_exception=False, set_status_on_exception=False
)
def refresh_running_guard(
    settings: Settings, process: NodeAgentProcessIdentity
) -> PreparedNativeRuntime | None:
    """Move an idle owner to qualified current Source without interrupting the node."""
    from coire_core.upgrade_artifacts import verify_prepared_release
    from coire_node.upgrade_bootstrap import refresh_guard_source
    from coire_node.upgrade_control import NativeUpgradeControl

    root = PrivateUpgradeStore(Path(settings.upgrade_bootstrap_root))
    with root.ownership("control.lock"):
        claim = root.read("claim.json", NativeUpgradeClaim)
        selected = root.read("selection.json", NativeRuntimeSelection)
        if claim is None or claim.state != "released" or selected is None:
            return None
        if process.environment_sha256 == selected.runtime.environment_sha256:
            return None
        journal = NativeUpgradeControl(settings).journal(claim.job_id)
        release = next(
            (
                value
                for value in (journal.plan.source, journal.plan.target)
                if value.manifest_sha256 == selected.release_sha256
            ),
            None,
        )
        if release is None:
            raise UpgradeUnavailable("Native recovery refresh Source release is absent")
        verified = verify_prepared_release(
            Path(settings.upgrade_prepared_dir),
            UpgradeReleaseRegister(
                prepared_artifact_id=release.prepared_artifact_id,
                expected_sha256=release.manifest_sha256,
            ),
        )
        if verified != release:
            raise UpgradeUnavailable("Native recovery refresh Source catalog differs")
        refresh = root.read("bootstrap-refresh.json", NativeBootstrapRefresh)
        if (
            refresh is not None
            and refresh.state == "committed"
            and refresh.selected == selected
            and refresh.previous_runtime.environment_sha256 == process.environment_sha256
            and root.read("runtime.json", PreparedNativeRuntime) == selected.runtime
        ):
            # A published handover already captured a positive Source PID and
            # cleanup proof. Its replay cannot depend on that PID surviving.
            from coire_node.upgrade_runtime import verify_prepared_runtime

            latest = journal.receipts.latest()
            if (
                latest is None
                or latest.phase not in TERMINAL_UPGRADE_PHASES
                or latest.outcome != "complete"
                or latest.observed_sha256 != selected.release_sha256
            ):
                raise UpgradeUnavailable("Native recovery refresh history differs")
            evidence = UpgradeEvidence.model_validate_json(latest.evidence)
            if (
                evidence.runtime_sha256 != selected.runtime.environment_sha256
                or not evidence.cleanup_verified
            ):
                raise UpgradeUnavailable("Native recovery refresh cleanup is unverified")
            envs = Path(settings.upgrade_environment_root)
            verify_prepared_runtime(envs, envs.parent / "runtimes", selected.runtime)
            return selected.runtime
        refresh_guard_source(
            settings,
            root,
            selected.runtime,
            selected.release_sha256,
            claim,
            refreshing_guard=process,
        )
        logger.info(
            "native recovery owner qualified current Source",
            extra={"job_id": claim.job_id, "node": settings.node_name},
        )
        return selected.runtime


def run_native_guard(settings: Settings, stopped: threading.Event) -> PreparedNativeRuntime | None:
    """Run the retained owner independently of node replacement and worker I/O."""
    from coire_node.upgrade_control import NativeUpgradeControl
    from coire_node.upgrade_workflow import advance_guard_step

    if (
        not settings.upgrades_enabled
        or settings.node_name not in {"coire-edge-a", "coire-edge-b"}
        or not is_darwin(sys.platform)
        or platform.machine() != "arm64"
        or not settings.node_token.get_secret_value()
    ):
        raise UpgradeUnavailable("Native recovery owner requires an enabled authenticated Studio")
    root = PrivateUpgradeStore(Path(settings.upgrade_bootstrap_root))
    bootstrap = root.read("runtime.json", PreparedNativeRuntime)
    if bootstrap is None:
        raise UpgradeUnavailable("Native recovery owner has no retained bootstrap")
    process = NodeAgentProcessIdentity(
        pid=os.getpid(),
        create_time_s=psutil.Process(os.getpid()).create_time(),
        environment_sha256=bootstrap.environment_sha256,
    )

    publisher_stopped = threading.Event()

    def heartbeat() -> None:
        while not publisher_stopped.wait(1.0) and not stopped.is_set():
            try:
                publish_guard_status(settings, process)
            except (CoireError, OSError, ValueError, psutil.Error):
                # No synthetic ready status: a failed heartbeat expires naturally.
                logger.warning("native upgrade recovery heartbeat remains unverified")

    with root.ownership("lifetime.lock"):
        publish_guard_status(settings, process)
        publisher = threading.Thread(target=heartbeat, name="coire-upgrade-heartbeat", daemon=True)
        publisher.start()
        try:
            with httpx.Client(trust_env=False, follow_redirects=False, timeout=5.0) as client:
                transport = NativeGuardTransport(settings, client)
                while not stopped.is_set():
                    claim: NativeUpgradeClaim | None = None
                    try:
                        claim = root.read("claim.json", NativeUpgradeClaim)
                        if claim is not None and claim.state == "active":
                            journal = NativeUpgradeControl(settings).journal(claim.job_id)
                            advance_guard_step(settings, journal, transport)
                            transport.relay_expired_failure(journal)
                        elif claim is not None and claim.state == "released":
                            delegate = refresh_running_guard(settings, process)
                            if delegate is not None:
                                return delegate if not stopped.is_set() else None
                    except (CoireError, OSError, ValueError, psutil.Error):
                        # Fixed diagnostics retain no command, credential, traceback
                        # or engine output. Journals/fences remain the authority.
                        logger.warning(
                            "native upgrade recovery progress remains unverified",
                            extra={"job_id": claim.job_id if claim is not None else None},
                        )
                    stopped.wait(0.25)
        finally:
            publisher_stopped.set()
            publisher.join(timeout=6.0)
    return None


def main() -> None:
    """Fixed launchd entrypoint; read only the existing node-control credential."""
    from coire_core.logging import StructuredJSONFormatter
    from coire_node.keychain import NODE_TOKEN_ITEM, read_item
    from coire_node.otel import configure_node_telemetry

    handler = logging.StreamHandler()
    handler.setFormatter(StructuredJSONFormatter())
    logging.basicConfig(level=logging.INFO, handlers=[handler])
    settings = Settings()
    if not is_darwin(sys.platform) or platform.machine() != "arm64":
        raise SystemExit(1)
    if not settings.node_token.get_secret_value():
        settings.node_token = read_item(NODE_TOKEN_ITEM)
    configure_node_telemetry(
        "upgrade-guard", settings.otlp_endpoint, diagnostics_enabled=settings.diagnostics_enabled
    )
    stopped = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stopped.set())
    signal.signal(signal.SIGINT, lambda *_: stopped.set())
    try:
        if settings.upgrade_recovery_id is not None:
            from coire_node.expired_recovery_support import run_configured_support

            run_configured_support(settings, stopped)
            return  # Never select the expired legacy budget/delegate on support exit.
        delegate = retained_guard_delegate(settings)
        if delegate is None:
            delegate = run_native_guard(settings, stopped)
        if delegate is not None and not stopped.is_set():
            python = (
                Path(settings.upgrade_environment_root) / delegate.environment_id / "bin/python3"
            )
            os.execv(str(python), [str(python), "-I", "-B", "-m", "coire_node.upgrade_guard"])
    except CoireError as error:
        logger.error(
            "native upgrade recovery owner refused",
            extra={"coire_code": error.code, "reason": error.detail},
        )
        raise SystemExit(1) from None
    except (OSError, ValueError, psutil.Error):
        logger.error("native upgrade recovery owner could not start")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
