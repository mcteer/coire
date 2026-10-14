"""Distinct administrative intent for expired recovery; never renew old authority."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Annotated, Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import AwareDatetime, Field, PositiveInt, model_validator

from coire_core.models.sharding import (
    LinkObservation,
    LinkProbeCommand,
    ProbeOutcome,
    ProbeTransport,
)
from coire_core.models.upgrade_recovery_cold_binding import UpgradeColdContinuationBinding
from coire_core.models.upgrades import (
    NODE_UPGRADE_OWNERS,
    ArtifactKey,
    NativeGuardStatus,
    NativeNodeProcess,
    NativeRecoveryIntent,
    NativeRuntimeSelection,
    NativeSmokeAttempt,
    NativeSmokeCommand,
    NativeUpgradeClaim,
    NodeAgentProcessIdentity,
    NodeMaintenanceAck,
    NodeUpgradeCollectiveScope,
    NodeUpgradeInventory,
    PreparedNativeRuntime,
    Sha256,
    UpgradeEvent,
    UpgradeEvidence,
    UpgradeExecutionPlan,
    UpgradeId,
    UpgradeMaintenance,
    UpgradeOperation,
    UpgradePhase,
    UpgradeRelease,
    UpgradeWire,
)

RECOVERY_PRESERVATION_TABLES = (
    "users",
    "training_jobs",
    "training_attempts",
    "training_checkpoints",
    "training_adapters",
    "training_dataset_revisions",
    "training_dataset_analyses",
    "feedback_preferences",
    "comparison_pairs",
    "harness_evaluations",
)


class UpgradeRecoverySchemaRequest(UpgradeWire):
    """Prepare only the known pre-recovery 0034 shape; no caller SQL or budget."""

    original_job_id: UpgradeId
    original_generation: PositiveInt
    original_plan_sha256: Sha256
    expected_source_sha256: Sha256
    expected_schema_sha256: Sha256
    idempotency_key: str = Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_.:-]+$")


class UpgradeRecoverySchemaObservation(UpgradeWire):
    original_job_id: UpgradeId
    original_generation: PositiveInt
    original_plan_sha256: Sha256
    source_sha256: Sha256
    schema_head: Literal["0034_upgrades"] = "0034_upgrades"
    schema_sha256: Sha256
    recovery_tables_absent: bool
    observed_at: AwareDatetime


class UpgradeRecoverySchemaReceipt(UpgradeWire):
    request: UpgradeRecoverySchemaRequest
    before: UpgradeRecoverySchemaObservation
    after: UpgradeRecoverySchemaObservation
    prepared_by: UUID
    prepared_at: AwareDatetime

    @model_validator(mode="after")
    def exact_additive_preparation(self) -> UpgradeRecoverySchemaReceipt:
        if (
            not self.before.recovery_tables_absent
            or self.after.recovery_tables_absent
            or self.before.schema_sha256 != self.request.expected_schema_sha256
            or self.before.schema_sha256 == self.after.schema_sha256
            or not self.before.observed_at <= self.after.observed_at <= self.prepared_at
        ):
            raise ValueError("recovery schema preparation lacks known additive before/after proof")
        for snapshot in (self.before, self.after):
            if (
                snapshot.original_job_id,
                snapshot.original_generation,
                snapshot.original_plan_sha256,
                snapshot.source_sha256,
            ) != (
                self.request.original_job_id,
                self.request.original_generation,
                self.request.original_plan_sha256,
                self.request.expected_source_sha256,
            ):
                raise ValueError("recovery schema preparation changes original operation authority")
        return self


class UpgradeRecoverySchemaBootstrapInput(UpgradeWire):
    command: Literal["observe", "prepare"]
    original_job_id: UpgradeId
    preparation: UpgradeRecoverySchemaRequest | None = None

    @model_validator(mode="after")
    def fixed_administrative_command(self) -> UpgradeRecoverySchemaBootstrapInput:
        if (self.command == "prepare") != (self.preparation is not None):
            raise ValueError("schema bootstrap preparation requires its exact typed request")
        if (
            self.preparation is not None
            and self.preparation.original_job_id != self.original_job_id
        ):
            raise ValueError("schema bootstrap cannot change the original operation")
        return self


class UpgradeExpiredRecoveryDataSnapshot(UpgradeWire):
    """Core-produced data fingerprints only; no private records in the wire shape."""

    recovery_id: UpgradeId
    original_job_id: UpgradeId
    original_generation: PositiveInt
    source_sha256: Sha256
    table_sha256s: dict[ArtifactKey, Sha256] = Field(min_length=10, max_length=10)
    row_counts: dict[ArtifactKey, int] = Field(min_length=10, max_length=10)
    observed_at: AwareDatetime

    @model_validator(mode="after")
    def fixed_preservation_scope(self) -> UpgradeExpiredRecoveryDataSnapshot:
        if (
            set(self.table_sha256s) != set(RECOVERY_PRESERVATION_TABLES)
            or set(self.row_counts) != set(RECOVERY_PRESERVATION_TABLES)
            or any(not 0 <= count <= 20000 for count in self.row_counts.values())
            or self.recovery_id == self.original_job_id
        ):
            raise ValueError(
                "recovery preservation scope differs from bounded fixed user-data tables"
            )
        return self


class UpgradeExpiredRecoveryNativeDataSnapshot(UpgradeWire):
    """Native fixed-store content fingerprints, with no private filenames/content."""

    original_job_id: UpgradeId
    original_generation: PositiveInt
    source_sha256: Sha256
    node: Literal["coire-edge-a", "coire-edge-b"]
    root_sha256s: dict[Literal["datasets", "artifacts", "components"], Sha256] = Field(
        min_length=3, max_length=3
    )
    file_counts: dict[Literal["datasets", "artifacts", "components"], int] = Field(
        min_length=3, max_length=3
    )
    total_bytes: int = Field(ge=0, le=1024**4)
    observed_at: AwareDatetime

    @model_validator(mode="after")
    def fixed_native_stores(self) -> UpgradeExpiredRecoveryNativeDataSnapshot:
        expected = {"datasets", "artifacts", "components"}
        if (
            set(self.root_sha256s) != expected
            or set(self.file_counts) != expected
            or any(not 0 <= count <= 4096 for count in self.file_counts.values())
        ):
            raise ValueError("native preservation snapshot differs from fixed bounded stores")
        return self


class UpgradeExpiredRecoveryRequest(UpgradeWire):
    """Optimistic bindings and catalog ID only; server captures the new epoch.

    Original deadlines, effect argv, artifact paths and peer credentials cannot
    be caller fields. Acceptance must resolve the retained plan/scopes and the
    immutable support release, authenticate/admin-audit and pin ownership before
    producing any execution authority. This request itself grants none.
    """

    original_job_id: UpgradeId
    original_generation: PositiveInt
    original_plan_sha256: Sha256
    original_scope_sha256s: tuple[Sha256, ...] = Field(max_length=6)
    expected_source_sha256: Sha256
    support_release_id: UUID
    idempotency_key: str = Field(min_length=1, max_length=128, pattern=r"^[!-~]+$")
    recovery_s: int = Field(default=300, ge=1, le=300)
    recovery_mode: Literal["preserve", "cold"] = "preserve"
    supervision: Literal["launchd", "fixture"] = "launchd"

    @model_validator(mode="after")
    def unique_scopes(self) -> UpgradeExpiredRecoveryRequest:
        if len(set(self.original_scope_sha256s)) != len(self.original_scope_sha256s):
            raise ValueError("duplicate original recovery scope")
        return self


class UpgradeRecoveryRetiredSupervisor(UpgradeWire):
    """Fixed private fixture supervisor preimage; not permission from absence."""

    pid: PositiveInt
    create_time_s: float = Field(gt=0, allow_inf_nan=False)
    owner_uid: int = Field(ge=0)
    script_sha256: Sha256


class UpgradeExpiredRecoveryColdClaim(UpgradeWire):
    """Separate physical owner evidence; native producer must hold both locks."""

    intent: UpgradeExpiredRecoveryIntent
    process: NodeAgentProcessIdentity
    runtime: PreparedNativeRuntime
    kernel_boot_identity: ArtifactKey
    original_claim: NativeUpgradeClaim
    original_native: NativeRecoveryIntent
    previous_guard: NativeGuardStatus
    last_node: NativeNodeProcess
    supervision: Literal["launchd", "fixture"]
    retired_supervisor: UpgradeRecoveryRetiredSupervisor | None = None
    managed_definition_sha256: Sha256 | None = None
    claimed_at: AwareDatetime
    continuation: UpgradeColdContinuationBinding | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

    @model_validator(mode="after")
    def exact_cold_scope(self) -> UpgradeExpiredRecoveryColdClaim:
        plan = self.intent.original_plan
        op = plan.operation
        support = self.intent.support_release.manifest
        native = self.original_native
        continuation = self.continuation
        original_claim = (
            continuation.post_handback.released_claim
            if continuation is not None and continuation.post_handback is not None
            else continuation.original_claim
            if continuation is not None
            else self.original_claim
        )
        if continuation is not None and (
            continuation.previous_recovery_id == self.intent.recovery_id
            or original_claim != self.original_claim
            or continuation.original_native != native
            or continuation.previous_guard != self.previous_guard
            or continuation.retained_node != self.last_node
            or self.supervision != "fixture"
            or self.retired_supervisor is None
            or self.retired_supervisor.sha256() != continuation.retired_supervisor_sha256
            or not continuation.previous_deadline_at
            <= continuation.observed_at
            <= self.intent.accepted_at
        ):
            raise ValueError("cold continuation owner changes its exact predecessor binding")
        if (
            self.intent.request.recovery_mode != "cold"
            or self.supervision != self.intent.request.supervision
            or (self.supervision == "fixture") != (self.retired_supervisor is not None)
            or (self.supervision == "launchd") != (self.managed_definition_sha256 is not None)
            or (
                self.original_claim.state != "active"
                and not (
                    continuation is not None
                    and continuation.post_handback is not None
                    and self.original_claim == continuation.post_handback.released_claim
                )
                and not (
                    continuation is not None
                    and continuation.previous_local_recovery_maintenance is not None
                    and self.original_claim == continuation.original_claim
                )
            )
            or (
                self.original_claim.job_id,
                self.original_claim.generation,
                self.original_claim.plan_sha256,
            )
            != (op.job_id, op.generation, plan.authority_sha256())
            or (native.job_id, native.generation, native.plan_sha256)
            != (op.job_id, op.generation, plan.authority_sha256())
            or native.source.release_sha256 != plan.source.manifest_sha256
            or (native.source.runtime.environment_id, native.source.runtime.environment_sha256)
            != (plan.source.manifest.environment_id, plan.source.manifest.environment_sha256)
            or (native.candidate.environment_id, native.candidate.environment_sha256)
            != (plan.target.manifest.environment_id, plan.target.manifest.environment_sha256)
            or native.recovery_deadline_at != self.intent.original_recovery_deadline_at
            or native.phase
            not in {"recovery_required", "restoring", "source_selected", "source_verified"}
            or (self.runtime.environment_id, self.runtime.environment_sha256)
            != (support.environment_id, support.environment_sha256)
            or self.process.environment_sha256 != self.runtime.environment_sha256
            or self.previous_guard.node != op.node
            or self.original_claim.node != op.node
            or self.last_node.selected.release_sha256
            not in {plan.source.manifest_sha256, plan.target.manifest_sha256}
            or self.previous_guard.selected.release_sha256
            not in {plan.source.manifest_sha256, plan.target.manifest_sha256}
            or self.process.pid
            in {
                self.previous_guard.process.pid,
                native.source_process.pid,
                self.last_node.process.pid,
            }
            or not self.intent.accepted_at <= self.claimed_at < self.intent.deadline_at
        ):
            raise ValueError("cold recovery scope or epoch differs")
        return self


class UpgradeExpiredRecoveryColdCheck(UpgradeWire):
    claim_sha256: Sha256
    consumer: NodeAgentProcessIdentity


class UpgradeExpiredRecoveryColdSourceIntent(UpgradeWire):
    """Write-ahead exact Source projection, preserving all old authority history."""

    owner: UpgradeExpiredRecoveryColdClaim
    before_pointer: PreparedNativeRuntime
    before_pointer_selection: NativeRuntimeSelection | None = None
    before_bootstrap_runtime: PreparedNativeRuntime
    source_selection: NativeRuntimeSelection
    projected_native: NativeRecoveryIntent
    prepared_at: AwareDatetime

    @model_validator(mode="after")
    def exact_source_projection(self) -> UpgradeExpiredRecoveryColdSourceIntent:
        owner = self.owner
        native = owner.original_native
        op = owner.intent.original_plan.operation
        selection = self.source_selection
        preactivation = native.activation_started_at is None
        expected_phase = "prepared" if preactivation else "source_selected"
        exact_selection = (
            selection == native.source and self.before_pointer == native.source.runtime
            if preactivation
            else (selection.node, selection.job_id, selection.generation)
            == (op.node, op.job_id, op.generation)
            and owner.claimed_at <= selection.selected_at <= self.prepared_at
        )
        if owner.continuation is not None:
            exact_selection = selection == owner.continuation.retained_node.selected
        if (
            self.before_pointer not in (native.source.runtime, native.candidate)
            or self.before_bootstrap_runtime != owner.previous_guard.bootstrap_runtime
            or selection.runtime != native.source.runtime
            or selection.release_sha256 != native.source.release_sha256
            or not exact_selection
            or not owner.claimed_at <= self.prepared_at < owner.intent.deadline_at
            or self.projected_native
            != native.model_copy(update={"phase": expected_phase, "updated_at": self.prepared_at})
        ):
            raise ValueError("cold Source projection changes retained authority")
        return self


class UpgradeExpiredRecoveryColdState(UpgradeWire):
    claim_sha256: Sha256
    owner: UpgradeExpiredRecoveryColdClaim | None = None
    checked_at: AwareDatetime


class UpgradeExpiredRecoveryColdSourceReceipt(UpgradeWire):
    projection: UpgradeExpiredRecoveryColdSourceIntent
    observed_at: AwareDatetime

    @model_validator(mode="after")
    def original_budget(self) -> UpgradeExpiredRecoveryColdSourceReceipt:
        if (
            not self.projection.prepared_at
            <= self.observed_at
            < self.projection.owner.intent.deadline_at
        ):
            raise ValueError("cold Source receipt lies outside captured recovery authority")
        return self


class UpgradeExpiredRecoveryColdSourceLaunch(UpgradeWire):
    """One fixed Source-node spawn intent; no arbitrary argv or caller deadline."""

    source: UpgradeExpiredRecoveryColdSourceReceipt
    attempt: int = Field(ge=0, le=2)
    started_at: AwareDatetime
    retained_process: NodeAgentProcessIdentity | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

    @model_validator(mode="after")
    def captured_scope(self) -> UpgradeExpiredRecoveryColdSourceLaunch:
        continuation = self.source.projection.owner.continuation
        if (self.retained_process is not None) != (continuation is not None) or (
            continuation is not None and self.retained_process != continuation.retained_node.process
        ):
            raise ValueError("cold Source retention has no exact continuation binding")
        if (
            not self.source.observed_at
            <= self.started_at
            < self.source.projection.owner.intent.deadline_at
        ):
            raise ValueError("cold Source launch exceeds captured authority")
        return self


class UpgradeExpiredRecoveryColdSourceProcess(UpgradeWire):
    """Positive owned process identity only; never application/serving readiness."""

    launch: UpgradeExpiredRecoveryColdSourceLaunch
    process: NodeAgentProcessIdentity
    observed_at: AwareDatetime

    @model_validator(mode="after")
    def exact_source_process(self) -> UpgradeExpiredRecoveryColdSourceProcess:
        projection = self.launch.source.projection
        valid_birth = (
            self.process == self.launch.retained_process
            if self.launch.retained_process is not None
            else self.launch.started_at.timestamp() <= self.process.create_time_s
        )
        if (
            self.process.environment_sha256
            != projection.source_selection.runtime.environment_sha256
            or not self.launch.started_at <= self.observed_at < projection.owner.intent.deadline_at
            or not valid_birth
            or self.process.create_time_s > self.observed_at.timestamp()
        ):
            raise ValueError("cold Source process provenance or epoch differs")
        return self


class UpgradeExpiredRecoveryColdCollectiveArchive(UpgradeWire):
    """Fresh cleanup and immutable history retained before legacy inventory projection."""

    source: UpgradeExpiredRecoveryColdSourceProcess
    scopes: tuple[NodeUpgradeCollectiveScope, ...] = Field(min_length=1, max_length=6)
    interrupted_scope_sha256s: tuple[Sha256, ...] = Field(max_length=6)
    previous_archive_sha256: Sha256 | None = Field(default=None, exclude_if=lambda v: v is None)
    local: NodeUpgradeInventory
    peer: NodeUpgradeInventory
    history_sha256: Sha256
    prepared_at: AwareDatetime

    @model_validator(mode="after")
    def captured_archive(self) -> UpgradeExpiredRecoveryColdCollectiveArchive:
        owner = self.source.launch.source.projection.owner
        expected_archive = (
            (
                owner.continuation.previous_archive_sha256
                or owner.continuation.untransferred_archive_sha256
            )
            if owner.continuation is not None
            else None
        )
        if self.previous_archive_sha256 != expected_archive:
            raise ValueError("cold archive predecessor differs from accepted continuation")
        hashes = tuple(scope.sha256() for scope in self.scopes)
        if (
            len(set(hashes)) != len(hashes)
            or set(hashes) != set(owner.intent.request.original_scope_sha256s)
            or any(scope.plan != owner.intent.original_plan for scope in self.scopes)
            or len(set(self.interrupted_scope_sha256s)) != len(self.interrupted_scope_sha256s)
            or not set(self.interrupted_scope_sha256s) <= set(hashes)
            or not self.source.observed_at <= self.prepared_at < owner.intent.deadline_at
        ):
            raise ValueError("cold archive changes captured dispatch history or authority")
        for inventory, node, runtime in (
            (self.local, "coire-edge-a", owner.original_native.source.runtime.environment_sha256),
            (self.peer, "coire-edge-b", owner.intent.original_plan.peer_runtime_sha256),
        ):
            if (
                inventory.job_id,
                inventory.generation,
                inventory.node,
                inventory.runtime_sha256,
            ) != (
                (
                    owner.intent.local_maintenance().job_id
                    if node == owner.original_claim.node
                    else owner.original_claim.job_id
                ),
                (
                    owner.intent.local_maintenance().generation
                    if node == owner.original_claim.node
                    else owner.original_claim.generation
                ),
                node,
                runtime,
            ) or not 0 <= (self.prepared_at - inventory.observed_at).total_seconds() <= 5:
                raise ValueError("cold archive inventory identity or freshness differs")
        return self


class UpgradeExpiredRecoveryColdCollectiveArchiveReceipt(UpgradeWire):
    initial_archive_sha256: Sha256
    archive: UpgradeExpiredRecoveryColdCollectiveArchive
    archived_at: AwareDatetime

    @model_validator(mode="after")
    def captured_budget(self) -> UpgradeExpiredRecoveryColdCollectiveArchiveReceipt:
        if (
            not self.archive.prepared_at
            <= self.archived_at
            < self.archive.source.launch.source.projection.owner.intent.deadline_at
        ):
            raise ValueError("cold archive receipt exceeds captured recovery budget")
        return self


class UpgradeExpiredRecoveryColdSmokeCommand(UpgradeWire):
    """Registry-bound Source probe in a distinct recovery ownership namespace."""

    source: UpgradeExpiredRecoveryColdSourceProcess
    command: NativeSmokeCommand
    captured_at: AwareDatetime

    @model_validator(mode="after")
    def captured_source_smoke(self) -> UpgradeExpiredRecoveryColdSmokeCommand:
        owner = self.source.launch.source.projection.owner
        if (
            self.command.plan != owner.intent.original_plan
            or self.command.stage != "source-serving"
            or self.command.runtime_sha256
            != owner.original_native.source.runtime.environment_sha256
            or self.command.deadline_at != owner.intent.deadline_at
            or not self.source.observed_at <= self.captured_at < owner.intent.deadline_at
        ):
            raise ValueError("cold smoke changes captured registry, Source or root budget")
        return self


class UpgradeExpiredRecoveryColdSmokeReceipt(UpgradeWire):
    smoke: UpgradeExpiredRecoveryColdSmokeCommand
    attempt: NativeSmokeAttempt
    observed_at: AwareDatetime

    @model_validator(mode="after")
    def qualified_actual_source(self) -> UpgradeExpiredRecoveryColdSmokeReceipt:
        owner = self.smoke.source.launch.source.projection.owner
        plan = owner.intent.original_plan
        command = self.smoke.command
        attempt = self.attempt
        evidence = attempt.evidence
        if (
            attempt.state != "complete"
            or not attempt.cleanup_verified
            or evidence is None
            or (
                attempt.job_id,
                attempt.generation,
                attempt.plan_sha256,
                attempt.stage,
                attempt.runtime_sha256,
                attempt.deadline_at,
            )
            != (
                plan.operation.job_id,
                plan.operation.generation,
                plan.authority_sha256(),
                command.stage,
                command.runtime_sha256,
                command.deadline_at,
            )
            or evidence.check != "node"
            or (evidence.model_id, evidence.model_variant_id, evidence.model_manifest_sha256)
            != (plan.model_id, plan.model_variant_id, plan.model_manifest_sha256)
            or not self.smoke.captured_at
            <= attempt.updated_at
            <= self.observed_at
            < owner.intent.deadline_at
        ):
            raise ValueError("cold Source smoke lacks actual captured serving and cleanup evidence")
        return self


class UpgradeExpiredRecoveryColdPairCommand(UpgradeWire):
    """New qualification dispatch; cannot reuse an interrupted original command."""

    smoke: UpgradeExpiredRecoveryColdSmokeReceipt
    archive: UpgradeExpiredRecoveryColdCollectiveArchiveReceipt
    index: int = Field(ge=0, le=2)
    command: LinkProbeCommand
    captured_at: AwareDatetime

    @model_validator(mode="after")
    def distinct_captured_pair(self) -> UpgradeExpiredRecoveryColdPairCommand:
        owner = self.smoke.smoke.source.launch.source.projection.owner
        intent = owner.intent
        expected = uuid5(
            NAMESPACE_URL,
            f"coire-expired-recovery-pair/{intent.recovery_id}/{intent.sha256()}/{self.index}",
        )
        if (
            intent.original_plan.peer_runtime_sha256 is None
            or self.archive.archive.source != self.smoke.smoke.source
            or set(intent.original_plan.maintenance.targets) != {"coire-edge-a", "coire-edge-b"}
            or self.command.command_id != expected
            or self.command.transport is not ProbeTransport.JACCL
            or not self.smoke.observed_at <= self.captured_at < intent.deadline_at
            or self.captured_at < self.archive.archived_at
        ):
            raise ValueError(
                "cold pair command differs from distinct captured exact-pair authority"
            )
        return self


class UpgradeExpiredRecoveryColdPairResult(UpgradeWire):
    """Actual new worker result retained even when it fails exact-pair qualification."""

    dispatch: UpgradeExpiredRecoveryColdPairCommand
    observation: LinkObservation

    @model_validator(mode="after")
    def actual_new_epoch(self) -> UpgradeExpiredRecoveryColdPairResult:
        owner = self.dispatch.smoke.smoke.source.launch.source.projection.owner
        if (
            self.observation.observed_at.tzinfo is None
            or (self.observation.node_a, self.observation.node_b, self.observation.transport)
            != ("coire-edge-a", "coire-edge-b", ProbeTransport.JACCL)
            or not self.dispatch.captured_at
            <= self.observation.observed_at
            < owner.intent.deadline_at
        ):
            raise ValueError("cold pair result is outside its new dispatch epoch")
        return self

    def qualified(self) -> bool:
        owner = self.dispatch.smoke.smoke.source.launch.source.projection.owner
        return self.observation.outcome is ProbeOutcome.SUCCEEDED and (
            self.observation.runtime_sha256_a,
            self.observation.runtime_sha256_b,
        ) == (
            owner.original_native.source.runtime.environment_sha256,
            owner.intent.original_plan.peer_runtime_sha256,
        )


class UpgradeExpiredRecoveryColdPairQualification(UpgradeWire):
    results: tuple[UpgradeExpiredRecoveryColdPairResult, ...] = Field(min_length=3, max_length=3)
    local: NodeUpgradeInventory
    peer: NodeUpgradeInventory
    observed_at: AwareDatetime

    @model_validator(mode="after")
    def three_actual_exact_pair_results(self) -> UpgradeExpiredRecoveryColdPairQualification:
        first = self.results[0].dispatch
        owner = first.smoke.smoke.source.launch.source.projection.owner
        if (
            tuple(result.dispatch.index for result in self.results) != (0, 1, 2)
            or len({result.observation.id for result in self.results}) != 3
            or any(
                result.dispatch.smoke != first.smoke
                or result.dispatch.archive != first.archive
                or not result.qualified()
                for result in self.results
            )
            or any(
                left.observation.observed_at > right.dispatch.captured_at
                for left, right in zip(self.results, self.results[1:], strict=False)
            )
            or not self.results[-1].observation.observed_at
            <= self.observed_at
            < owner.intent.deadline_at
        ):
            raise ValueError(
                "cold pair qualification lacks three sequential new exact-pair results"
            )
        for inventory, node, runtime in (
            (self.local, "coire-edge-a", owner.original_native.source.runtime.environment_sha256),
            (self.peer, "coire-edge-b", owner.intent.original_plan.peer_runtime_sha256),
        ):
            if (
                inventory.job_id,
                inventory.generation,
                inventory.node,
                inventory.runtime_sha256,
            ) != (
                (
                    owner.intent.local_maintenance().job_id
                    if node == owner.original_claim.node
                    else owner.original_claim.job_id
                ),
                (
                    owner.intent.local_maintenance().generation
                    if node == owner.original_claim.node
                    else owner.original_claim.generation
                ),
                node,
                runtime,
            ) or not 0 <= (self.observed_at - inventory.observed_at).total_seconds() <= 5:
                raise ValueError("cold pair qualification inventory identity or freshness differs")
            if not inventory.idle(NODE_UPGRADE_OWNERS):
                raise ValueError("cold pair qualification retains physical or unknown obligations")
        return self


class UpgradeExpiredRecoveryColdVerificationRequest(UpgradeWire):
    smoke: UpgradeExpiredRecoveryColdSmokeReceipt
    archive: UpgradeExpiredRecoveryColdCollectiveArchiveReceipt | None = None
    pair: UpgradeExpiredRecoveryColdPairQualification | None = None
    native_after: UpgradeExpiredRecoveryNativeDataSnapshot


class UpgradeExpiredRecoveryColdVerification(UpgradeWire):
    """Core-produced bounded verification; handback and release remain separate."""

    request: UpgradeExpiredRecoveryColdVerificationRequest
    data_before: UpgradeExpiredRecoveryDataSnapshot
    data_after: UpgradeExpiredRecoveryDataSnapshot
    native_before: UpgradeExpiredRecoveryNativeDataSnapshot
    local: NodeUpgradeInventory
    peer: NodeUpgradeInventory | None = None
    verified_at: AwareDatetime

    @model_validator(mode="after")
    def fresh_linked_source_verification(self) -> UpgradeExpiredRecoveryColdVerification:
        from coire_core.errors import CoireError
        from coire_core.upgrade_recovery import verify_native_data_preserved

        smoke = self.request.smoke
        owner = smoke.smoke.source.launch.source.projection.owner
        intent = owner.intent
        plan = intent.original_plan
        try:
            verify_native_data_preserved(self.native_before, self.request.native_after, intent)
        except CoireError:
            raise ValueError("cold verification native preservation differs") from None
        for snapshot in (self.data_before, self.data_after):
            if (
                snapshot.recovery_id,
                snapshot.original_job_id,
                snapshot.original_generation,
                snapshot.source_sha256,
            ) != (
                intent.recovery_id,
                intent.request.original_job_id,
                intent.request.original_generation,
                intent.request.expected_source_sha256,
            ):
                raise ValueError("cold verification data belongs to another captured scope")
        if (
            self.data_before.table_sha256s != self.data_after.table_sha256s
            or self.data_before.row_counts != self.data_after.row_counts
            or not intent.accepted_at
            <= self.data_before.observed_at
            <= self.data_after.observed_at
            <= self.verified_at
            < intent.deadline_at
            or not smoke.observed_at <= self.verified_at
            or not self.request.native_after.observed_at <= self.verified_at
        ):
            raise ValueError("cold verification cannot prove bounded preserved Source data")
        pair = self.request.pair
        archive = self.request.archive
        if intent.request.original_scope_sha256s and archive is None:
            raise ValueError("cold verification lacks captured original collective cleanup")
        if archive is not None and archive.archive.source != smoke.smoke.source:
            raise ValueError("cold verification archive has another Source process preimage")
        if (
            plan.source.manifest.paired_required or plan.target.manifest.paired_required
        ) and pair is None:
            raise ValueError("cold verification requires actual new exact-pair qualification")
        if pair is not None and pair.results[0].dispatch.smoke != smoke:
            raise ValueError("cold verification pair has another Source-serving preimage")
        if pair is not None and pair.results[0].dispatch.archive != archive:
            raise ValueError("cold verification pair has another original cleanup preimage")
        snapshots = [
            (
                self.local,
                plan.operation.node,
                owner.original_native.source.runtime.environment_sha256,
            )
        ]
        if plan.peer_runtime_sha256 is not None:
            if self.peer is None:
                raise ValueError("cold verification has no actual fenced peer inventory")
            snapshots.append(
                (
                    self.peer,
                    "coire-edge-b" if plan.operation.node == "coire-edge-a" else "coire-edge-a",
                    plan.peer_runtime_sha256,
                )
            )
        elif self.peer is not None:
            raise ValueError("cold verification invents an unaccepted peer")
        for inventory, node, runtime in snapshots:
            if (
                (inventory.job_id, inventory.generation, inventory.node, inventory.runtime_sha256)
                != (
                    (
                        owner.intent.local_maintenance().job_id
                        if node == plan.operation.node
                        else plan.operation.job_id
                    ),
                    (
                        owner.intent.local_maintenance().generation
                        if node == plan.operation.node
                        else plan.operation.generation
                    ),
                    node,
                    runtime,
                )
                or not 0 <= (self.verified_at - inventory.observed_at).total_seconds() <= 5
                or not inventory.idle(NODE_UPGRADE_OWNERS)
            ):
                raise ValueError("cold verification inventory is foreign, stale or still obligated")
        return self


class UpgradeExpiredRecoveryColdVerificationRead(UpgradeWire):
    verification_sha256: Sha256
    consumer: NodeAgentProcessIdentity


class UpgradeExpiredRecoveryColdHandbackIntent(UpgradeWire):
    """Fixed Source owner handback; original history is retained in full preimages."""

    verification: UpgradeExpiredRecoveryColdVerification
    before_selection: NativeRuntimeSelection
    before_runtime: PreparedNativeRuntime
    before_accepted_claim: NativeUpgradeClaim
    released_claim: NativeUpgradeClaim
    started_at: AwareDatetime

    @model_validator(mode="after")
    def exact_verified_source_handback(self) -> UpgradeExpiredRecoveryColdHandbackIntent:
        owner = self.verification.request.smoke.smoke.source.launch.source.projection.owner
        already_released = owner.continuation is not None and (
            (
                owner.continuation.post_handback is not None
                and self.before_accepted_claim == owner.continuation.post_handback.released_claim
            )
            or (
                owner.continuation.previous_local_recovery_maintenance is not None
                and self.before_accepted_claim == owner.original_claim
                and owner.original_claim.state == "released"
            )
        )
        released = (
            owner.original_claim
            if already_released
            else owner.original_claim.model_copy(
                update={"state": "released", "updated_at": self.started_at}
            )
        )
        if (
            self.before_runtime != owner.runtime
            or self.before_selection.node != owner.original_claim.node
            or (self.before_selection.runtime, self.before_selection.release_sha256)
            != (owner.original_native.source.runtime, owner.original_native.source.release_sha256)
            or (
                self.before_accepted_claim.job_id,
                self.before_accepted_claim.generation,
                self.before_accepted_claim.plan_sha256,
                self.before_accepted_claim.node,
            )
            != (
                owner.original_claim.job_id,
                owner.original_claim.generation,
                owner.original_claim.plan_sha256,
                owner.original_claim.node,
            )
            or (self.before_accepted_claim.state != "active" and not already_released)
            or self.released_claim != released
            or not self.verification.verified_at <= self.started_at < owner.intent.deadline_at
        ):
            raise ValueError("cold handback changes verified Source or original claim history")
        return self


class UpgradeExpiredRecoveryColdHandbackHelper(UpgradeWire):
    """Exact fixed Source guard process; no ordinary work or Core resolution grant."""

    handback: UpgradeExpiredRecoveryColdHandbackIntent
    process: NodeAgentProcessIdentity
    observed_at: AwareDatetime

    @model_validator(mode="after")
    def actual_source_helper(self) -> UpgradeExpiredRecoveryColdHandbackHelper:
        owner = self.handback.verification.request.smoke.smoke.source.launch.source.projection.owner
        if (
            self.process.environment_sha256
            != owner.original_native.source.runtime.environment_sha256
            or not self.handback.started_at.timestamp()
            <= self.process.create_time_s
            <= self.observed_at.timestamp()
            or not self.handback.started_at <= self.observed_at < owner.intent.deadline_at
            or (self.process.pid, self.process.create_time_s)
            == (owner.process.pid, owner.process.create_time_s)
        ):
            raise ValueError("cold Source helper has no exact new process provenance")
        return self


class UpgradeExpiredRecoveryColdHandbackReceipt(UpgradeWire):
    helper: UpgradeExpiredRecoveryColdHandbackHelper
    status: NativeGuardStatus
    node: NativeNodeProcess
    observed_at: AwareDatetime

    @model_validator(mode="after")
    def source_owner_physically_ready(self) -> UpgradeExpiredRecoveryColdHandbackReceipt:
        effect = (
            self.helper.handback.verification.request.smoke.smoke.source.launch.source.projection
        )
        source = self.helper.handback.verification.request.smoke.smoke.source
        if (
            self.status.process != self.helper.process
            or self.status.bootstrap_runtime != effect.source_selection.runtime
            or self.status.selected != effect.source_selection
            or not self.status.ready
            or self.node.selected != effect.source_selection
            or self.node.process != source.process
            or not 0 <= (self.observed_at - self.status.observed_at).total_seconds() <= 5
            or not self.helper.observed_at <= self.observed_at < effect.owner.intent.deadline_at
        ):
            raise ValueError("cold Source handback lacks actual current Source-owner readiness")
        return self


class UpgradeExpiredRecoveryColdSupervisionIntent(UpgradeWire):
    handback: UpgradeExpiredRecoveryColdHandbackReceipt
    started_at: AwareDatetime

    @model_validator(mode="after")
    def bounded_source_supervision(self) -> UpgradeExpiredRecoveryColdSupervisionIntent:
        owner = self.handback.helper.handback.verification.request.smoke.smoke.source.launch.source.projection.owner
        if not self.handback.observed_at <= self.started_at < owner.intent.deadline_at:
            raise ValueError("Source supervision changes the captured handback epoch")
        return self


class UpgradeExpiredRecoveryColdSupervisionReceipt(UpgradeWire):
    supervision: UpgradeExpiredRecoveryColdSupervisionIntent
    process: NodeAgentProcessIdentity
    kind: Literal["fixture", "launchd"]
    definition_sha256: Sha256 | None = None
    observed_at: AwareDatetime

    @model_validator(mode="after")
    def exact_source_supervisor(self) -> UpgradeExpiredRecoveryColdSupervisionReceipt:
        owner = self.supervision.handback.helper.handback.verification.request.smoke.smoke.source.launch.source.projection.owner
        if (
            self.kind != owner.supervision
            or not self.supervision.started_at <= self.observed_at < owner.intent.deadline_at
            or self.process.create_time_s > self.observed_at.timestamp()
        ):
            raise ValueError("Source supervision has foreign provenance or an unbounded epoch")
        if self.kind == "fixture":
            if (
                self.definition_sha256 is not None
                or self.process.environment_sha256 != owner.runtime.environment_sha256
                or not self.supervision.started_at.timestamp() <= self.process.create_time_s
                or (self.process.pid, self.process.create_time_s)
                == (owner.process.pid, owner.process.create_time_s)
            ):
                raise ValueError("fixture handback has no distinct fixed immutable supervisor")
        elif (
            self.definition_sha256 != owner.managed_definition_sha256
            or self.process != self.supervision.handback.helper.process
        ):
            raise ValueError("managed Source handback has no exact installed service owner")
        return self


class UpgradeExpiredRecoveryColdClosureIntent(UpgradeWire):
    supervision: UpgradeExpiredRecoveryColdSupervisionReceipt
    before_maintenance: UpgradeMaintenance
    started_at: AwareDatetime

    @model_validator(mode="after")
    def exact_original_fence(self) -> UpgradeExpiredRecoveryColdClosureIntent:
        owner = self.supervision.supervision.handback.helper.handback.verification.request.smoke.smoke.source.launch.source.projection.owner
        late = owner.continuation.post_handback if owner.continuation is not None else None
        if (
            not self.before_maintenance.matches_plan(owner.intent.local_inventory_plan())
            and not (late is not None and self.before_maintenance == late.released_maintenance)
        ) or not self.supervision.observed_at <= self.started_at < owner.intent.deadline_at:
            raise ValueError("cold closure intent changes original fencing or captured root")
        return self


class UpgradeRecoveryLocalFenceObservation(UpgradeWire):
    maintenance: UpgradeMaintenance
    observed_at: AwareDatetime


class UpgradeRecoveryHostHoldRequest(UpgradeWire):
    resolution_sha256: Sha256


class UpgradeRecoveryHostHoldReceipt(UpgradeWire):
    recovery_id: UpgradeId
    resolution_sha256: Sha256
    original_job_id: UpgradeId
    original_generation: PositiveInt
    plan_sha256: Sha256
    released: Literal[True] = True
    reconciled_by: UUID
    reconciled_at: AwareDatetime


class UpgradeColdClosureSupervision(UpgradeWire):
    """Exact retained supervision evidence without repeating the full handback."""

    protocol: Literal["cold-closure-supervision-v1"] = "cold-closure-supervision-v1"
    handback_sha256: Sha256
    receipt_sha256: Sha256
    process: NodeAgentProcessIdentity
    kind: Literal["fixture", "launchd"]
    definition_sha256: Sha256 | None = None
    started_at: AwareDatetime
    observed_at: AwareDatetime

    @classmethod
    def from_receipt(
        cls, receipt: UpgradeExpiredRecoveryColdSupervisionReceipt
    ) -> UpgradeColdClosureSupervision:
        return cls(
            handback_sha256=receipt.supervision.handback.sha256(),
            receipt_sha256=receipt.sha256(),
            process=receipt.process,
            kind=receipt.kind,
            definition_sha256=receipt.definition_sha256,
            started_at=receipt.supervision.started_at,
            observed_at=receipt.observed_at,
        )


class UpgradeExpiredRecoveryColdClosure(UpgradeWire):
    """Trusted native closure after Source handback; never an effect grant."""

    handback: UpgradeExpiredRecoveryColdHandbackReceipt
    supervision: UpgradeExpiredRecoveryColdSupervisionReceipt | UpgradeColdClosureSupervision
    local_release: NodeMaintenanceAck
    status: NativeGuardStatus
    node: NativeNodeProcess
    local_observation: UpgradeRecoveryLocalFenceObservation | None = Field(
        default=None, exclude_if=lambda v: v is None
    )
    observed_at: AwareDatetime

    @model_validator(mode="after")
    def exact_fresh_source_closure(self) -> UpgradeExpiredRecoveryColdClosure:
        source = self.handback.helper.handback.verification.request.smoke.smoke.source
        owner = source.launch.source.projection.owner
        late = owner.continuation.post_handback if owner.continuation is not None else None
        observation = self.local_observation
        if isinstance(self.supervision, UpgradeColdClosureSupervision):
            compact = self.supervision
            if (
                compact.handback_sha256 != self.handback.sha256()
                or compact.kind != owner.supervision
                or not self.handback.observed_at
                <= compact.started_at
                <= compact.observed_at
                < owner.intent.deadline_at
                or compact.process.create_time_s > compact.observed_at.timestamp()
                or (
                    compact.kind == "fixture"
                    and (
                        compact.definition_sha256 is not None
                        or compact.process.environment_sha256 != owner.runtime.environment_sha256
                        or compact.process.create_time_s < compact.started_at.timestamp()
                        or compact.process.pid
                        in {owner.process.pid, self.handback.helper.process.pid, source.process.pid}
                    )
                )
                or (
                    compact.kind == "launchd"
                    and (
                        compact.definition_sha256 != owner.managed_definition_sha256
                        or compact.process != self.handback.helper.process
                    )
                )
            ):
                raise ValueError("compact closure supervision changes exact handback provenance")
        historical_release = (
            late is not None
            and self.local_release.applied_at == late.released_maintenance.updated_at
        )
        if observation is not None and (
            observation.maintenance.state != "released"
            or observation.maintenance.model_dump(exclude={"state", "updated_at"})
            != owner.intent.local_maintenance().model_dump(exclude={"state", "updated_at"})
            or observation.maintenance.updated_at > self.local_release.applied_at
            or not self.local_release.applied_at <= observation.observed_at <= self.observed_at
            or not 0 <= (self.observed_at - observation.observed_at).total_seconds() <= 5
            or (
                historical_release
                and late is not None
                and observation.maintenance != late.released_maintenance
            )
        ):
            raise ValueError("cold closure has no fresh exact local fence observation")
        if (
            (
                isinstance(self.supervision, UpgradeExpiredRecoveryColdSupervisionReceipt)
                and self.supervision.supervision.handback != self.handback
            )
            or (
                not self.supervision.observed_at <= self.local_release.applied_at
                and not historical_release
            )
            or (self.local_release.job_id, self.local_release.generation, self.local_release.node)
            != (
                owner.intent.local_maintenance().job_id,
                owner.intent.local_maintenance().generation,
                owner.original_claim.node,
            )
            or not self.local_release.released
            or self.status.process != self.handback.helper.process
            or self.status.bootstrap_runtime != self.handback.status.bootstrap_runtime
            or self.status.selected != self.handback.status.selected
            or not self.status.ready
            or self.node.selected != self.handback.node.selected
            or self.node.process != self.handback.node.process
            or not self.local_release.applied_at <= self.observed_at
            or (
                self.local_release.applied_at < self.handback.observed_at and not historical_release
            )
            or not self.observed_at < owner.intent.deadline_at
            or not 0 <= (self.observed_at - self.status.observed_at).total_seconds() <= 5
            or (
                observation is None
                and not 0 <= (self.observed_at - self.local_release.applied_at).total_seconds() <= 5
            )
        ):
            raise ValueError("cold closure lacks exact fresh Source ownership and fence release")
        return self


class UpgradeExpiredRecoveryColdResolutionObservation(UpgradeWire):
    closure_sha256: Sha256
    status: NativeGuardStatus
    node: NativeNodeProcess
    maintenance: UpgradeMaintenance
    observed_at: AwareDatetime


class UpgradeExpiredRecoveryColdResolutionRequest(UpgradeWire):
    closure: UpgradeExpiredRecoveryColdClosure
    observation: UpgradeExpiredRecoveryColdResolutionObservation

    @model_validator(mode="after")
    def current_exact_closed_source(self) -> UpgradeExpiredRecoveryColdResolutionRequest:
        owner = self.closure.handback.helper.handback.verification.request.smoke.smoke.source.launch.source.projection.owner
        actual = self.observation
        before = owner.intent.local_maintenance()
        if (
            actual.closure_sha256 != self.closure.sha256()
            or actual.status.process != self.closure.handback.helper.process
            or actual.status.selected != self.closure.handback.status.selected
            or actual.status.bootstrap_runtime != self.closure.handback.status.bootstrap_runtime
            or not actual.status.ready
            or actual.node.process != self.closure.node.process
            or actual.node.selected != self.closure.node.selected
            or actual.maintenance.state != "released"
            or (
                actual.maintenance.job_id,
                actual.maintenance.generation,
                actual.maintenance.targets,
                actual.maintenance.drain_deadline_at,
                actual.maintenance.initiator_user_id,
            )
            != (
                before.job_id,
                before.generation,
                before.targets,
                before.drain_deadline_at,
                before.initiator_user_id,
            )
            or not self.closure.local_release.applied_at >= actual.maintenance.updated_at
            or not self.closure.observed_at <= actual.observed_at < owner.intent.deadline_at
            or not 0 <= (actual.observed_at - actual.status.observed_at).total_seconds() <= 5
        ):
            raise ValueError("cold resolution observation is foreign, stale or no longer closed")
        return self


class UpgradeExpiredRecoveryColdResolution(UpgradeWire):
    """Linked late recovery, retaining original upgrade failure and history."""

    closure: UpgradeExpiredRecoveryColdClosure
    observation: UpgradeExpiredRecoveryColdResolutionObservation
    peer_release: NodeMaintenanceAck | None = None
    operation: UpgradeOperation
    before_operation: UpgradeOperation
    event: UpgradeEvent
    resolved_at: AwareDatetime
    outcome: Literal["recovered_after_deadline"] = "recovered_after_deadline"
    original_upgrade_succeeded: Literal[False] = False

    @model_validator(mode="after")
    def explicit_failed_upgrade_resolution(self) -> UpgradeExpiredRecoveryColdResolution:
        UpgradeExpiredRecoveryColdResolutionRequest(
            closure=self.closure, observation=self.observation
        )
        intent = self.closure.handback.helper.handback.verification.request.smoke.smoke.source.launch.source.projection.owner.intent
        original = intent.original_plan.operation
        evidence = UpgradeEvidence.model_validate_json(self.event.evidence)
        if (
            self.before_operation.authority_sha256() != original.authority_sha256()
            or self.before_operation.phase is not UpgradePhase.RECOVERY_REQUIRED
            or self.before_operation.finished_at is not None
            or self.before_operation.receipt_sequence + 1 != self.event.sequence
            or self.operation.reason != "expired_operation_recovered"
            or self.operation.authority_sha256() != original.authority_sha256()
            or self.operation.phase is not UpgradePhase.ROLLED_BACK
            or self.operation.finished_at != self.resolved_at
            or self.operation.updated_at != self.resolved_at
            or self.operation.cancellation_allowed
            or (self.event.job_id, self.event.generation, self.event.sequence)
            != (original.job_id, original.generation, self.operation.receipt_sequence)
            or self.event.phase is not UpgradePhase.ROLLED_BACK
            or self.event.effect_id != f"expired-recovery-{intent.recovery_id}"
            or self.event.reason != "expired_operation_recovered"
            or self.event.outcome != "complete"
            or self.event.restoration != "verified"
            or self.event.occurred_at != self.resolved_at
            or self.event.observed_sha256 != intent.request.expected_source_sha256
            or not self.observation.observed_at <= self.resolved_at < intent.deadline_at
            or evidence.source_verified is not True
            or evidence.cleanup_verified is not True
            or evidence.release_sha256 != intent.request.expected_source_sha256
            or evidence.runtime_sha256 != intent.original_plan.source.manifest.environment_sha256
            or evidence.peer_runtime_sha256 != intent.original_plan.peer_runtime_sha256
            or evidence.collective_verified
            != (self.closure.handback.helper.handback.verification.request.pair is not None)
        ):
            raise ValueError("late resolution changes original authority or claims upgrade success")
        if intent.original_plan.peer_runtime_sha256 is None:
            if self.peer_release is not None:
                raise ValueError("late resolution invents an unaccepted peer")
        elif (
            self.peer_release is None
            or not self.peer_release.released
            or (self.peer_release.job_id, self.peer_release.generation, self.peer_release.node)
            != (
                original.job_id,
                original.generation,
                "coire-edge-b" if original.node == "coire-edge-a" else "coire-edge-a",
            )
            or not intent.accepted_at <= self.peer_release.applied_at <= self.resolved_at
        ):
            raise ValueError("late resolution lacks exact actual peer fence closure")
        return self


class UpgradeExpiredRecoveryColdResolutionRead(UpgradeWire):
    intent_sha256: Sha256
    closure_sha256: Sha256


class UpgradeExpiredRecoveryColdResolutionState(UpgradeWire):
    intent_sha256: Sha256
    closure_sha256: Sha256
    resolution: UpgradeExpiredRecoveryColdResolution | None = None
    checked_at: AwareDatetime

    @model_validator(mode="after")
    def exact_terminal_metadata(self) -> UpgradeExpiredRecoveryColdResolutionState:
        if self.resolution is not None:
            intent = self.resolution.closure.handback.helper.handback.verification.request.smoke.smoke.source.launch.source.projection.owner.intent
            if (
                self.intent_sha256 != intent.sha256()
                or self.closure_sha256 != self.resolution.closure.sha256()
                or not self.resolution.resolved_at <= self.checked_at
            ):
                raise ValueError("cold resolution metadata has another intent, closure or clock")
        return self


class UpgradeExpiredRecoveryObserverInput(UpgradeWire):
    """Trusted passive installed-observer input, no path or effect command."""

    request: UpgradeExpiredRecoveryRequest
    support: UpgradeRelease
    runtime: PreparedNativeRuntime

    @model_validator(mode="after")
    def exact_support(self) -> UpgradeExpiredRecoveryObserverInput:
        if self.request.support_release_id != self.support.release_id or (
            self.runtime.environment_id,
            self.runtime.environment_sha256,
        ) != (self.support.manifest.environment_id, self.support.manifest.environment_sha256):
            raise ValueError("initial observer support differs")
        return self


class UpgradeExpiredRecoveryCancelRequest(UpgradeWire):
    """Cancel exact separate authority; never release original obligations."""

    intent_sha256: Sha256


class UpgradeExpiredRecoveryCancellation(UpgradeWire):
    recovery_id: UpgradeId
    intent_sha256: Sha256
    original_job_id: UpgradeId
    original_generation: PositiveInt
    cancelled_by: UUID
    cancelled_at: AwareDatetime
    phase: Literal["recovery_required"] = "recovery_required"
    maintenance_retained: Literal[True] = True


class UpgradeExpiredRecoveryLegacyService(UpgradeWire):
    """Positive managed-service observation; not ownership transfer or cleanup."""

    target: str = Field(pattern=r"^(system|gui/[0-9]+)/com\.coire\.upgrade-guard$")
    owner_uid: int = Field(ge=0)
    abandon_process_group: Literal[True]
    process: NodeAgentProcessIdentity
    observation_sha256: Sha256
    observed_at: AwareDatetime

    @model_validator(mode="after")
    def fixed_domain(self) -> UpgradeExpiredRecoveryLegacyService:
        if self.target not in {
            "system/com.coire.upgrade-guard",
            f"gui/{self.owner_uid}/com.coire.upgrade-guard",
        }:
            raise ValueError("legacy service belongs to another launchd domain")
        return self


class UpgradeExpiredRecoveryLegacyStopIntent(UpgradeWire):
    """Persist before a fixed legacy stop; never proof that stop occurred."""

    recovery_id: UpgradeId
    consumption_sha256: Sha256
    legacy_guard: NativeGuardStatus
    successor: NodeAgentProcessIdentity
    started_at: AwareDatetime
    service: UpgradeExpiredRecoveryLegacyService | None = None
    definition_sha256: Sha256 | None = None
    preserved_source: NativeNodeProcess | None = None

    @model_validator(mode="after")
    def bound_managed_stop(self) -> UpgradeExpiredRecoveryLegacyStopIntent:
        if (
            len(
                {
                    self.service is None,
                    self.definition_sha256 is None,
                    self.preserved_source is None,
                }
            )
            != 1
        ):
            raise ValueError("managed legacy stop definition is incomplete")
        if self.service is not None and (
            self.service.process != self.legacy_guard.process
            or self.preserved_source is None
            or self.preserved_source.selected != self.legacy_guard.selected
            or not 0 <= (self.started_at - self.service.observed_at).total_seconds() <= 5
        ):
            raise ValueError("managed legacy stop service or freshness differs")
        return self


class UpgradeExpiredRecoveryLegacyStopReceipt(UpgradeWire):
    """Verified fixed service stop; not new ownership or complete physical cleanup."""

    effect: UpgradeExpiredRecoveryLegacyStopIntent
    preserved_source: NativeNodeProcess
    stopped_at: AwareDatetime

    @model_validator(mode="after")
    def completed_bound_stop(self) -> UpgradeExpiredRecoveryLegacyStopReceipt:
        if (
            self.effect.service is None
            or self.effect.definition_sha256 is None
            or self.stopped_at < self.effect.started_at
            or self.preserved_source != self.effect.preserved_source
        ):
            raise ValueError("legacy stop lacks bound service or preserved Source")
        return self


class UpgradeExpiredRecoveryLegacyStopReconciliation(UpgradeWire):
    """Fresh observed retirement, never an invented bootout acknowledgement.

    Producer must independently verify kernel exclusion, authority, actual legacy
    identity absence, service unload and exact Source preservation. DTO alone is
    neither ownership nor cleanup/serving qualification.
    """

    consumption: UpgradeExpiredRecoveryTransferConsumption
    effect: UpgradeExpiredRecoveryLegacyStopIntent
    preserved_source: NativeNodeProcess
    observed_at: AwareDatetime
    service_unloaded: Literal[True]
    legacy_identity_retired: Literal[True]

    @model_validator(mode="after")
    def observed_exact_initial_retirement(self) -> UpgradeExpiredRecoveryLegacyStopReconciliation:
        receipt = self.consumption
        if (
            self.effect.recovery_id != receipt.transfer.intent.recovery_id
            or self.effect.consumption_sha256 != receipt.sha256()
            or self.effect.legacy_guard != receipt.transfer.legacy_guard
            or self.effect.successor != receipt.consumer
            or self.effect.service is None
            or self.effect.definition_sha256 is None
            or self.preserved_source != self.effect.preserved_source
            or not receipt.consumed_at
            <= self.effect.started_at
            <= self.observed_at
            < receipt.transfer.expires_at
        ):
            raise ValueError("observed legacy retirement scope or epoch differs")
        return self


class UpgradeExpiredRecoveryOwnerClaim(UpgradeWire):
    """Claim evidence, not proof by DTO shape; producer must hold kernel exclusion.

    Actual process/module/bytes, positive retirement, authenticated current Core
    scope and Source preservation must be verified before retaining this record.
    Maintenance/serving/qualification cannot be inferred from claim publication.
    """

    consumption: UpgradeExpiredRecoveryTransferConsumption
    legacy_stop: (
        UpgradeExpiredRecoveryLegacyStopReceipt | UpgradeExpiredRecoveryLegacyStopReconciliation
    )
    process: NodeAgentProcessIdentity
    runtime: PreparedNativeRuntime
    kernel_boot_identity: ArtifactKey
    claimed_at: AwareDatetime

    @model_validator(mode="after")
    def exact_claim(self) -> UpgradeExpiredRecoveryOwnerClaim:
        receipt = self.consumption
        transfer = receipt.transfer
        effect = self.legacy_stop.effect
        support = transfer.intent.support_release.manifest
        retired_at = (
            self.legacy_stop.observed_at
            if isinstance(self.legacy_stop, UpgradeExpiredRecoveryLegacyStopReconciliation)
            else self.legacy_stop.stopped_at
        )
        if (
            isinstance(self.legacy_stop, UpgradeExpiredRecoveryLegacyStopReconciliation)
            and self.legacy_stop.consumption != receipt
        ):
            raise ValueError("observed retirement consumption differs from owner")
        if (
            self.process != receipt.consumer
            or effect.consumption_sha256 != receipt.sha256()
            or effect.recovery_id != transfer.intent.recovery_id
            or effect.legacy_guard != transfer.legacy_guard
            or effect.successor != receipt.consumer
            or (self.runtime.environment_id, self.runtime.environment_sha256)
            != (support.environment_id, support.environment_sha256)
            or not receipt.consumed_at
            <= effect.started_at
            <= retired_at
            <= self.claimed_at
            < transfer.expires_at
        ):
            raise ValueError("recovery owner claim differs from retired scope or epoch")
        return self


class UpgradeExpiredRecoverySuccessionObservation(UpgradeWire):
    """Same-kernel crash preconditions, not a new grant or absence-based takeover.

    A separately authenticated/audited succession protocol must capture authority
    before effects. Changed boot requires its own supported fresh Source protocol.
    """

    succession_id: UpgradeId
    previous_owner: UpgradeExpiredRecoveryOwnerClaim
    successor: NodeAgentProcessIdentity
    kernel_boot_identity: ArtifactKey
    preserved_source: NativeNodeProcess
    observed_at: AwareDatetime
    previous_owner_retired: Literal[True]

    @model_validator(mode="after")
    def exact_same_boot_preconditions(self) -> UpgradeExpiredRecoverySuccessionObservation:
        previous = self.previous_owner
        intent = previous.consumption.transfer.intent
        if (
            self.succession_id in {intent.recovery_id, intent.request.original_job_id}
            or (self.successor.pid, self.successor.create_time_s)
            == (previous.process.pid, previous.process.create_time_s)
            or self.successor.environment_sha256 != previous.runtime.environment_sha256
            or self.kernel_boot_identity != previous.kernel_boot_identity
            or self.preserved_source != previous.legacy_stop.preserved_source
            or not previous.claimed_at <= self.observed_at < intent.deadline_at
        ):
            raise ValueError("succession observation scope, identity, boot or epoch differs")
        return self


class UpgradeExpiredRecoverySuccessionGrant(UpgradeWire):
    """Audited scope DTO, not lifetime ownership or permission by shape alone.

    Core must separately reauthorize admin and persist this exact captured window.
    Neither replay nor crash may change the original intent's recovery deadline.
    """

    observation: UpgradeExpiredRecoverySuccessionObservation
    user_id: UUID
    key_id: UUID | None
    credential_version: Annotated[int, Field(ge=1)] | None
    accepted_at: AwareDatetime
    expires_at: AwareDatetime

    @model_validator(mode="after")
    def original_epoch_and_fresh_observation(self) -> UpgradeExpiredRecoverySuccessionGrant:
        intent = self.observation.previous_owner.consumption.transfer.intent
        if (
            (self.key_id is None) != (self.credential_version is None)
            or not 0 <= (self.accepted_at - self.observation.observed_at).total_seconds() <= 5
            or not 0 < (self.expires_at - self.accepted_at).total_seconds() <= 30
            or not intent.accepted_at <= self.accepted_at < self.expires_at <= intent.deadline_at
        ):
            raise ValueError("succession grant credential, observation or window differs")
        return self


class UpgradeExpiredRecoverySuccessorClaim(UpgradeWire):
    """Distinct successor preimage: shape alone is never kernel ownership.

    A trusted producer must retain the actual lifetime exclusion capability and
    repeat fresh Core/physical gates. The original consumption is preserved,
    never rewritten to make the successor look like the initial consumer.
    """

    grant: UpgradeExpiredRecoverySuccessionGrant
    process: NodeAgentProcessIdentity
    runtime: PreparedNativeRuntime
    kernel_boot_identity: str = Field(min_length=1, max_length=128)
    preserved_source: NativeNodeProcess
    claimed_at: AwareDatetime

    @model_validator(mode="after")
    def exact_successor_scope(self) -> UpgradeExpiredRecoverySuccessorClaim:
        proof = self.grant.observation
        if (
            self.process != proof.successor
            or self.runtime != proof.previous_owner.runtime
            or self.kernel_boot_identity != proof.kernel_boot_identity
            or self.preserved_source != proof.preserved_source
            or not self.grant.accepted_at <= self.claimed_at < self.grant.expires_at
        ):
            raise ValueError("successor claim differs from exact grant or initial window")
        return self


class UpgradeExpiredRecoverySuccessorPublicationIntent(UpgradeWire):
    """Write-ahead successor transition; not an activation or serving proof."""

    owner: UpgradeExpiredRecoverySuccessorClaim
    original_claim: NativeUpgradeClaim
    before_runtime: PreparedNativeRuntime
    before_status: NativeGuardStatus
    published_status: NativeGuardStatus

    @model_validator(mode="after")
    def exact_successor_transition(self) -> UpgradeExpiredRecoverySuccessorPublicationIntent:
        previous = self.owner.grant.observation.previous_owner
        legacy = previous.consumption.transfer.legacy_guard
        intent = previous.consumption.transfer.intent
        plan = intent.original_plan
        claim = self.original_claim
        before = self.before_status
        status = self.published_status
        legacy_before = (before.process, before.bootstrap_runtime, before.selected) == (
            legacy.process,
            legacy.bootstrap_runtime,
            legacy.selected,
        )
        owner_before = (
            before.process == previous.process
            and before.bootstrap_runtime == previous.runtime
            and before.selected == previous.legacy_stop.preserved_source.selected
            and not before.ready
        )
        if (
            (claim.job_id, claim.generation, claim.node, claim.plan_sha256, claim.state)
            != (
                plan.operation.job_id,
                plan.operation.generation,
                legacy.node,
                plan.authority_sha256(),
                "active",
            )
            or self.before_runtime not in (legacy.bootstrap_runtime, previous.runtime)
            or before.node != legacy.node
            or not (legacy_before or owner_before)
            or status.ready
            or status.node != legacy.node
            or status.process != self.owner.process
            or status.bootstrap_runtime != self.owner.runtime
            or status.selected != self.owner.preserved_source.selected
            or not self.owner.claimed_at <= status.observed_at < intent.deadline_at
        ):
            raise ValueError("successor publication differs from exact recorded unready transition")
        return self


class UpgradeExpiredRecoverySuccessorControllerStatus(UpgradeWire):
    owner: UpgradeExpiredRecoverySuccessorClaim
    observed_at: AwareDatetime
    ready: Literal[False] = False

    @model_validator(mode="after")
    def captured_original_budget(self) -> UpgradeExpiredRecoverySuccessorControllerStatus:
        intent = self.owner.grant.observation.previous_owner.consumption.transfer.intent
        if not self.owner.claimed_at <= self.observed_at < intent.deadline_at:
            raise ValueError("successor controller heartbeat exceeds original recovery budget")
        return self


class UpgradeExpiredRecoverySuccessorCheck(UpgradeWire):
    claim_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    consumer: NodeAgentProcessIdentity


class UpgradeExpiredRecoverySuccessorStateCheck(UpgradeExpiredRecoverySuccessorCheck):
    grant_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class UpgradeExpiredRecoverySuccessorState(UpgradeWire):
    """Positive authenticated inspection, never error-based initial fallback."""

    claim_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    owner: UpgradeExpiredRecoverySuccessorClaim | None
    checked_at: AwareDatetime

    @model_validator(mode="after")
    def exact_successor_state(self) -> UpgradeExpiredRecoverySuccessorState:
        if self.owner is not None:
            intent = self.owner.grant.observation.previous_owner.consumption.transfer.intent
            if (
                self.owner.sha256() != self.claim_sha256
                or not self.owner.claimed_at <= self.checked_at < intent.deadline_at
            ):
                raise ValueError("successor state differs from accepted owner or original epoch")
        return self


class UpgradeExpiredRecoveryPublicationIntent(UpgradeWire):
    """Write-ahead bootstrap preimages; support status must remain unready."""

    owner: UpgradeExpiredRecoveryOwnerClaim
    original_claim: NativeUpgradeClaim
    before_runtime: PreparedNativeRuntime
    before_status: NativeGuardStatus
    published_status: NativeGuardStatus

    @model_validator(mode="after")
    def exact_unready_transition(self) -> UpgradeExpiredRecoveryPublicationIntent:
        legacy = self.owner.consumption.transfer.legacy_guard
        plan = self.owner.consumption.transfer.intent.original_plan
        claim = self.original_claim
        status = self.published_status
        if (
            (claim.job_id, claim.generation, claim.node, claim.plan_sha256, claim.state)
            != (
                plan.operation.job_id,
                plan.operation.generation,
                legacy.node,
                plan.authority_sha256(),
                "active",
            )
            or self.before_runtime != legacy.bootstrap_runtime
            or (
                self.before_status.process,
                self.before_status.bootstrap_runtime,
                self.before_status.selected,
            )
            != (legacy.process, legacy.bootstrap_runtime, legacy.selected)
            or status.ready
            or status.node != legacy.node
            or status.process != self.owner.process
            or status.bootstrap_runtime != self.owner.runtime
            or status.selected != self.owner.legacy_stop.preserved_source.selected
            or not self.owner.claimed_at
            <= status.observed_at
            < self.owner.consumption.transfer.intent.deadline_at
        ):
            raise ValueError("support publication differs from frozen unready transition")
        return self


class UpgradeExpiredRecoveryControllerStatus(UpgradeWire):
    """Holding controller heartbeat, never Source readiness or qualification."""

    owner: UpgradeExpiredRecoveryOwnerClaim
    ready: Literal[False] = False
    observed_at: AwareDatetime

    @model_validator(mode="after")
    def inside_recovery_epoch(self) -> UpgradeExpiredRecoveryControllerStatus:
        if (
            not self.owner.claimed_at
            <= self.observed_at
            < self.owner.consumption.transfer.intent.deadline_at
        ):
            raise ValueError("controller observation outside captured recovery budget")
        return self


class UpgradeExpiredRecoverySupportStartInput(UpgradeWire):
    """Fixed installed launch input; no argv, environment, path or extra budget."""

    intent: UpgradeExpiredRecoveryIntent
    runtime: PreparedNativeRuntime


class UpgradeExpiredRecoverySupportStartup(UpgradeWire):
    """Immutable verified installed support context, not launch/effect authority."""

    intent: UpgradeExpiredRecoveryIntent
    runtime: PreparedNativeRuntime
    prepared_at: AwareDatetime

    @model_validator(mode="after")
    def exact_support_context(self) -> UpgradeExpiredRecoverySupportStartup:
        support = self.intent.support_release.manifest
        if (self.runtime.environment_id, self.runtime.environment_sha256) != (
            support.environment_id,
            support.environment_sha256,
        ) or not self.intent.accepted_at <= self.prepared_at < self.intent.deadline_at:
            raise ValueError("support startup differs from captured context")
        return self


class UpgradeExpiredRecoverySupportLaunchIntent(UpgradeWire):
    """Actual launcher identity retained before spawn; no ownership grant."""

    startup: UpgradeExpiredRecoverySupportStartup
    launcher: NodeAgentProcessIdentity
    started_at: AwareDatetime

    @model_validator(mode="after")
    def captured_launcher(self) -> UpgradeExpiredRecoverySupportLaunchIntent:
        if (
            self.launcher.environment_sha256 != self.startup.runtime.environment_sha256
            or not self.startup.prepared_at <= self.started_at < self.startup.intent.deadline_at
            or self.launcher.create_time_s > self.started_at.timestamp()
        ):
            raise ValueError("recovery launcher identity or captured window differs")
        return self


class UpgradeExpiredRecoverySupportProcess(UpgradeWire):
    """Verified passive support identity, not serving or transferred ownership."""

    intent: UpgradeExpiredRecoveryIntent
    runtime: PreparedNativeRuntime
    process: NodeAgentProcessIdentity
    observed_at: AwareDatetime

    @model_validator(mode="after")
    def passive_binding(self) -> UpgradeExpiredRecoverySupportProcess:
        support = self.intent.support_release.manifest
        if (
            (self.runtime.environment_id, self.runtime.environment_sha256)
            != (support.environment_id, support.environment_sha256)
            or self.process.environment_sha256 != self.runtime.environment_sha256
            or not self.intent.accepted_at <= self.observed_at < self.intent.deadline_at
        ):
            raise ValueError("passive support identity differs from captured context")
        return self


class UpgradeExpiredRecoverySupportContextRead(UpgradeWire):
    """Fixed startup context read; no caller paths, budgets or credentials."""

    protocol: Literal[1] = 1


class UpgradeExpiredRecoveryConsumptionDeliveryRead(UpgradeWire):
    """Fixed delivery request; no paths, deadlines or credential fields."""

    intent_sha256: Sha256
    consumer: NodeAgentProcessIdentity


class UpgradeExpiredRecoveryConsumptionState(UpgradeWire):
    """Fresh pending/consumed delivery state, never inferred from an error."""

    intent_sha256: Sha256
    consumption: UpgradeExpiredRecoveryTransferConsumption | None
    checked_at: AwareDatetime

    @model_validator(mode="after")
    def exact_consumed_intent(self) -> UpgradeExpiredRecoveryConsumptionState:
        if (
            self.consumption is not None
            and self.consumption.transfer.intent.sha256() != self.intent_sha256
        ):
            raise ValueError("consumption state differs from captured intent")
        return self


class UpgradeExpiredRecoveryOwnerState(UpgradeWire):
    """Fresh accepted/unaccepted result, never infer from generic failure.

    An unaccepted observation is not an effect grant; initial transfer gates
    remain mandatory. Accepted state still requires actual lifetime ownership.
    """

    owner_sha256: Sha256
    owner: UpgradeExpiredRecoveryOwnerClaim | None
    checked_at: AwareDatetime

    @model_validator(mode="after")
    def canonical_owner(self) -> UpgradeExpiredRecoveryOwnerState:
        if self.owner is not None and self.owner.sha256() != self.owner_sha256:
            raise ValueError("accepted owner state differs from requested scope")
        return self


class UpgradeExpiredRecoveryOwnerCheck(UpgradeWire):
    """Fixed-node fresh owner callback; no caller deadline or target URL."""

    owner_sha256: Sha256
    consumer: NodeAgentProcessIdentity


class UpgradeExpiredRecoveryConsumptionCheck(UpgradeWire):
    """Fixed-node callback bindings only; no URL, credentials or effect argv."""

    consumption_sha256: Sha256
    consumer: NodeAgentProcessIdentity


class UpgradeExpiredRecoveryTransferConsumption(UpgradeWire):
    """Core-produced one-time consumption evidence, not completed native handover.

    Receiving a structurally valid receipt is insufficient: transport and durable
    Core/local provenance, current admin authority and physical ownership must be
    verified by producers. No receipt changes original deadlines or fencing.
    """

    consumption_id: UUID
    transfer: UpgradeExpiredRecoveryOwnerTransfer
    consumer: NodeAgentProcessIdentity
    consumed_at: AwareDatetime

    @model_validator(mode="after")
    def exact_consumer_epoch(self) -> UpgradeExpiredRecoveryTransferConsumption:
        if (
            self.consumer != self.transfer.successor
            or not self.transfer.issued_at <= self.consumed_at < self.transfer.expires_at
        ):
            raise ValueError("recovery transfer consumer or captured window differs")
        return self


class UpgradeExpiredRecoveryOwnerTransfer(UpgradeWire):
    """Retain before handover; not an authenticated effect grant or completion.

    Producers must reauthorize the admin, verify both process identities and
    exclusive kernel/launchd ownership, retain fences and consume once durably.
    """

    transfer_id: UUID
    intent: UpgradeExpiredRecoveryIntent
    legacy_guard: NativeGuardStatus
    successor: NodeAgentProcessIdentity
    issued_at: AwareDatetime
    expires_at: AwareDatetime

    @model_validator(mode="after")
    def exact_owner_epoch(self) -> UpgradeExpiredRecoveryOwnerTransfer:
        plan = self.intent.original_plan
        if (
            self.legacy_guard.node != plan.operation.node
            or self.legacy_guard.selected.release_sha256 != plan.source.manifest_sha256
            or self.legacy_guard.selected.runtime.environment_sha256
            != plan.source.manifest.environment_sha256
            or self.successor.environment_sha256
            != self.intent.support_release.manifest.environment_sha256
            or self.successor.pid == self.legacy_guard.process.pid
            or not self.intent.accepted_at
            <= self.issued_at
            < self.expires_at
            <= self.intent.deadline_at
            or self.expires_at > self.issued_at + timedelta(seconds=30)
            or not 0 <= (self.issued_at - self.legacy_guard.observed_at).total_seconds() <= 5
        ):
            raise ValueError("recovery transfer owner or bounded epoch differs")
        return self


class UpgradeExpiredRecoveryObservation(UpgradeWire):
    """Passive retained metadata; not Source-serving or cleanup qualification.

    Producers must independently verify installed observer bytes and authenticate
    transport. Consumers must check freshness and recheck live Core authority.
    """

    request: UpgradeExpiredRecoveryRequest
    plan: UpgradeExecutionPlan
    native: NativeRecoveryIntent
    claim: NativeUpgradeClaim
    scopes: tuple[NodeUpgradeCollectiveScope, ...] = Field(max_length=6)
    native_data: UpgradeExpiredRecoveryNativeDataSnapshot | None = None
    support: UpgradeRelease
    observer_runtime: PreparedNativeRuntime
    observed_at: AwareDatetime

    @model_validator(mode="after")
    def retained_authority(self) -> UpgradeExpiredRecoveryObservation:
        request = self.request
        authority = (
            request.original_job_id,
            request.original_generation,
            request.original_plan_sha256,
        )
        if (
            self.claim.state != "active"
            or self.native.phase
            not in {"recovery_required", "restoring", "source_selected", "source_verified"}
            or (self.claim.job_id, self.claim.generation, self.claim.plan_sha256) != authority
            or (self.native.job_id, self.native.generation, self.native.plan_sha256) != authority
            or (
                self.plan.operation.job_id,
                self.plan.operation.generation,
                self.plan.authority_sha256(),
            )
            != authority
            or self.claim.node != self.plan.operation.node
            or self.native.source.node != self.claim.node
            or self.native.source.release_sha256 != request.expected_source_sha256
            or self.plan.source.manifest_sha256 != request.expected_source_sha256
            or sorted(scope.sha256() for scope in self.scopes)
            != sorted(request.original_scope_sha256s)
            or any(
                scope.plan.authority_sha256() != request.original_plan_sha256
                for scope in self.scopes
            )
            or self.support.release_id != request.support_release_id
            or (self.observer_runtime.environment_id, self.observer_runtime.environment_sha256)
            != (self.support.manifest.environment_id, self.support.manifest.environment_sha256)
            or self.native.recovery_deadline_at is None
            or self.native.recovery_deadline_at > self.observed_at
        ):
            raise ValueError("retained recovery observation authority differs")
        if self.native_data is not None and (
            (
                self.native_data.original_job_id,
                self.native_data.original_generation,
                self.native_data.node,
                self.native_data.source_sha256,
            )
            != (
                request.original_job_id,
                request.original_generation,
                self.claim.node,
                request.expected_source_sha256,
            )
            or not 0 <= (self.observed_at - self.native_data.observed_at).total_seconds() <= 5
        ):
            raise ValueError("retained native data observation differs from original authority")
        return self


class UpgradeExpiredRecoveryIntent(UpgradeWire):
    """Immutable server-captured intent; effect authorization remains separate."""

    recovery_id: UpgradeId
    request: UpgradeExpiredRecoveryRequest
    original_plan: UpgradeExecutionPlan
    support_release: UpgradeRelease
    accepted_by: UUID
    accepted_at: datetime
    original_recovery_deadline_at: datetime
    deadline_at: datetime

    local_recovery_maintenance: UpgradeMaintenance | None = Field(
        default=None, exclude_if=lambda v: v is None
    )
    inherited_local_maintenance: UpgradeMaintenance | None = Field(
        default=None, exclude_if=lambda v: v is None
    )

    def local_maintenance(self) -> UpgradeMaintenance:
        return (
            self.local_recovery_maintenance
            or self.inherited_local_maintenance
            or self.original_plan.maintenance
        )

    def local_inventory_plan(self) -> UpgradeExecutionPlan:
        fence = self.local_maintenance()
        return self.original_plan.model_copy(
            update={
                "maintenance": fence,
                "operation": self.original_plan.operation.model_copy(
                    update={
                        "job_id": fence.job_id,
                        "generation": fence.generation,
                    }
                ),
            }
        )

    @model_validator(mode="after")
    def captured_authority(self) -> UpgradeExpiredRecoveryIntent:
        request = self.request
        plan = self.original_plan
        if self.recovery_id == request.original_job_id:
            raise ValueError("recovery requires distinct authority identity")
        if (
            plan.operation.job_id != request.original_job_id
            or plan.operation.generation != request.original_generation
            or plan.authority_sha256() != request.original_plan_sha256
            or plan.source.manifest_sha256 != request.expected_source_sha256
            or self.support_release.release_id != request.support_release_id
            or self.support_release.manifest.kind != plan.operation.kind
            or self.support_release.manifest.platform != plan.source.manifest.platform
            or plan.source.manifest_sha256
            not in self.support_release.manifest.compatibility.source_sha256s
        ):
            raise ValueError("recovery intent original or support authority differs")
        for instant in (self.accepted_at, self.original_recovery_deadline_at, self.deadline_at):
            if instant.tzinfo is None or instant.utcoffset() is None:
                raise ValueError("recovery intent timestamps require timezone")
        if self.original_recovery_deadline_at > self.accepted_at:
            raise ValueError("original recovery epoch has not expired")
        if self.deadline_at != self.accepted_at + timedelta(seconds=request.recovery_s):
            raise ValueError("recovery intent deadline differs from captured budget")
        fence = self.local_recovery_maintenance
        inherited = self.inherited_local_maintenance
        if inherited is not None and (
            fence is not None
            or request.recovery_mode != "cold"
            or inherited.job_id in {self.recovery_id, request.original_job_id}
            or inherited.generation <= request.original_generation
            or inherited.targets != (plan.operation.node,)
            or inherited.state != "active"
            or inherited.updated_at > self.accepted_at
            or inherited.drain_deadline_at > self.accepted_at
        ):
            raise ValueError("inherited recovery fence changes its expired scope")
        if fence is not None and (
            request.recovery_mode != "cold"
            or fence.job_id != self.recovery_id
            or fence.generation <= request.original_generation
            or fence.targets != (plan.operation.node,)
            or fence.state != "active"
            or fence.updated_at != self.accepted_at
            or fence.drain_deadline_at != self.deadline_at
            or fence.initiator_user_id != self.accepted_by
        ):
            raise ValueError("local recovery fence changes separately captured authority")
        return self


class UpgradeExpiredRecoveryStatus(UpgradeWire):
    """Durable administrative inspection; no executable owner capability."""

    intent: UpgradeExpiredRecoveryIntent
    phase: Literal[
        "accepted",
        "transferring",
        "transfer_consumed",
        "recovering",
        "verifying",
        "cold_recovering",
        "handback_prepared",
        "handing_back",
        "resolving",
        "recovery_required",
        "recovered",
        "cancelled_unchanged",
        "superseded",
    ]
    active: bool
    cancellation: UpgradeExpiredRecoveryCancellation | None = None
    owner_sha256: Sha256 | None = None
    observed_at: AwareDatetime

    @model_validator(mode="after")
    def exact_status(self) -> UpgradeExpiredRecoveryStatus:
        if self.active != (self.phase not in {"recovered", "cancelled_unchanged", "superseded"}):
            raise ValueError("recovery status active slot differs")
        if self.cancellation is not None and (
            self.cancellation.recovery_id != self.intent.recovery_id
            or self.cancellation.intent_sha256 != self.intent.sha256()
            or self.phase not in {"recovery_required", "recovered", "superseded"}
        ):
            raise ValueError("recovery status cancellation differs")
        return self


class UpgradeExpiredRecoverySuccessionDeliveryRead(UpgradeWire):
    """Fixed passive successor inspection, never a caller destination or authority."""

    protocol: Literal[1] = 1
    succession_id: UpgradeId
    predecessor_sha256: Sha256
    successor: NodeAgentProcessIdentity


class UpgradeExpiredRecoverySuccessionDeliveryState(UpgradeWire):
    """Explicit pending/grant result; neither grants native effect permission."""

    protocol: Literal[1] = 1
    request: UpgradeExpiredRecoverySuccessionDeliveryRead
    grant: UpgradeExpiredRecoverySuccessionGrant | None
    checked_at: AwareDatetime

    @model_validator(mode="after")
    def exact_delivery(self) -> UpgradeExpiredRecoverySuccessionDeliveryState:
        if self.grant is not None:
            proof = self.grant.observation
            if (
                proof.succession_id != self.request.succession_id
                or proof.previous_owner.sha256() != self.request.predecessor_sha256
                or proof.successor != self.request.successor
                or not self.grant.accepted_at <= self.checked_at < self.grant.expires_at
            ):
                raise ValueError("succession delivery scope or initial epoch differs")
        return self


class UpgradeExpiredRecoveryUnstartedRead(UpgradeWire):
    intent_sha256: Sha256


class UpgradeExpiredRecoveryUnstartedObserverInput(UpgradeWire):
    intent: UpgradeExpiredRecoveryIntent
    observer_release: UpgradeRelease
    observer_runtime: PreparedNativeRuntime


class UpgradeExpiredRecoveryUnstartedObservation(UpgradeWire):
    """Retired startup and retained original preimages; no cleanup/serving claim."""

    intent: UpgradeExpiredRecoveryIntent
    startup: UpgradeExpiredRecoverySupportProcess
    observer_release: UpgradeRelease
    observer_runtime: PreparedNativeRuntime
    observer: NodeAgentProcessIdentity
    native: NativeRecoveryIntent
    claim: NativeUpgradeClaim
    guard: NativeGuardStatus
    node: NativeNodeProcess
    pointer: PreparedNativeRuntime
    bootstrap_runtime: PreparedNativeRuntime
    native_data: UpgradeExpiredRecoveryNativeDataSnapshot
    startup_retired: Literal[True] = True
    original_maintenance_retained: Literal[True] = True
    observed_at: AwareDatetime

    @model_validator(mode="after")
    def exact_unstarted_scope(self) -> UpgradeExpiredRecoveryUnstartedObservation:
        intent, plan = self.intent, self.intent.original_plan
        scope = (plan.operation.job_id, plan.operation.generation, plan.authority_sha256())
        support = self.observer_release.manifest
        runtimes = (self.native.source.runtime, self.native.candidate)
        if (
            intent.request.recovery_mode != "cold"
            or self.startup.intent != intent
            or self.startup.process == self.observer
            or self.observer.environment_sha256 != self.observer_runtime.environment_sha256
            or self.observer_release.status != "prepared"
            or (self.observer_runtime.environment_id, self.observer_runtime.environment_sha256)
            != (support.environment_id, support.environment_sha256)
            or support.kind != plan.source.manifest.kind
            or support.platform != plan.source.manifest.platform
            or plan.source.manifest_sha256 not in support.compatibility.source_sha256s
            or (self.native.job_id, self.native.generation, self.native.plan_sha256) != scope
            or (self.claim.job_id, self.claim.generation, self.claim.plan_sha256) != scope
            or self.claim.state != "active"
            or self.native.recovery_deadline_at != intent.original_recovery_deadline_at
            or self.native.source.release_sha256 != plan.source.manifest_sha256
            or (runtimes[0].environment_id, runtimes[0].environment_sha256)
            != (plan.source.manifest.environment_id, plan.source.manifest.environment_sha256)
            or (runtimes[1].environment_id, runtimes[1].environment_sha256)
            != (plan.target.manifest.environment_id, plan.target.manifest.environment_sha256)
            or self.pointer not in runtimes
            or self.guard.node != plan.operation.node
            or self.claim.node != plan.operation.node
            or self.node.selected.release_sha256
            not in {plan.source.manifest_sha256, plan.target.manifest_sha256}
            or self.guard.selected.release_sha256
            not in {plan.source.manifest_sha256, plan.target.manifest_sha256}
            or self.observed_at < intent.deadline_at
            or self.native_data.observed_at > self.observed_at
            or self.native_data.original_job_id != plan.operation.job_id
            or self.native_data.original_generation != plan.operation.generation
            or self.native_data.source_sha256 != plan.source.manifest_sha256
            or self.native_data.node != plan.operation.node
        ):
            raise ValueError("unstarted recovery observation changes original authority or scope")
        return self


class UpgradeExpiredRecoveryUnstartedCloseRequest(UpgradeWire):
    intent_sha256: Sha256
    observation_sha256: Sha256
    idempotency_key: str = Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_.:-]+$")


class UpgradeExpiredRecoveryUnstartedClosure(UpgradeWire):
    request_sha256: Sha256
    intent_sha256: Sha256
    observation_sha256: Sha256
    recovery_id: UpgradeId
    original_job_id: UpgradeId
    original_generation: PositiveInt
    source_sha256: Sha256
    startup_sha256: Sha256
    observer_release_id: UUID
    original_deadline_at: AwareDatetime
    recovery_deadline_at: AwareDatetime
    closed_by: UUID
    closed_at: AwareDatetime
    phase: Literal["cancelled_unchanged"] = "cancelled_unchanged"
    original_maintenance_retained: Literal[True] = True
    original_upgrade_succeeded: Literal[False] = False

    @model_validator(mode="after")
    def unchanged_failure(self) -> UpgradeExpiredRecoveryUnstartedClosure:
        if (
            self.recovery_id == self.original_job_id
            or not self.original_deadline_at < self.recovery_deadline_at <= self.closed_at
        ):
            raise ValueError("unstarted closure has no exact fresh observed scope")
        return self
