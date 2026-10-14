"""Archive only a never-published preparation under audited cancellation authority."""

import hashlib
import logging
import os
import platform
import stat
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import psutil
from opentelemetry import metrics, trace

from coire_core.errors import CoireError, UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrade_unclaimed import (
    UpgradeUnclaimedCancellationGrant,
    UpgradeUnclaimedCancellationState,
    UpgradeUnclaimedPreparationArchive,
)
from coire_core.models.upgrades import (
    NativeNodeProcess,
    NativeRecoveryIntent,
    NativeRuntimeSelection,
    NativeUpgradeClaim,
    NodeUpgradeCancel,
    NodeUpgradeCommand,
    NodeUpgradeStatus,
    PreparedNativeRuntime,
    UpgradeExecutionPlan,
    UpgradeMaintenance,
)
from coire_core.settings import Settings
from coire_core.upgrade_journal import PrivateUpgradeStore
from coire_node.native_platform import is_darwin
from coire_node.upgrade_control import NativeUpgradeControl
from coire_node.upgrade_guard import NativeGuardTransport, _owned_node_process
from coire_node.upgrade_inventory import native_process_entries
from coire_node.upgrade_runtime import verify_prepared_runtime

tracer = trace.get_tracer("coire.node.unclaimed_cancellation")
archives = metrics.get_meter("coire.node.unclaimed_cancellation").create_counter(
    "coire_upgrade_unclaimed_preparation_archive_attempts_total"
)
FILES = frozenset(
    {
        "plan.json",
        "command.json",
        "guard/intent.json",
        "guard/intent.lock",
        "receipts/plan.json",
        "receipts/journal.lock",
    }
)
DIRECTORIES = frozenset({"guard", "receipts"})


def stable_metadata(info: os.stat_result) -> tuple[int, ...]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_uid,
        info.st_gid,
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def same_preparation_plan(
    actual: UpgradeExecutionPlan | None, expected: UpgradeExecutionPlan
) -> bool:
    # Core reissues the transport TTL before delivery. It is excluded from the
    # stable preparation authority, while drain/readiness/recovery epochs remain
    # part of the exact comparison and the original bytes remain archived.
    return actual is not None and actual.model_dump(
        mode="python", exclude={"authorization_expires_at"}
    ) == expected.model_dump(mode="python", exclude={"authorization_expires_at"})


def prepared_bytes(path: Path, plan: UpgradeExecutionPlan) -> dict[str, str]:
    store = PrivateUpgradeStore(path, create=False)
    if not same_preparation_plan(store.read("plan.json", UpgradeExecutionPlan), plan):
        raise UpgradeConflict("Unclaimed preparation plan differs")
    command = store.read("command.json", NodeUpgradeCommand)
    intent = PrivateUpgradeStore(path / "guard", create=False).read(
        "intent.json", NativeRecoveryIntent
    )
    if (
        command is None
        or command.authority_sha256() != plan.authority_sha256()
        or command.activation_grant is not None
        or command.activation_receipt is not None
        or command.peer_inventory is not None
        or intent is None
        or intent.phase != "prepared"
        or intent.job_id != plan.operation.job_id
        or intent.generation != plan.operation.generation
        or intent.plan_sha256 != plan.authority_sha256()
        or intent.activation_grant_id is not None
        or intent.activation_started_at is not None
        or intent.recovery_started_at is not None
        or intent.source.release_sha256 != plan.source.manifest_sha256
        or intent.source.runtime.environment_sha256 != plan.source.manifest.environment_sha256
        or intent.candidate.environment_sha256 != plan.target.manifest.environment_sha256
    ):
        raise UpgradeConflict("Unclaimed preparation has another or potentially effected authority")
    if not same_preparation_plan(
        PrivateUpgradeStore(path / "receipts", create=False).read(
            "plan.json", UpgradeExecutionPlan
        ),
        plan,
    ):
        raise UpgradeConflict("Unclaimed receipt journal plan differs")
    hashes = {}
    for index, entry in enumerate(path.rglob("*")):
        if index >= len(FILES) + len(DIRECTORIES):
            raise UpgradeConflict("Unclaimed preparation exceeds its namespace bound")
        name = entry.relative_to(path).as_posix()
        info = entry.lstat()
        if info.st_uid != os.getuid() or info.st_mode & 0o077 or entry.is_symlink():
            raise UpgradeUnavailable("Unclaimed preparation is not private and owned")
        if stat.S_ISDIR(info.st_mode) and name in DIRECTORIES:
            continue
        if (
            not stat.S_ISREG(info.st_mode)
            or name not in FILES
            or info.st_nlink != 1
            or info.st_size > 512 * 1024
        ):
            raise UpgradeConflict("Unclaimed preparation contains unknown effects")
        descriptor = os.open(entry, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            opened = os.fstat(descriptor)
            if (
                opened.st_dev,
                opened.st_ino,
                opened.st_size,
                opened.st_mtime_ns,
                opened.st_ctime_ns,
            ) != (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns):
                raise UpgradeConflict("Unclaimed preparation changed before read")
            with os.fdopen(descriptor, "rb", closefd=False) as source:
                data = source.read(512 * 1024 + 1)
            if (
                len(data) != info.st_size
                or stable_metadata(os.fstat(descriptor)) != stable_metadata(opened)
                or stable_metadata(entry.lstat()) != stable_metadata(info)
            ):
                raise UpgradeConflict("Unclaimed preparation changed during read")
        finally:
            os.close(descriptor)
        if name.endswith(".lock") and data:
            raise UpgradeConflict("Unclaimed preparation lock contains unknown bytes")
        hashes[name] = hashlib.sha256(data).hexdigest()
    if set(hashes) != FILES:
        raise UpgradeConflict("Unclaimed preparation namespace is incomplete")
    return hashes


class SupportCancellationControl(NativeUpgradeControl):
    """Observe the exact existing Source; support never impersonates its PID."""

    def observed(self) -> NativeRuntimeSelection:
        node = PrivateUpgradeStore(
            Path(self.settings.node_state_dir) / "upgrades", create=False
        ).read("node-process.json", NativeNodeProcess)
        if node is None or node.selected.node != self.node:
            raise UpgradeUnavailable("Unclaimed cancellation has no recorded Source")
        envs = Path(self.settings.upgrade_environment_root)
        python = verify_prepared_runtime(envs, envs.parent / "runtimes", node.selected.runtime)
        child = _owned_node_process(node.process, python)
        if child is None or child.cmdline() != [str(python), "-I", "-B", "-m", "coire_node"]:
            raise UpgradeUnavailable("Unclaimed cancellation Source process differs")
        if (envs / "current").resolve(strict=True) != (
            envs / node.selected.runtime.environment_id
        ).resolve(strict=True):
            raise UpgradeConflict("Unclaimed cancellation Source selection differs")
        return node.selected


@tracer.start_as_current_span("coire.node.unclaimed_cancellation", record_exception=False)
def cancel_unclaimed(
    settings: Settings,
    grant: UpgradeUnclaimedCancellationGrant,
    transport: NativeGuardTransport,
) -> NodeUpgradeStatus:
    plan = grant.plan
    job = plan.operation.job_id
    if settings.node_name != plan.operation.node:
        raise UpgradeConflict("Unclaimed cancellation targets another Studio")
    root = PrivateUpgradeStore(Path(settings.upgrade_bootstrap_root), create=False)
    control = SupportCancellationControl(settings)
    with root.ownership("control.lock"):
        state = transport._read(
            f"/api/v1/internal/upgrades/{job}/unclaimed-cancellation",
            UpgradeUnclaimedCancellationState,
            datetime.now(UTC) + timedelta(seconds=5),
            core=True,
        )
        if (
            state.grant != grant
            or not 0 <= (datetime.now(UTC) - state.observed_at).total_seconds() <= 5
        ):
            raise UpgradeConflict("Unclaimed cancellation has no fresh exact audited authority")
        selected = control.observed()
        node_record = PrivateUpgradeStore(
            Path(settings.node_state_dir) / "upgrades", create=False
        ).read("node-process.json", NativeNodeProcess)
        if (
            selected.release_sha256 != plan.source.manifest_sha256
            or selected.runtime.environment_sha256 != plan.source.manifest.environment_sha256
        ):
            raise UpgradeConflict("Unclaimed cancellation has another Source")
        claim = root.read("claim.json", NativeUpgradeClaim)
        if claim is not None and (claim.state != "released" or claim.job_id == job):
            raise UpgradeConflict("Unclaimed cancellation has a published native owner")
        maintenance = PrivateUpgradeStore(
            Path(settings.node_state_dir) / "upgrades", create=False
        ).read("maintenance.json", UpgradeMaintenance)
        if (
            maintenance is None
            or maintenance.state not in {"fencing", "active"}
            or not maintenance.matches_plan(plan)
        ):
            raise UpgradeConflict("Unclaimed cancellation has another maintenance scope")
        reservations = Path(settings.upgrade_environment_root) / ".upgrade-reservations" / job
        if reservations.exists() or reservations.is_symlink():
            raise UpgradeConflict("Unclaimed cancellation has a physical disk reservation")
        if native_process_entries(
            "unclaimed-preparation",
            (
                job,
                str(
                    Path(settings.upgrade_environment_root)
                    / str(plan.target.manifest.environment_id)
                ),
            ),
        ):
            raise UpgradeConflict("Unclaimed cancellation has a potentially owned native process")
        original = root.root / "operations" / job
        archive_root = PrivateUpgradeStore(root.root / "unclaimed-preparations")
        archive = archive_root.path(job)
        marker_name = "unclaimed-preparation-" + job + ".json"
        marker = root.read(marker_name, UpgradeUnclaimedPreparationArchive)
        if marker is not None and marker.grant != grant:
            raise UpgradeConflict("Unclaimed cancellation archive authority changed")
        if original.exists() or original.is_symlink():
            if archive.exists() or archive.is_symlink():
                raise UpgradeConflict("Unclaimed cancellation has duplicate preparation custody")
            prepared_intent = PrivateUpgradeStore(original / "guard", create=False).read(
                "intent.json", NativeRecoveryIntent
            )
            if (
                node_record is None
                or prepared_intent is None
                or prepared_intent.source != selected
                or prepared_intent.source_process != node_record.process
            ):
                raise UpgradeConflict("Unclaimed cancellation retained Source facts differ")
            before = prepared_bytes(original, plan)
            if marker is None:
                marker = UpgradeUnclaimedPreparationArchive(
                    grant=grant, files=before, prepared_at=datetime.now(UTC)
                )
                root.write(marker_name, marker, replace=False)
            elif marker.files != before:
                raise UpgradeConflict("Unclaimed preparation changed before archival")
            if prepared_bytes(original, plan) != before:
                raise UpgradeConflict("Unclaimed cancellation preparation changed")
            original.rename(archive)
            archive_root.sync()
            PrivateUpgradeStore(root.root / "operations").sync()
            if prepared_bytes(archive, plan) != before:
                raise UpgradeUnavailable("Archived preparation bytes differ")
            archives.add(1, {"outcome": "verified"})
        else:
            if marker is None or prepared_bytes(archive, plan) != marker.files:
                raise UpgradeConflict("Archived preparation has no exact captured bytes")
    # Ordinary producer reacquires the control lock and verifies unchanged Source,
    # exact current fence and positively absent preparation/disk effects.
    return control.cancel_unstarted(
        job, NodeUpgradeCancel(generation=plan.operation.generation, unstarted_plan=plan)
    )


def verify_support_interpreter(settings: Settings) -> None:
    envs = Path(settings.upgrade_environment_root)
    prefix = Path(sys.prefix).resolve(strict=True)
    if (
        prefix.parent != envs.resolve(strict=True)
        or platform.node().lower().split(".", 1)[0] != settings.node_name
    ):
        raise UpgradeUnavailable("Unclaimed cancellation is not installed native support")
    runtime = PrivateUpgradeStore(envs / ".identities", create=False).read(
        prefix.name + ".json", PreparedNativeRuntime
    )
    if runtime is None or verify_prepared_runtime(envs, envs.parent / "runtimes", runtime).resolve(
        strict=True
    ) != Path(sys.executable).resolve(strict=True):
        raise UpgradeUnavailable("Unclaimed cancellation support bytes differ")


def execute(settings: Settings) -> NodeUpgradeStatus:
    if not is_darwin(sys.platform) or platform.machine() != "arm64":
        raise UpgradeUnavailable("Unclaimed cancellation support requires a native Studio")
    body = sys.stdin.buffer.read(512 * 1024 + 1)
    if len(body) > 512 * 1024:
        raise UpgradeUnavailable("Unclaimed cancellation input exceeds its bound")
    grant = UpgradeUnclaimedCancellationGrant.model_validate_json(body)
    verify_support_interpreter(settings)
    with httpx.Client(trust_env=False, follow_redirects=False, timeout=5) as client:
        result = cancel_unclaimed(settings, grant, NativeGuardTransport(settings, client))
    return result


def main() -> None:
    from opentelemetry.sdk.metrics import MeterProvider

    from coire_core.logging import StructuredJSONFormatter
    from coire_node.keychain import NODE_TOKEN_ITEM, read_item
    from coire_node.otel import configure_node_telemetry

    handler = logging.StreamHandler()
    handler.setFormatter(StructuredJSONFormatter())
    logging.basicConfig(level=logging.INFO, handlers=[handler])
    try:
        settings = Settings()
        configure_node_telemetry(
            "unclaimed-cancellation",
            settings.otlp_endpoint,
            diagnostics_enabled=settings.diagnostics_enabled,
        )
        if not settings.node_token.get_secret_value():
            settings.node_token = read_item(NODE_TOKEN_ITEM)
        result = execute(settings)
        logging.getLogger(__name__).info(
            "Unpublished native preparation cancelled",
            extra={"job_id": result.job_id, "node": result.node},
        )
        sys.stdout.write(result.model_dump_json())
    except (CoireError, OSError, ValueError, psutil.Error) as error:
        logging.getLogger(__name__).error(
            "Unclaimed cancellation refused",
            extra={
                "error_type": type(error).__name__,
                "reason": str(error)[:256]
                if isinstance(error, CoireError)
                else "bounded native observation refused",
            },
        )
        archives.add(1, {"outcome": "refused"})
        raise SystemExit(
            "Unclaimed cancellation refused; original obligations remain retained"
        ) from None
    finally:
        provider = metrics.get_meter_provider()
        if isinstance(provider, MeterProvider):
            provider.shutdown(timeout_millis=1000)


if __name__ == "__main__":
    main()
