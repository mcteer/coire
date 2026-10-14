"""Durable handover intent; runtime verification and sole-writer locking remain external."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
from opentelemetry import trace

from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrades import (
    BootstrapHandover,
    BootstrapStabilityObservation,
    CoreBootstrapPhysicalObservation,
    CoreBootstrapSeed,
    UpgradeReleaseRegister,
)
from coire_core.settings import Settings
from coire_core.upgrade_artifacts import verify_prepared_release
from coire_core.upgrade_journal import PrivateUpgradeStore

tracer = trace.get_tracer("coire.upgrade_executor.bootstrap")


@tracer.start_as_current_span(
    "coire.upgrade_executor.initial_bootstrap_prepare",
    record_exception=False,
    set_status_on_exception=False,
)
async def prepare_initial_bootstrap(
    settings: Settings, seed: CoreBootstrapSeed, client: httpx.AsyncClient
) -> CoreBootstrapPhysicalObservation:
    """Publish exact first-install data only after fresh physical verification.

    This read-only preparation cannot hold deployment ownership or advertise ready.
    A conflicting or interrupted preparation never becomes an accepted operation.
    """
    from .source_observer import InitialBootstrapObserver

    seed = CoreBootstrapSeed.model_validate(seed.model_dump(mode="json"))
    if seed.source.manifest.project != settings.upgrade_project:
        raise UpgradeConflict("Initial bootstrap Source project differs")
    prepared = Path(settings.upgrade_prepared_dir)
    verified = verify_prepared_release(
        prepared,
        UpgradeReleaseRegister(
            prepared_artifact_id=seed.source.prepared_artifact_id,
            expected_sha256=seed.source.manifest_sha256,
        ),
    )
    if verified != seed.source:
        raise UpgradeConflict("Initial bootstrap Source catalog differs")
    if seed.executor_source != seed.source:
        executor_release = verify_prepared_release(
            prepared,
            UpgradeReleaseRegister(
                prepared_artifact_id=seed.executor_source.prepared_artifact_id,
                expected_sha256=seed.executor_source.manifest_sha256,
            ),
        )
        if executor_release != seed.executor_source:
            raise UpgradeConflict("Initial bootstrap executor provenance differs")
    store = PrivateUpgradeStore(Path(settings.upgrade_state_dir) / "bootstrap")
    with store.ownership("seed.lock"):
        previous = store.read("initial-seed.json", CoreBootstrapSeed)
        if previous is not None and previous != seed:
            raise UpgradeConflict("Initial bootstrap seed differs from retained preparation")
        observation = await InitialBootstrapObserver(settings, prepared, seed, client).observe()
        if previous is None:
            store.write("initial-seed.json", seed, replace=False)
        return observation


class BootstrapJournal:
    """The incumbent owns transitions under the shared deployment mutex.

    This journal cannot advertise readiness or start/stop containers. Its caller
    must inspect both exact images and retain the incumbent before each advance.
    """

    def __init__(self, root: Path) -> None:
        self.store = PrivateUpgradeStore(root)

    def current(self) -> BootstrapHandover | None:
        return self.store.read("bootstrap-handover.json", BootstrapHandover)

    def begin(self, value: BootstrapHandover, *, now: datetime | None = None) -> BootstrapHandover:
        with self.store.ownership("handover.lock"):
            return self._begin(value, now=now)

    def _begin(self, value: BootstrapHandover, *, now: datetime | None = None) -> BootstrapHandover:
        clock = now or datetime.now(UTC)
        if (
            value.phase != "passive"
            or value.stability_started_at is not None
            or value.updated_at != clock
            or value.deadline_at != clock + timedelta(seconds=120)
        ):
            raise UpgradeConflict(
                "Executor handover requires a captured passive verification window"
            )
        previous = self.current()
        if previous is not None:
            identity = (
                "incumbent_id",
                "candidate_id",
                "incumbent_container_id",
                "candidate_container_id",
                "incumbent_image_id",
                "candidate_image_id",
                "generation",
            )
            if all(getattr(previous, key) == getattr(value, key) for key in identity):
                return previous
            if (
                previous.phase not in {"committed", "restored"}
                or value.generation <= previous.generation
            ):
                raise UpgradeConflict("Prior executor handover retains recovery ownership")
            selected = (
                (
                    previous.candidate_id,
                    previous.candidate_image_id,
                    previous.candidate_container_id,
                )
                if previous.phase == "committed"
                else (
                    previous.incumbent_id,
                    previous.incumbent_image_id,
                    previous.incumbent_container_id,
                )
            )
            if (
                value.incumbent_id,
                value.incumbent_image_id,
                value.incumbent_container_id,
            ) != selected:
                raise UpgradeConflict("New executor handover differs from committed incumbent")
        self.store.write("bootstrap-handover.json", value)
        return value

    def advance(
        self,
        expected: BootstrapHandover,
        phase: str,
        *,
        now: datetime | None = None,
    ) -> BootstrapHandover:
        with self.store.ownership("handover.lock"):
            return self._advance(expected, phase, now=now)

    def _advance(
        self,
        expected: BootstrapHandover,
        phase: str,
        *,
        now: datetime | None = None,
    ) -> BootstrapHandover:
        clock = now or datetime.now(UTC)
        current = self.current()
        if current != expected:
            raise UpgradeConflict("Executor handover changed before transition")
        if phase == current.phase:
            return current
        allowed = {
            "passive": {"verified", "restoring"},
            "verified": {"claimed", "restoring"},
            "claimed": {"stable", "restoring"},
            "stable": {"committed", "restoring"},
            "restoring": {"restored"},
        }
        if phase not in allowed.get(current.phase, set()) or clock < current.updated_at:
            raise UpgradeConflict("Executor handover transition or observation time differs")
        if (
            phase not in {"restoring", "restored", "stable", "committed"}
            and clock >= current.deadline_at
        ):
            raise UpgradeUnavailable("Executor handover verification deadline expired")
        if phase == "stable" and clock < current.deadline_at:
            raise UpgradeConflict("Executor candidate has not completed its stability interval")
        if phase in {"stable", "committed"}:
            observation = self.store.read(
                "stability-observation.json", BootstrapStabilityObservation
            )
            if (
                observation is None
                or observation.generation != current.generation
                or not 0 <= (clock - observation.observed_at).total_seconds() <= 10
            ):
                raise UpgradeUnavailable("Executor stability has no fresh continuous observation")
        updates: dict[str, object] = {"phase": phase, "updated_at": clock}
        if phase == "claimed":
            updates |= {
                "stability_started_at": clock,
                "deadline_at": clock + timedelta(seconds=120),
            }
        result = BootstrapHandover.model_validate(current.model_dump() | updates)
        self.store.write("bootstrap-handover.json", result)
        return result

    def observe_stability(
        self, value: BootstrapStabilityObservation, *, now: datetime | None = None
    ) -> BootstrapHandover:
        """A missed interval or a changed process restores; it never renews stability.

        The independent incumbent supplies actual original Docker read times and
        a held sole-writer witness. This receipt cannot produce either witness.
        """
        clock = now or datetime.now(UTC)
        with self.store.ownership("handover.lock"):
            current = self.current()
            if (
                current is None
                or current.phase not in {"claimed", "stable"}
                or current.stability_started_at is None
            ):
                raise UpgradeConflict("Executor stability has no claimed handover")
            if (
                value.generation,
                value.incumbent_id,
                value.candidate_id,
                value.incumbent_image_id,
                value.candidate_image_id,
            ) != (
                current.generation,
                current.incumbent_id,
                current.candidate_id,
                current.incumbent_image_id,
                current.candidate_image_id,
            ) or not 0 <= (clock - value.observed_at).total_seconds() <= 5:
                raise UpgradeConflict(
                    "Executor stability observation authority or freshness differs"
                )
            previous = self.store.read("stability-observation.json", BootstrapStabilityObservation)
            previous = (
                previous
                if previous is not None and previous.generation == current.generation
                else None
            )
            baseline = previous.observed_at if previous else current.stability_started_at
            gap = (value.observed_at - baseline).total_seconds()
            identity = (
                "incumbent_container_id",
                "candidate_container_id",
                "kernel_boot_identity",
                "candidate_started_at",
            )
            if (
                not 0 <= gap <= 10
                or value.incumbent_container_id != current.incumbent_container_id
                or value.candidate_container_id != current.candidate_container_id
                or value.candidate_started_at > current.stability_started_at
                or (
                    previous is not None
                    and any(getattr(previous, key) != getattr(value, key) for key in identity)
                )
            ):
                return self._advance(current, "restoring", now=clock)
            if previous is not None and value.observed_at == previous.observed_at:
                if previous != value:
                    raise UpgradeConflict("Executor repeated physical observation differs")
                return current
            self.store.write("stability-observation.json", value)
            return current
