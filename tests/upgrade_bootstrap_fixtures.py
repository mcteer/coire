"""Complete typed bootstrap data for CPU-only wire and ownership tests."""

from datetime import UTC, datetime, timedelta

from upgrade_fixtures import core_plan

from coire_core.models.upgrades import (
    BootstrapSourceObservation,
    CoreBootstrapHeartbeat,
    CoreBootstrapPhysicalObservation,
    CoreBootstrapSeed,
    UpgradeRelease,
)


def prepared_bootstrap(
    source: UpgradeRelease | None = None,
) -> tuple[CoreBootstrapSeed, CoreBootstrapPhysicalObservation]:
    source = source or core_plan().source
    now = datetime.now(UTC)
    seed = CoreBootstrapSeed(
        source=source,
        source_container_ids={role.service: "c" * 64 for role in source.manifest.services},
        executor_runtime_id="initial-executor",
        executor_container_id="d" * 64,
        executor_image_id="sha256:" + "e" * 64,
        generation=1,
        prepared_at=now,
    )
    proof = CoreBootstrapPhysicalObservation(
        seed_sha256=seed.sha256(),
        source=BootstrapSourceObservation(
            release_id=source.release_id,
            source_sha256=source.manifest_sha256,
            project="coire",
            container_ids=seed.source_container_ids,
            image_ids={role.service: role.image_id for role in source.manifest.services},
            observed_at=now,
            configuration_verified=True,
            source_healthy=True,
        ),
        executor_runtime_id=seed.executor_runtime_id,
        executor_container_id=seed.executor_container_id,
        executor_image_id=seed.executor_image_id,
        executor_started_at=now - timedelta(seconds=10),
        observed_at=now,
        configuration_verified=True,
    )
    return seed, proof


def bootstrap_heartbeat() -> CoreBootstrapHeartbeat:
    from coire_core.models.upgrades import (
        CoreBootstrapHeartbeat,
        DeploymentLease,
        DeploymentOwnership,
    )

    seed, proof = prepared_bootstrap()
    owner = DeploymentOwnership(
        project="coire",
        owner_kind="executor",
        stage="preparing",
        owner_identity="coire-upgrade-executor",
        executor_runtime_id=seed.executor_runtime_id,
        executor_container_id=seed.executor_container_id,
        boot_identity="isolated-linux-boot",
        generation=1,
        source_sha256=seed.source.manifest_sha256,
    )
    lease = DeploymentLease(
        project="coire",
        owner_identity=owner.owner_identity,
        kernel_platform="linux",
        kernel_boot_identity=owner.boot_identity,
        created_at=proof.observed_at,
    )
    return CoreBootstrapHeartbeat(
        executor_id=owner.owner_identity,
        seed=seed,
        physical=proof,
        ownership=owner,
        lease=lease,
        exclusive_writer_verified=True,
    )
