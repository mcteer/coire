"""Flat retained Source binding for explicitly approved cold crash continuation."""

from __future__ import annotations

from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from coire_core.models.upgrades import (
    ArtifactKey,
    NativeGuardStatus,
    NativeNodeProcess,
    NativeRecoveryIntent,
    NativeUpgradeClaim,
    NodeAgentProcessIdentity,
    Sha256,
    UpgradeId,
    UpgradeMaintenance,
    UpgradeWire,
)


class UpgradeColdPostHandbackBinding(UpgradeWire):
    """Recorded released preimages and positively identified idle Source writer."""

    handback_sha256: Sha256
    stage: Literal["completed", "ready_unreceipted", "keeper_retired"] = Field(
        default="completed", exclude_if=lambda v: v == "completed"
    )
    keeper_custodian_recovery_id: UpgradeId | None = Field(
        default=None, exclude_if=lambda v: v is None
    )
    keeper_retirement_sha256: Sha256 | None = Field(default=None, exclude_if=lambda v: v is None)
    helper_sha256: Sha256 | None = Field(default=None, exclude_if=lambda v: v is None)
    supervision_sha256: Sha256 | None = Field(default=None, exclude_if=lambda v: v is None)
    closure_intent_sha256: Sha256 | None = Field(default=None, exclude_if=lambda v: v is None)
    released_claim: NativeUpgradeClaim
    released_maintenance: UpgradeMaintenance
    source_helper: NativeGuardStatus
    retired_supervisor: NodeAgentProcessIdentity | None = Field(
        default=None, exclude_if=lambda v: v is None
    )

    @model_validator(mode="after")
    def exact_stage(self) -> UpgradeColdPostHandbackBinding:
        completed = (self.supervision_sha256, self.closure_intent_sha256, self.retired_supervisor)
        if self.stage == "completed":
            if (
                any(v is None for v in completed)
                or self.helper_sha256 is not None
                or self.released_maintenance.state != "released"
            ):
                raise ValueError("completed handback requires all retained completion preimages")
        elif (
            self.helper_sha256 is None
            or any(v is not None for v in completed)
            or self.released_maintenance.state == "released"
        ):
            raise ValueError("unreceipted ready handback requires its helper and held maintenance")
        if (self.keeper_retirement_sha256 is not None) != (self.stage == "keeper_retired") or (
            self.keeper_custodian_recovery_id is not None
        ) != (self.stage == "keeper_retired"):
            raise ValueError("keeper retirement stage requires its exact receipt digest")
        return self


class UpgradeColdContinuationBinding(UpgradeWire):
    """Metadata only; an installed producer must independently acquire exclusion.

    Prior owner/source payloads stay in their original journals. Fixed digests and
    native preimages avoid recursively embedding the entire predecessor chain.
    """

    protocol: Literal["cold-source-continuation-v1"] = "cold-source-continuation-v1"
    previous_recovery_id: UpgradeId
    previous_intent_sha256: Sha256
    previous_owner_sha256: Sha256
    previous_source_sha256: Sha256
    previous_deadline_at: AwareDatetime
    previous_kernel_boot_identity: ArtifactKey
    retired_supervisor_sha256: Sha256
    original_claim: NativeUpgradeClaim
    original_native: NativeRecoveryIntent
    projected_native: NativeRecoveryIntent
    previous_guard: NativeGuardStatus
    retained_node: NativeNodeProcess
    core_data_baseline_sha256: Sha256
    native_data_baseline_sha256: Sha256
    previous_archive_sha256: Sha256 | None = Field(default=None, exclude_if=lambda v: v is None)
    previous_history_sha256: Sha256 | None = Field(default=None, exclude_if=lambda v: v is None)
    previous_smoke_sha256: Sha256 | None = Field(default=None, exclude_if=lambda v: v is None)
    previous_pair_result_sha256s: tuple[Sha256, ...] | None = Field(
        default=None, max_length=3, exclude_if=lambda v: v is None
    )
    previous_pair_qualification_sha256: Sha256 | None = Field(
        default=None, exclude_if=lambda v: v is None
    )
    previous_verification_request_sha256: Sha256 | None = Field(
        default=None, exclude_if=lambda v: v is None
    )
    post_handback: UpgradeColdPostHandbackBinding | None = Field(
        default=None, exclude_if=lambda v: v is None
    )
    previous_local_recovery_maintenance: UpgradeMaintenance | None = Field(
        default=None, exclude_if=lambda v: v is None
    )
    untransferred_archive_custodian: UpgradeId | None = Field(
        default=None, exclude_if=lambda v: v is None
    )
    untransferred_archive_sha256: Sha256 | None = Field(
        default=None, exclude_if=lambda v: v is None
    )
    untransferred_history_sha256: Sha256 | None = Field(
        default=None, exclude_if=lambda v: v is None
    )
    previous_owner_retired: Literal[True] = True
    original_maintenance_retained: Literal[True] = True
    observed_at: AwareDatetime

    @model_validator(mode="after")
    def fixed_source_boundary(self) -> UpgradeColdContinuationBinding:
        native = self.original_native
        claim = self.original_claim
        selected = self.retained_node.selected
        guard = self.previous_guard
        late = self.post_handback
        held = self.previous_local_recovery_maintenance
        pending_archive = (
            self.untransferred_archive_custodian,
            self.untransferred_archive_sha256,
            self.untransferred_history_sha256,
        )
        if held is not None and (
            late is not None
            or held.state != "active"
            or held.generation <= claim.generation
            or held.targets != (claim.node,)
            or held.drain_deadline_at > self.observed_at
        ):
            raise ValueError("inherited local fence changes held recovery scope")
        if any(value is not None for value in pending_archive) and (
            held is None
            or any(value is None for value in pending_archive)
            or self.previous_archive_sha256 is not None
            or self.untransferred_archive_custodian in {self.previous_recovery_id, native.job_id}
        ):
            raise ValueError("untransferred archive has another or incomplete custodian")
        if late is not None and (
            late.released_claim.state != "released"
            or late.released_claim.model_dump(exclude={"state", "updated_at"})
            != claim.model_dump(exclude={"state", "updated_at"})
            or (late.stage == "completed" and late.released_maintenance.state != "released")
            or (
                late.stage in {"ready_unreceipted", "keeper_retired"}
                and late.released_maintenance.state == "released"
            )
            or (
                (late.released_maintenance.job_id, late.released_maintenance.generation)
                != (claim.job_id, claim.generation)
                and not (
                    late.released_maintenance.generation > claim.generation
                    and late.released_maintenance.targets == (claim.node,)
                )
            )
            or late.source_helper != guard
            or not guard.ready
            or (
                late.retired_supervisor is not None
                and late.retired_supervisor.pid
                in {guard.process.pid, self.retained_node.process.pid}
            )
        ):
            raise ValueError("post-handback continuation has foreign or incomplete preimages")
        archive_fields = (
            self.previous_archive_sha256,
            self.previous_history_sha256,
            self.previous_pair_result_sha256s,
        )
        if any(value is not None for value in archive_fields) and (
            any(value is None for value in archive_fields)
            or len(set(self.previous_pair_result_sha256s or ()))
            != len(self.previous_pair_result_sha256s or ())
        ):
            raise ValueError("cold continuation archive boundary is incomplete or repeated")
        if self.previous_smoke_sha256 is not None and self.previous_archive_sha256 is None:
            raise ValueError("cold continuation smoke has no preserved archive")
        if self.previous_pair_qualification_sha256 is not None and (
            self.previous_smoke_sha256 is None or len(self.previous_pair_result_sha256s or ()) != 3
        ):
            raise ValueError("cold continuation qualification has no three preserved results")
        if self.previous_verification_request_sha256 is not None and (
            self.previous_pair_qualification_sha256 is None and self.post_handback is None
        ):
            raise ValueError("cold continuation pending verification has no qualification")
        if (
            self.previous_recovery_id == native.job_id
            or (
                claim.state != "active"
                and not (
                    claim.state == "released"
                    and (held is not None or (late is not None and late.released_claim == claim))
                )
            )
            or (claim.job_id, claim.generation, claim.plan_sha256)
            != (native.job_id, native.generation, native.plan_sha256)
            or (selected.job_id, selected.generation, selected.node)
            != (native.job_id, native.generation, claim.node)
            or guard.node != claim.node
            or guard.selected != selected
            or selected.runtime != native.source.runtime
            or selected.release_sha256 != native.source.release_sha256
            or self.retained_node.process.environment_sha256
            != native.source.runtime.environment_sha256
            or native.activation_started_at is None
            or native.recovery_deadline_at is None
            or self.projected_native.phase != "source_selected"
            or self.projected_native
            != native.model_copy(
                update={
                    "phase": "source_selected",
                    "updated_at": self.projected_native.updated_at,
                }
            )
            or not native.recovery_deadline_at
            < self.projected_native.updated_at
            < self.previous_deadline_at
            <= self.observed_at
            or not selected.selected_at
            <= self.retained_node.recorded_at
            < self.previous_deadline_at
            or self.retained_node.process.pid == guard.process.pid
        ):
            raise ValueError("cold continuation has no exact retained Source boundary")
        return self
