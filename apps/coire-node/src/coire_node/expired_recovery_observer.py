"""Passive immutable-support observer; no guard construction, writes or effects."""

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
from opentelemetry import trace
from pydantic import ValidationError

from coire_core.errors import UpgradeUnavailable
from coire_core.models.upgrade_recovery import (
    UpgradeExpiredRecoveryObservation,
    UpgradeExpiredRecoveryObserverInput,
    UpgradeExpiredRecoveryRequest,
)
from coire_core.models.upgrades import (
    NativeRecoveryIntent,
    NativeUpgradeClaim,
    NodeUpgradeCollectiveScope,
    PreparedNativeRuntime,
    UpgradeExecutionPlan,
    UpgradeRelease,
)
from coire_core.settings import Settings
from coire_core.upgrade_journal import PrivateUpgradeStore
from coire_node.upgrade_runtime import verify_prepared_runtime

tracer = trace.get_tracer("coire.node.expired_recovery_observer")


def observe_retained_expired_recovery(
    settings: Settings,
    request: UpgradeExpiredRecoveryRequest,
    support: UpgradeRelease,
    observer_runtime: PreparedNativeRuntime,
) -> UpgradeExpiredRecoveryObservation:
    """Read exact retained preimages from fixed configured roots.

    This trusted local callable is not an API accepting remote file paths.
    Transport authentication and Core authority rechecks remain mandatory.
    Byte verification is real in production; tests stub it only for fixtures.
    """
    with tracer.start_as_current_span("coire.node.expired_recovery_observe"):
        envs = Path(settings.upgrade_environment_root)
        python = verify_prepared_runtime(envs, envs.parent / "runtimes", observer_runtime)
        environment = envs / observer_runtime.environment_id
        if Path(sys.prefix).resolve(strict=True) != environment.resolve(strict=True) or Path(
            sys.executable
        ).resolve(strict=True) != python.resolve(strict=True):
            raise UpgradeUnavailable("Recovery observer is not running immutable support")
        bootstrap = PrivateUpgradeStore(Path(settings.upgrade_bootstrap_root), create=False)
        operation = PrivateUpgradeStore(bootstrap.path("operations"), create=False)
        operation = PrivateUpgradeStore(operation.path(request.original_job_id), create=False)
        guard = PrivateUpgradeStore(operation.path("guard"), create=False)
        upgrades = PrivateUpgradeStore(Path(settings.node_state_dir) / "upgrades", create=False)
        collections_path = upgrades.path("collectives")
        collections = (
            PrivateUpgradeStore(collections_path, create=False)
            if collections_path.exists() or collections_path.is_symlink()
            else None
        )
        collectives = collections.path(request.original_job_id) if collections is not None else None

        def snapshot() -> UpgradeExpiredRecoveryObservation:
            claim = bootstrap.read("claim.json", NativeUpgradeClaim)
            plan = operation.read("plan.json", UpgradeExecutionPlan)
            native = guard.read("intent.json", NativeRecoveryIntent)
            if claim is None or plan is None or native is None or claim.node != settings.node_name:
                raise UpgradeUnavailable("Retained recovery authority is absent or foreign")
            scopes = []
            if collectives is None or not (collectives.exists() or collectives.is_symlink()):
                if request.original_scope_sha256s:
                    raise UpgradeUnavailable("Declared recovery dispatch history is absent")
                return UpgradeExpiredRecoveryObservation(
                    request=request,
                    plan=plan,
                    native=native,
                    claim=claim,
                    scopes=(),
                    support=support,
                    observer_runtime=observer_runtime,
                    observed_at=datetime.now(UTC),
                )
            job = PrivateUpgradeStore(collectives, create=False)
            for stage_path in sorted(job.root.iterdir()):
                if stage_path.name not in {"candidate", "source"}:
                    raise UpgradeUnavailable("Unknown retained recovery collective stage")
                stage = PrivateUpgradeStore(stage_path, create=False)
                for slot_path in sorted(stage.root.iterdir()):
                    if slot_path.name not in {"0", "1", "2"}:
                        raise UpgradeUnavailable("Unknown retained recovery collective slot")
                    slot = PrivateUpgradeStore(slot_path, create=False)
                    scope = slot.read("scope.json", NodeUpgradeCollectiveScope)
                    if (
                        scope is None
                        or scope.request.stage != stage_path.name
                        or str(scope.index) != slot_path.name
                    ):
                        raise UpgradeUnavailable("Retained recovery dispatch preimage differs")
                    scopes.append(scope)
            return UpgradeExpiredRecoveryObservation(
                request=request,
                plan=plan,
                native=native,
                claim=claim,
                scopes=tuple(scopes),
                support=support,
                observer_runtime=observer_runtime,
                observed_at=datetime.now(UTC),
            )

        try:
            first = snapshot()
            from coire_node.expired_recovery_native_data import capture_native_data

            native_data = capture_native_data(
                settings,
                first.plan,
                deadline_at=datetime.now(UTC) + timedelta(seconds=min(30, request.recovery_s)),
            )
            second = snapshot()
        except (OSError, ValidationError):
            raise UpgradeUnavailable(
                "Retained recovery observation is incomplete or differs"
            ) from None
        if first.model_copy(update={"observed_at": second.observed_at}) != second:
            raise UpgradeUnavailable("Retained recovery authority changed during observation")
        return UpgradeExpiredRecoveryObservation.model_validate(
            second.model_dump(mode="json") | {"native_data": native_data}
        )


def main() -> None:
    """Installed immutable passive producer; no Source edits or guard effects."""
    from coire_node.keychain import NODE_TOKEN_ITEM, read_item
    from coire_node.upgrade_guard import NativeGuardTransport

    settings = Settings()
    if not settings.upgrades_enabled:
        raise UpgradeUnavailable("Managed recovery observation is disabled")
    if not settings.node_token.get_secret_value():
        settings.node_token = read_item(NODE_TOKEN_ITEM)
    data = sys.stdin.buffer.read(512 * 1024 + 1)
    if len(data) > 512 * 1024:
        raise UpgradeUnavailable("Initial observer input exceeds its bound")
    body = UpgradeExpiredRecoveryObserverInput.model_validate_json(data)
    observation = observe_retained_expired_recovery(
        settings, body.request, body.support, body.runtime
    )
    with httpx.Client(trust_env=False, follow_redirects=False, timeout=5.0) as client:
        NativeGuardTransport(settings, client).publish_initial_recovery_observation(observation)


if __name__ == "__main__":
    main()
