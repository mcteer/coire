"""Typed cold crash continuation; metadata never renews expired effect authority."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from coire_core.models.upgrade_recovery import (
    UpgradeExpiredRecoveryColdClaim,
    UpgradeExpiredRecoveryColdCollectiveArchiveReceipt,
    UpgradeExpiredRecoveryColdHandbackHelper,
    UpgradeExpiredRecoveryColdHandbackIntent,
    UpgradeExpiredRecoveryColdHandbackReceipt,
    UpgradeExpiredRecoveryColdPairResult,
    UpgradeExpiredRecoveryColdSmokeCommand,
    UpgradeExpiredRecoveryColdSmokeReceipt,
    UpgradeExpiredRecoveryColdSourceProcess,
    UpgradeExpiredRecoveryDataSnapshot,
    UpgradeExpiredRecoveryIntent,
    UpgradeExpiredRecoveryNativeDataSnapshot,
    UpgradeExpiredRecoveryRequest,
    UpgradeExpiredRecoverySupportProcess,
)
from coire_core.models.upgrade_recovery_cold_binding import UpgradeColdContinuationBinding
from coire_core.models.upgrades import (
    NodeAgentProcessIdentity,
    NodeUpgradeCollectiveScope,
    NodeUpgradeInventory,
    PreparedNativeRuntime,
    Sha256,
    UpgradeId,
    UpgradeRelease,
    UpgradeWire,
)


class UpgradeColdContinuationRead(UpgradeWire):
    previous_intent_sha256: Sha256
    previous_owner_sha256: Sha256


class UpgradeColdContinuationObservation(UpgradeWire):
    binding: UpgradeColdContinuationBinding
    previous_owner: UpgradeExpiredRecoveryColdClaim
    previous_source: UpgradeExpiredRecoveryColdSourceProcess
    previous_archive: UpgradeExpiredRecoveryColdCollectiveArchiveReceipt | None = Field(
        default=None, exclude_if=lambda v: v is None
    )
    previous_smoke: UpgradeExpiredRecoveryColdSmokeReceipt | None = Field(
        default=None, exclude_if=lambda v: v is None
    )
    previous_pair_results: tuple[UpgradeExpiredRecoveryColdPairResult, ...] | None = Field(
        default=None, max_length=3, exclude_if=lambda v: v is None
    )
    observer_release: UpgradeRelease
    observer_runtime: PreparedNativeRuntime
    observer: NodeAgentProcessIdentity
    native_baseline: UpgradeExpiredRecoveryNativeDataSnapshot
    native_data: UpgradeExpiredRecoveryNativeDataSnapshot
    scopes: tuple[NodeUpgradeCollectiveScope, ...] = Field(max_length=6)
    interrupted_scope_sha256s: tuple[Sha256, ...] = Field(max_length=6)
    local: NodeUpgradeInventory | None
    peer: NodeUpgradeInventory
    observed_at: AwareDatetime
    unstarted_startup: UpgradeExpiredRecoverySupportProcess | None = Field(
        default=None, exclude_if=lambda v: v is None
    )

    previous_unreceipted_helper: UpgradeExpiredRecoveryColdHandbackHelper | None = Field(
        default=None, exclude_if=lambda v: v is None
    )

    @model_validator(mode="after")
    def exact_metadata(self) -> UpgradeColdContinuationObservation:
        binding = self.binding
        owner = self.previous_owner
        intent = owner.intent
        source = self.previous_source
        plan = intent.original_plan
        manifest = self.observer_release.manifest
        unstarted = self.unstarted_startup
        if unstarted is not None and (
            unstarted.intent.recovery_id == intent.recovery_id
            or unstarted.intent.original_plan.authority_sha256() != plan.authority_sha256()
            or unstarted.intent.deadline_at > self.observed_at
        ):
            raise ValueError("cold unstarted metadata changes Source or expired custody")
        if self.local is None and binding.post_handback is None:
            raise ValueError("absent local inventory requires verified released handback")
        before, after = self.native_baseline, self.native_data
        if (
            binding.post_handback is not None
            and (plan.source.manifest.paired_required or plan.target.manifest.paired_required)
            and binding.previous_pair_qualification_sha256 is None
        ):
            raise ValueError("post-handback continuation has no required historical qualification")
        partial = self.previous_unreceipted_helper
        late = binding.post_handback
        if (partial is not None) != (late is not None and late.stage == "ready_unreceipted") or (
            partial is not None
            and (
                late is None
                or partial.sha256() != late.helper_sha256
                or partial.handback.sha256() != late.handback_sha256
                or partial.handback.verification.request.smoke.smoke.source != source
                or partial.process != late.source_helper.process
                or partial.handback.released_claim != late.released_claim
            )
        ):
            raise ValueError("unreceipted handback metadata changes exact helper provenance")
        archive = self.previous_archive
        if archive is None:
            if (
                binding.previous_archive_sha256 is not None
                or binding.previous_smoke_sha256 is not None
                or self.previous_smoke is not None
                or self.previous_pair_results is not None
            ):
                raise ValueError("cold continuation has unbound later-stage evidence")
        elif (
            binding.previous_archive_sha256 != archive.sha256()
            or binding.previous_history_sha256 != archive.archive.history_sha256
            or archive.archive.source != source
            or binding.previous_smoke_sha256
            != (self.previous_smoke.sha256() if self.previous_smoke is not None else None)
            or self.previous_pair_results is None
            or binding.previous_pair_result_sha256s
            != tuple(result.sha256() for result in self.previous_pair_results)
            or (self.previous_smoke is not None and self.previous_smoke.smoke.source != source)
            or any(
                result.dispatch.archive != archive or result.dispatch.smoke != self.previous_smoke
                for result in self.previous_pair_results
            )
            or (
                binding.previous_pair_qualification_sha256 is not None
                and (
                    len(self.previous_pair_results) != 3
                    or not all(result.qualified() for result in self.previous_pair_results)
                )
            )
            or any(
                result.dispatch.index != index
                for index, result in enumerate(self.previous_pair_results)
            )
        ):
            raise ValueError("cold continuation archive or failed-pair evidence differs")
        if (
            owner.supervision != "fixture"
            or owner.retired_supervisor is None
            or binding.retired_supervisor_sha256 != owner.retired_supervisor.sha256()
            or plan.operation.node != "coire-edge-a"
            or binding.previous_recovery_id != intent.recovery_id
            or binding.previous_intent_sha256 != intent.sha256()
            or binding.previous_owner_sha256 != owner.sha256()
            or binding.previous_source_sha256 != source.sha256()
            or source.launch.source.projection.owner != owner
            or binding.previous_deadline_at != intent.deadline_at
            or binding.original_claim != owner.original_claim
            or binding.original_native != owner.original_native
            or binding.projected_native != source.launch.source.projection.projected_native
            or binding.previous_kernel_boot_identity != owner.kernel_boot_identity
            or binding.retained_node.process != source.process
            or binding.retained_node.selected != source.launch.source.projection.source_selection
            or binding.native_data_baseline_sha256 != before.sha256()
            or binding.observed_at != self.observed_at
            or not intent.deadline_at <= after.observed_at <= self.observed_at
            or (manifest.environment_id, manifest.environment_sha256)
            != (self.observer_runtime.environment_id, self.observer_runtime.environment_sha256)
            or self.observer.environment_sha256 != self.observer_runtime.environment_sha256
            or manifest.kind != "studio"
            or plan.source.manifest_sha256 not in manifest.compatibility.source_sha256s
            or (
                before.original_job_id,
                before.original_generation,
                before.source_sha256,
                before.node,
            )
            != (
                plan.operation.job_id,
                plan.operation.generation,
                plan.source.manifest_sha256,
                plan.operation.node,
            )
            or (after.original_job_id, after.original_generation, after.source_sha256, after.node)
            != (
                before.original_job_id,
                before.original_generation,
                before.source_sha256,
                before.node,
            )
            or (before.root_sha256s, before.file_counts, before.total_bytes)
            != (after.root_sha256s, after.file_counts, after.total_bytes)
        ):
            raise ValueError("cold continuation changes predecessor, Source or preserved data")
        for snapshot, node, runtime in (
            (
                self.local,
                plan.operation.node,
                owner.original_native.source.runtime.environment_sha256,
            ),
            (
                self.peer,
                "coire-edge-b" if plan.operation.node == "coire-edge-a" else "coire-edge-a",
                plan.peer_runtime_sha256,
            ),
        ):
            if snapshot is None:
                continue
            if (snapshot.job_id, snapshot.generation, snapshot.node, snapshot.runtime_sha256) != (
                (
                    intent.local_maintenance().job_id
                    if node == plan.operation.node
                    else plan.operation.job_id
                ),
                (
                    intent.local_maintenance().generation
                    if node == plan.operation.node
                    else plan.operation.generation
                ),
                node,
                runtime,
            ) or not intent.deadline_at <= snapshot.observed_at <= self.observed_at:
                raise ValueError("cold continuation inventory changes exact physical scope")
        return self


class UpgradeColdContinuationRequest(UpgradeWire):
    previous_recovery_id: UpgradeId
    previous_intent_sha256: Sha256
    previous_owner_sha256: Sha256
    observation_sha256: Sha256
    request: UpgradeExpiredRecoveryRequest

    @model_validator(mode="after")
    def distinct_cold_request(self) -> UpgradeColdContinuationRequest:
        if (
            self.previous_recovery_id == self.request.original_job_id
            or self.request.recovery_mode != "cold"
        ):
            raise ValueError("cold continuation requires a distinct predecessor and cold request")
        return self


class UpgradeColdContinuationGrant(UpgradeWire):
    protocol: Literal["cold-source-continuation-v1"] = "cold-source-continuation-v1"
    grant_id: UpgradeId
    request_sha256: Sha256
    intent: UpgradeExpiredRecoveryIntent
    binding: UpgradeColdContinuationBinding
    issued_by: UUID
    issued_at: AwareDatetime
    unstarted_startup: UpgradeExpiredRecoverySupportProcess | None = Field(
        default=None, exclude_if=lambda v: v is None
    )

    @model_validator(mode="after")
    def separate_captured_epoch(self) -> UpgradeColdContinuationGrant:
        intent = self.intent
        binding = self.binding
        unstarted = self.unstarted_startup
        if unstarted is not None and (
            unstarted.intent.recovery_id in {intent.recovery_id, binding.previous_recovery_id}
            or unstarted.intent.original_plan.authority_sha256()
            != intent.original_plan.authority_sha256()
            or unstarted.intent.deadline_at > intent.accepted_at
        ):
            raise ValueError("unstarted lineage changes original scope or revives an old budget")
        if (
            intent.inherited_local_maintenance != binding.previous_local_recovery_maintenance
            or (intent.local_recovery_maintenance is not None)
            != (binding.post_handback is not None)
            or self.grant_id in {intent.recovery_id, binding.previous_recovery_id}
            or intent.recovery_id == binding.previous_recovery_id
            or self.issued_by != intent.accepted_by
            or intent.request.recovery_mode != "cold"
            or binding.original_claim.job_id != intent.request.original_job_id
            or binding.original_claim.generation != intent.request.original_generation
            or binding.original_claim.plan_sha256 != intent.request.original_plan_sha256
            or binding.original_native.source.release_sha256
            != intent.request.expected_source_sha256
            or binding.original_native.recovery_deadline_at != intent.original_recovery_deadline_at
            or not binding.previous_deadline_at
            <= binding.observed_at
            <= intent.accepted_at
            <= self.issued_at
            < intent.deadline_at
        ):
            raise ValueError("cold continuation changes original authority or captured epochs")
        return self


class UpgradeColdRecoverySupersession(UpgradeWire):
    protocol: Literal["cold-source-continuation-v1", "cold-unstarted-supersession-v1"] = (
        "cold-source-continuation-v1"
    )
    unstarted_startup: UpgradeExpiredRecoverySupportProcess | None = Field(
        default=None, exclude_if=lambda v: v is None
    )
    previous_recovery_id: UpgradeId
    previous_intent_sha256: Sha256
    previous_owner_sha256: Sha256
    previous_deadline_at: AwareDatetime
    next_recovery_id: UpgradeId
    next_intent_sha256: Sha256
    grant_sha256: Sha256
    original_job_id: UpgradeId
    original_generation: int
    original_maintenance_retained: Literal[True] = True
    original_upgrade_succeeded: Literal[False] = False
    phase: Literal["superseded"] = "superseded"
    superseded_by: UUID
    superseded_at: AwareDatetime

    @model_validator(mode="after")
    def exact_unstarted_custody(self) -> UpgradeColdRecoverySupersession:
        startup = self.unstarted_startup
        if (startup is not None) != (self.protocol == "cold-unstarted-supersession-v1"):
            raise ValueError("cold supersession changes its typed custody protocol")
        if startup is not None and (
            startup.intent.recovery_id != self.previous_recovery_id
            or startup.intent.sha256() != self.previous_intent_sha256
            or startup.intent.deadline_at != self.previous_deadline_at
            or startup.intent.request.original_job_id != self.original_job_id
            or startup.intent.request.original_generation != self.original_generation
        ):
            raise ValueError("unstarted supersession changes exact expired custody")
        return self

    @model_validator(mode="after")
    def retained_failure(self) -> UpgradeColdRecoverySupersession:
        if (
            len({self.previous_recovery_id, self.next_recovery_id, self.original_job_id}) != 3
            or self.original_generation < 1
            or self.superseded_at < self.previous_deadline_at
        ):
            raise ValueError("cold supersession changes scope or expired history")
        return self


class UpgradeColdContinuationSchemaRequest(UpgradeWire):
    previous_recovery_id: UpgradeId
    previous_intent_sha256: Sha256
    previous_owner_sha256: Sha256
    expected_schema_sha256: Sha256
    idempotency_key: Sha256


class UpgradeColdContinuationSchemaObservation(UpgradeWire):
    previous_recovery_id: UpgradeId
    previous_intent_sha256: Sha256
    previous_owner_sha256: Sha256
    original_job_id: UpgradeId
    original_generation: int
    schema_head: Literal["0034_upgrades"] = "0034_upgrades"
    schema_sha256: Sha256
    continuation_state_ready: bool
    observed_at: AwareDatetime


class UpgradeColdContinuationSchemaReceipt(UpgradeWire):
    request_sha256: Sha256
    previous_recovery_id: UpgradeId
    previous_intent_sha256: Sha256
    previous_owner_sha256: Sha256
    original_job_id: UpgradeId
    original_generation: int
    before_schema_sha256: Sha256
    after_schema_sha256: Sha256
    prepared_by: UUID
    prepared_at: AwareDatetime
    continuation_state_ready: Literal[True] = True


class UpgradeColdContinuationContext(UpgradeWire):
    intent: UpgradeExpiredRecoveryIntent
    owner: UpgradeExpiredRecoveryColdClaim
    unstarted_intent: UpgradeExpiredRecoveryIntent | None = Field(
        default=None, exclude_if=lambda v: v is None
    )
    previous_smoke_command: UpgradeExpiredRecoveryColdSmokeCommand | None = Field(
        default=None, exclude_if=lambda v: v is None
    )
    previous_handback_intent: UpgradeExpiredRecoveryColdHandbackIntent | None = Field(
        default=None, exclude_if=lambda v: v is None
    )
    previous_handback: UpgradeExpiredRecoveryColdHandbackReceipt | None = Field(
        default=None, exclude_if=lambda v: v is None
    )
    data_baseline: UpgradeExpiredRecoveryDataSnapshot
    native_baseline: UpgradeExpiredRecoveryNativeDataSnapshot
    current_data: UpgradeExpiredRecoveryDataSnapshot
    observed_at: AwareDatetime

    @model_validator(mode="after")
    def metadata_preserves_original_data(self) -> UpgradeColdContinuationContext:
        intent = self.intent
        plan = intent.original_plan
        before, after = self.data_baseline, self.current_data
        if (
            self.owner.intent != intent
            or (
                self.previous_handback is not None
                and self.previous_handback.helper.handback.verification.request.smoke.smoke.source.launch.source.projection.owner
                != self.owner
            )
            or (
                self.previous_smoke_command is not None
                and self.previous_smoke_command.source.launch.source.projection.owner != self.owner
            )
            or self.owner.supervision != "fixture"
            or self.owner.retired_supervisor is None
            or self.observed_at < intent.deadline_at
            or not intent.deadline_at <= after.observed_at <= self.observed_at
            or (
                before.recovery_id,
                before.original_job_id,
                before.original_generation,
                before.source_sha256,
            )
            != (
                intent.recovery_id,
                plan.operation.job_id,
                plan.operation.generation,
                plan.source.manifest_sha256,
            )
            or (
                after.recovery_id,
                after.original_job_id,
                after.original_generation,
                after.source_sha256,
            )
            != (
                before.recovery_id,
                before.original_job_id,
                before.original_generation,
                before.source_sha256,
            )
            or (after.table_sha256s, after.row_counts) != (before.table_sha256s, before.row_counts)
            or (
                self.native_baseline.original_job_id,
                self.native_baseline.original_generation,
                self.native_baseline.source_sha256,
                self.native_baseline.node,
            )
            != (
                plan.operation.job_id,
                plan.operation.generation,
                plan.source.manifest_sha256,
                plan.operation.node,
            )
        ):
            raise ValueError("cold continuation metadata changes original obligation or data")
        return self


class UpgradeColdContinuationObserverInput(UpgradeWire):
    previous_recovery_id: UpgradeId
    read: UpgradeColdContinuationRead
    observer_release: UpgradeRelease
    observer_runtime: PreparedNativeRuntime


class UpgradeColdContinuationBindingRead(UpgradeWire):
    intent_sha256: Sha256


class UpgradeColdContinuationBindingState(UpgradeWire):
    intent_sha256: Sha256
    grant: UpgradeColdContinuationGrant | None = None
    observed_at: AwareDatetime

    @model_validator(mode="after")
    def exact_grant(self) -> UpgradeColdContinuationBindingState:
        if self.grant is not None and (
            self.grant.intent.sha256() != self.intent_sha256
            or not self.grant.intent.accepted_at <= self.observed_at < self.grant.intent.deadline_at
        ):
            raise ValueError("cold continuation binding changes its exact live intent")
        return self


class UpgradeColdSourceKeeperRetirement(UpgradeWire):
    """Fixed guard-only transfer step under a separately accepted late continuation."""

    protocol: Literal["cold-source-keeper-retirement-v1"] = "cold-source-keeper-retirement-v1"
    grant: UpgradeColdContinuationGrant
    actor: NodeAgentProcessIdentity
    started_at: AwareDatetime
    retired_at: AwareDatetime | None = Field(default=None, exclude_if=lambda v: v is None)

    @model_validator(mode="after")
    def exact_source_keeper_step(self) -> UpgradeColdSourceKeeperRetirement:
        intent = self.grant.intent
        late = self.grant.binding.post_handback
        if (
            late is None
            or self.actor.environment_sha256 != intent.support_release.manifest.environment_sha256
            or self.actor.pid
            in {late.source_helper.process.pid, self.grant.binding.retained_node.process.pid}
            or not intent.accepted_at.timestamp()
            <= self.actor.create_time_s
            <= self.started_at.timestamp()
            or not intent.accepted_at <= self.started_at < intent.deadline_at
            or (
                self.retired_at is not None
                and not self.started_at <= self.retired_at < intent.deadline_at
            )
        ):
            raise ValueError("Source keeper retirement changes captured guard scope or budget")
        return self
