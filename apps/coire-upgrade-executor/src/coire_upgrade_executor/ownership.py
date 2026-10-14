"""Kernel-backed host/executor exclusion, bound to one accepted operation."""

import fcntl
import os
import stat
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from coire_core.deploy import deployment_mutex, host_boot_identity
from coire_core.errors import UpgradeConflict, UpgradeForbidden, UpgradeUnavailable
from coire_core.models.upgrades import (
    CoreBootstrapHeartbeat,
    CoreBootstrapPhysicalObservation,
    CoreBootstrapSeed,
    DeploymentHandoverHold,
    DeploymentLease,
    DeploymentOperationHold,
    DeploymentOwnership,
    UpgradeExecutionPlan,
)
from coire_core.upgrade_journal import PrivateUpgradeStore
from coire_upgrade_executor.bootstrap_admission import verify_bootstrap_admission


def verify_held_deployment(
    store: PrivateUpgradeStore, descriptor: int, claim: DeploymentOwnership
) -> None:
    """A durable row alone cannot substitute for the held kernel and namespace lease."""
    try:
        held = os.fstat(descriptor)
        path = store.path("deployment.lock")
        expected = path.lstat()
        if (
            not stat.S_ISREG(expected.st_mode)
            or expected.st_uid != os.getuid()
            or expected.st_mode & 0o077
            or expected.st_nlink != 1
            or (held.st_dev, held.st_ino) != (expected.st_dev, expected.st_ino)
        ):
            raise UpgradeUnavailable("Executor kernel ownership descriptor differs")
        probe = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
        try:
            try:
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                pass
            else:
                raise UpgradeUnavailable("Executor kernel ownership is not held")
        finally:
            os.close(probe)
    except OSError:
        raise UpgradeUnavailable("Executor kernel ownership is unavailable") from None
    current = store.read("ownership.json", DeploymentOwnership)
    lease = store.read("deployment.lease", DeploymentLease)
    if (
        current != claim
        or lease is None
        or (
            lease.project,
            lease.owner_identity,
            lease.kernel_platform,
            lease.kernel_boot_identity,
        )
        != (claim.project, claim.owner_identity, sys.platform, host_boot_identity())
    ):
        raise UpgradeConflict("Executor durable ownership or kernel epoch changed")


def verify_bootstrap_physical(
    seed: CoreBootstrapSeed, observation: CoreBootstrapPhysicalObservation
) -> None:
    """Require original physical reads before a first-install writer is acquired."""
    if (
        observation.seed_sha256 != seed.sha256()
        or observation.source.release_id != seed.source.release_id
        or observation.source.source_sha256 != seed.source.manifest_sha256
        or observation.source.project != seed.source.manifest.project
        or observation.source.container_ids != seed.source_container_ids
        or observation.source.image_ids
        != {role.service: role.image_id for role in seed.source.manifest.services}
        or (
            observation.executor_runtime_id,
            observation.executor_container_id,
            observation.executor_image_id,
        )
        != (seed.executor_runtime_id, seed.executor_container_id, seed.executor_image_id)
        or not 0 <= (datetime.now(UTC) - observation.observed_at).total_seconds() <= 5
        or not 0 <= (datetime.now(UTC) - observation.source.observed_at).total_seconds() <= 5
    ):
        raise UpgradeUnavailable("Initial bootstrap physical Source or executor proof differs")


class BootstrapAuthority:
    """Held initial recovery authority; it carries no accepted application plan."""

    def __init__(
        self, store: PrivateUpgradeStore, descriptor: int, claim: DeploymentOwnership
    ) -> None:
        self.store, self.descriptor, self.claim = store, descriptor, claim

    def verify(self) -> None:
        verify_held_deployment(self.store, self.descriptor, self.claim)

    def heartbeat(
        self, seed: CoreBootstrapSeed, observation: CoreBootstrapPhysicalObservation
    ) -> CoreBootstrapHeartbeat:
        """Only the currently held owner can produce a fresh positive heartbeat."""
        verify_bootstrap_physical(seed, observation)
        self.verify()
        lease = self.store.read("deployment.lease", DeploymentLease)
        if lease is None:
            raise UpgradeUnavailable("Initial bootstrap writer lease disappeared")
        value = CoreBootstrapHeartbeat(
            executor_id=self.claim.owner_identity,
            seed=seed,
            physical=observation,
            ownership=self.claim,
            lease=lease,
            exclusive_writer_verified=True,
        )
        self.verify()
        return value


@contextmanager
def bootstrap_ownership(
    root: Path,
    seed: CoreBootstrapSeed,
    observation: CoreBootstrapPhysicalObservation,
    executor_id: str,
    *,
    reconciled_prior: DeploymentOwnership | None = None,
    admission_hold: DeploymentOperationHold | None = None,
    handover_admission: DeploymentHandoverHold | None = None,
    bootstrap_root: Path | None = None,
) -> Iterator[BootstrapAuthority]:
    """Hold independent initial ownership without inventing an application job.

    Only an unchanged, physically verified bootstrap owner may be reconciled.
    Active application operations and foreign-kernel claims remain protected.
    Normal exit commits this effect-free owner; interrupted entry retains it.
    """
    verify_bootstrap_physical(seed, observation)
    project = seed.source.manifest.project
    if project is None:
        raise UpgradeUnavailable("Initial bootstrap Source project is absent")
    store = PrivateUpgradeStore(root)
    previous = store.read("ownership.json", DeploymentOwnership)
    verify_bootstrap_admission(
        store,
        project,
        executor_id,
        seed,
        previous,
        admission_hold,
        handover_admission,
        bootstrap_root,
    )
    if previous is not None and (
        previous.project != project
        or (previous.state == "active" and previous != reconciled_prior)
        or (
            previous.owner_kind == "executor"
            and (previous.executor_runtime_id, previous.executor_container_id)
            != (seed.executor_runtime_id, seed.executor_container_id)
        )
    ):
        raise UpgradeConflict("Initial bootstrap cannot replace retained recovery ownership")
    if reconciled_prior is not None and (
        previous != reconciled_prior
        or previous is None
        or previous.owner_kind != "executor"
        or previous.job_id is not None
        or previous.stage != "preparing"
        or (
            previous.owner_identity,
            previous.project,
            previous.executor_runtime_id,
            previous.executor_container_id,
            previous.source_sha256,
        )
        != (
            executor_id,
            seed.source.manifest.project,
            seed.executor_runtime_id,
            seed.executor_container_id,
            seed.source.manifest_sha256,
        )
    ):
        raise UpgradeConflict("Initial bootstrap cannot reconcile another recovery owner")
    with deployment_mutex(
        store,
        project,
        executor_id,
        reconciled_prior=reconciled_prior,
        operation=(admission_hold.job_id, admission_hold.generation) if admission_hold else None,
        handover=(handover_admission.preparation_sha256, handover_admission.generation)
        if handover_admission
        else None,
    ) as descriptor:
        verify_bootstrap_admission(
            store,
            project,
            executor_id,
            seed,
            previous,
            admission_hold,
            handover_admission,
            bootstrap_root,
        )
        if store.read("ownership.json", DeploymentOwnership) != previous:
            raise UpgradeConflict("Initial bootstrap prior ownership changed")
        verify_bootstrap_physical(seed, observation)
        claim = DeploymentOwnership(
            project=project,
            owner_kind="executor",
            stage="preparing",
            owner_identity=executor_id,
            executor_runtime_id=seed.executor_runtime_id,
            executor_container_id=seed.executor_container_id,
            boot_identity=host_boot_identity(),
            generation=max(seed.generation, previous.generation + 1 if previous else 1),
            source_sha256=seed.source.manifest_sha256,
        )
        store.write("ownership.json", claim)
        authority = BootstrapAuthority(store, descriptor, claim)
        authority.verify()
        yield authority
        authority.verify()
        if store.read("ownership.json", DeploymentOwnership) != claim:
            raise UpgradeConflict("Initial bootstrap ownership changed before release")
        store.write("ownership.json", claim.model_copy(update={"state": "committed"}))


class ExecutorAuthority:
    def __init__(
        self,
        store: PrivateUpgradeStore,
        descriptor: int,
        claim: DeploymentOwnership,
        plan: UpgradeExecutionPlan,
    ) -> None:
        self.store, self.descriptor, self.claim, self.plan = store, descriptor, claim, plan
        self.operation_hold = store.read("operation-hold.json", DeploymentOperationHold)
        if (
            claim.owner_kind,
            claim.project,
            claim.job_id,
            claim.operation_generation,
            claim.source_sha256,
            claim.state,
        ) != (
            "executor",
            plan.source.manifest.project,
            plan.operation.job_id,
            plan.operation.generation,
            plan.operation.source_sha256,
            "active",
        ):
            raise UpgradeForbidden("Executor host ownership differs from the accepted operation")
        self.verify()

    def verify(self) -> None:
        """Recheck the held kernel descriptor and exact durable epoch before every effect."""
        verify_held_deployment(self.store, self.descriptor, self.claim)
        hold = self.store.read("operation-hold.json", DeploymentOperationHold)
        if hold != self.operation_hold or (
            hold is not None
            and (
                hold.released
                or (
                    hold.project,
                    hold.job_id,
                    hold.generation,
                    hold.plan_sha256,
                    hold.source_sha256,
                )
                != (
                    self.claim.project,
                    self.plan.operation.job_id,
                    self.plan.operation.generation,
                    self.plan.authority_sha256(),
                    self.plan.operation.source_sha256,
                )
            )
        ):
            raise UpgradeConflict("Executor cluster maintenance ownership changed")


@contextmanager
def executor_ownership(
    root: Path,
    claim: DeploymentOwnership,
    plan: UpgradeExecutionPlan,
    *,
    reconciled_prior: DeploymentOwnership | None = None,
) -> Iterator[ExecutorAuthority]:
    """Closing a process is not a completion receipt; active intent stays retained."""
    if claim.owner_kind != "executor" or claim.boot_identity != host_boot_identity():
        raise UpgradeForbidden("Executor owner kind or boot identity differs")
    store = PrivateUpgradeStore(root)
    with deployment_mutex(
        store,
        claim.project,
        claim.owner_identity,
        reconciled_prior=reconciled_prior,
        operation=(plan.operation.job_id, plan.operation.generation),
    ) as descriptor:
        previous = store.read("ownership.json", DeploymentOwnership)
        if previous is not None and (
            previous.project != claim.project
            or claim.generation <= previous.generation
            or (previous.state == "active" and previous != reconciled_prior)
        ):
            raise UpgradeConflict("Prior executor ownership needs exact runtime reconciliation")
        if previous is None and reconciled_prior is not None:
            raise UpgradeConflict("Prior executor ownership witness is absent")
        if (
            previous is not None
            and previous.executor_runtime_id is not None
            and (
                claim.executor_runtime_id,
                claim.executor_container_id,
            )
            != (previous.executor_runtime_id, previous.executor_container_id)
        ):
            raise UpgradeConflict(
                "Operation ownership cannot change the bootstrapped executor incarnation"
            )
        # Validate authority before publishing the new durable owner.
        if (
            claim.job_id,
            claim.operation_generation,
            claim.source_sha256,
            claim.project,
            claim.state,
        ) != (
            plan.operation.job_id,
            plan.operation.generation,
            plan.operation.source_sha256,
            plan.source.manifest.project,
            "active",
        ):
            raise UpgradeForbidden("Executor ownership does not bind the accepted plan")
        store.write("ownership.json", claim)
        authority = ExecutorAuthority(store, descriptor, claim, plan)
        yield authority
