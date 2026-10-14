"""Fixed immutable-support launcher; uncertain launches never repeat implicitly."""

import logging
import os
import platform
import stat
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx
import psutil
from opentelemetry import metrics, trace
from opentelemetry.sdk.metrics import MeterProvider

from coire_core.errors import CoireError, UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrade_recovery import (
    UpgradeExpiredRecoverySupportLaunchIntent,
    UpgradeExpiredRecoverySupportProcess,
    UpgradeExpiredRecoverySupportStartInput,
)
from coire_core.models.upgrades import NodeAgentProcessIdentity
from coire_core.settings import Settings
from coire_core.upgrade_journal import PrivateUpgradeStore
from coire_node.expired_recovery_support import prepare_support_startup
from coire_node.native_platform import is_darwin
from coire_node.upgrade_guard import NativeGuardTransport, _owned_native_process
from coire_node.upgrade_runtime import verify_prepared_runtime

tracer = trace.get_tracer("coire.node.recovery_startup")
logger = logging.getLogger(__name__)
launches = metrics.get_meter("coire.node.recovery_startup").create_counter(
    "coire_upgrade_recovery_support_launches_total"
)


def launcher_python(settings: Settings, body: UpgradeExpiredRecoverySupportStartInput) -> Path:
    envs = Path(settings.upgrade_environment_root)
    python = verify_prepared_runtime(envs, envs.parent / "runtimes", body.runtime)
    if (
        not is_darwin(sys.platform)
        or platform.machine() != "arm64"
        or platform.node().lower().split(".", 1)[0] != settings.node_name
        or Path(sys.prefix).resolve(strict=True)
        != (envs / body.runtime.environment_id).resolve(strict=True)
        or Path(sys.executable).resolve(strict=True) != python.resolve(strict=True)
        or settings.upgrade_recovery_id is not None
        or settings.upgrade_recovery_succession_id is not None
    ):
        raise UpgradeUnavailable("Recovery launcher is not the fixed installed support runtime")
    return python


@tracer.start_as_current_span("coire.node.recovery_support_launch")
def launch_support(
    settings: Settings,
    body: UpgradeExpiredRecoverySupportStartInput,
    control: NativeGuardTransport,
) -> UpgradeExpiredRecoverySupportProcess:
    python = launcher_python(settings, body)
    startup = prepare_support_startup(settings, body.intent, body.runtime, control)
    bootstrap = PrivateUpgradeStore(Path(settings.upgrade_bootstrap_root), create=False)
    runs = PrivateUpgradeStore(bootstrap.path("expired-recovery"), create=False)
    run = PrivateUpgradeStore(runs.path(body.intent.recovery_id), create=False)
    with run.ownership("support.launch.lock"):
        if control.verify_support_context(body.intent) != body.intent:
            raise UpgradeUnavailable("Recovery launch Core context changed")
        previous = run.read("support.launch.receipt.json", UpgradeExpiredRecoverySupportProcess)
        if previous is not None:
            if previous.intent != body.intent or previous.runtime != body.runtime:
                raise UpgradeConflict("Recovery launch receipt differs from installed scope")
            if _owned_native_process(previous.process, python, "coire_node.upgrade_guard") is None:
                raise UpgradeUnavailable(
                    "Recovery launch actor is absent; explicit succession required"
                )
            return previous
        marker = run.read("support.launch.intent.json", UpgradeExpiredRecoverySupportLaunchIntent)
        if marker is not None:
            if marker.startup != startup:
                raise UpgradeConflict("Recovery launch intent differs")
            raise UpgradeUnavailable("Recovery launch outcome is uncertain; no repeated launch")
        environment = dict(os.environ)
        for key in (
            "PYTHONPATH",
            "PYTHONHOME",
            "DYLD_INSERT_LIBRARIES",
            "LD_PRELOAD",
            "UPGRADE_RECOVERY_SUCCESSION_ID",
            "UPGRADE_RECOVERY_SUCCESSION_MODE",
            "UPGRADE_RECOVERY_SUPERVISION_FD",
        ):
            environment.pop(key, None)
        environment.update(
            NODE_NAME=settings.node_name,
            NODE_STATE_DIR=settings.node_state_dir,
            NODE_STORE_DIR=settings.node_store_dir,
            UPGRADE_BOOTSTRAP_ROOT=settings.upgrade_bootstrap_root,
            UPGRADE_ENVIRONMENT_ROOT=settings.upgrade_environment_root,
            UPGRADE_RECOVERY_ID=body.intent.recovery_id,
            UPGRADES_ENABLED="true",
            PYTHONDONTWRITEBYTECODE="1",
            PYTHONNOUSERSITE="1",
            HF_HUB_OFFLINE="1",
            TRANSFORMERS_OFFLINE="1",
        )
        # Capture the effect boundary before spawn. A lost result grants no
        # permission to repeat Popen, even when a process cannot be observed.
        launch = UpgradeExpiredRecoverySupportLaunchIntent(
            startup=startup,
            launcher=NodeAgentProcessIdentity(
                pid=os.getpid(),
                create_time_s=psutil.Process(os.getpid()).create_time(),
                environment_sha256=body.runtime.environment_sha256,
            ),
            started_at=datetime.now(UTC),
        )
        run.write("support.launch.intent.json", launch, replace=False)
        if control.verify_support_context(body.intent) != body.intent:
            raise UpgradeUnavailable("Recovery launch authority changed before spawn")
        if launcher_python(settings, body) != python:
            raise UpgradeUnavailable("Recovery launch immutable runtime changed")
        descriptor = os.open(
            run.path("support.log"), os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600
        )
        with os.fdopen(descriptor, "ab") as output:
            details = os.fstat(output.fileno())
            if (
                not stat.S_ISREG(details.st_mode)
                or details.st_uid != os.getuid()
                or details.st_nlink != 1
                or details.st_mode & 0o077
                or details.st_size > 1024 * 1024
            ):
                raise UpgradeUnavailable("Recovery support log is not a bounded private file")
            child = subprocess.Popen(
                [str(python), "-I", "-B", "-m", "coire_node.upgrade_guard"],
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
                close_fds=True,
                start_new_session=True,
                env=environment,
            )
        process = NodeAgentProcessIdentity(
            pid=child.pid,
            create_time_s=psutil.Process(child.pid).create_time(),
            environment_sha256=body.runtime.environment_sha256,
        )
        if _owned_native_process(process, python, "coire_node.upgrade_guard") is None:
            raise UpgradeUnavailable("Recovery launch child identity is uncertain")
        receipt = UpgradeExpiredRecoverySupportProcess(
            intent=body.intent, runtime=body.runtime, process=process, observed_at=datetime.now(UTC)
        )
        run.write("support.launch.receipt.json", receipt, replace=False)
        launches.add(1, {"node": settings.node_name, "outcome": "recorded"})
        logger.info(
            "Immutable recovery support process launched",
            extra={
                "job_id": body.intent.request.original_job_id,
                "recovery_id": body.intent.recovery_id,
                "generation": body.intent.request.original_generation,
                "user_id": str(body.intent.accepted_by),
                "node": settings.node_name,
            },
        )
        return receipt


def main() -> None:
    from coire_core.logging import StructuredJSONFormatter
    from coire_node.keychain import NODE_TOKEN_ITEM, read_item
    from coire_node.otel import configure_node_telemetry

    handler = logging.StreamHandler()
    handler.setFormatter(StructuredJSONFormatter())
    logging.basicConfig(level=logging.INFO, handlers=[handler])
    settings: Settings | None = None
    try:
        settings = Settings()
        configure_node_telemetry(
            "recovery-startup",
            settings.otlp_endpoint,
            diagnostics_enabled=settings.diagnostics_enabled,
        )
        if not settings.node_token.get_secret_value():
            settings.node_token = read_item(NODE_TOKEN_ITEM)
        data = sys.stdin.buffer.read(512 * 1024 + 1)
        if len(data) > 512 * 1024:
            raise UpgradeUnavailable("Recovery launch input exceeds its bound")
        body = UpgradeExpiredRecoverySupportStartInput.model_validate_json(data)
        with httpx.Client(trust_env=False, follow_redirects=False, timeout=5.0) as client:
            receipt = launch_support(settings, body, NativeGuardTransport(settings, client))
        print(receipt.model_dump_json())
    except (CoireError, OSError, ValueError, psutil.Error):
        launches.add(
            1,
            {
                "node": settings.node_name
                if settings is not None and settings.node_name in {"coire-edge-a", "coire-edge-b"}
                else "unknown",
                "outcome": "refused",
            },
        )
        logger.error("Immutable recovery support startup refused")
        raise SystemExit("Recovery startup refused; original authority remains retained") from None
    finally:
        provider = metrics.get_meter_provider()
        if isinstance(provider, MeterProvider):
            provider.shutdown(timeout_millis=1000)


if __name__ == "__main__":
    main()
