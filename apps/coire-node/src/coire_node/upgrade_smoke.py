"""One registry-bound serving probe through the normal bare-engine lifecycle.

Execute in the verified retained runtime on a fenced, drained Studio. This helper
owns only its smoke engine; it never unloads another engine to make room.
"""

import hashlib
import logging
import os
import platform
import signal
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO, Literal, TextIO
from uuid import uuid4

import httpx
import psutil
from opentelemetry import metrics, trace

from coire_core.errors import CoireError, UpgradeUnavailable
from coire_core.models.engine import EngineState
from coire_core.models.gateway import TextCompletionResponse
from coire_core.models.upgrades import (
    NativeSmokeCommand,
    NativeSmokeEngineRecord,
    NativeSmokePhase,
    NativeSmokeProgress,
    PreparedNativeRuntime,
    UpgradeEvidence,
    UpgradeExecutionPlan,
    UpgradeInventoryEntry,
    UpgradeMaintenance,
    UpgradeOwnerInventory,
)
from coire_core.settings import Settings
from coire_core.upgrade_journal import PrivateUpgradeStore
from coire_node.engines import EngineManager
from coire_node.store import Store
from coire_node.upgrade_runtime import verify_prepared_runtime

tracer = trace.get_tracer("coire.node.upgrade_smoke")
logger = logging.getLogger(__name__)
smoke_progress = metrics.get_meter("coire.node.upgrade_smoke").create_counter(
    "coire_upgrade_native_smoke_progress_total"
)


def smoke_child(source: BinaryIO, destination: TextIO, settings: Settings) -> None:
    """Read one fixed private authority and return identifiers only, never engine text."""
    try:
        document = source.read(2 * 1024 * 1024 + 1)
        if len(document) > 2 * 1024 * 1024:
            raise ValueError("input exceeds bound")
        command = NativeSmokeCommand.model_validate_json(document)
        remaining = (command.deadline_at - datetime.now(UTC)).total_seconds()
        if not 0 < remaining <= 300:
            raise ValueError("deadline unavailable")
    except (OSError, ValueError):
        raise UpgradeUnavailable("Native smoke child authority cannot be verified") from None
    with httpx.Client(trust_env=False, follow_redirects=False) as client:
        evidence = run_node_smoke(
            settings,
            command.plan,
            command.runtime_sha256,
            command.deadline_at,
            client,
            stage=command.stage,
        )
    result = evidence.model_dump_json()
    if len(result.encode()) > 4096:
        raise UpgradeUnavailable("Native smoke evidence exceeds its bound")
    destination.write(result + "\n")
    destination.flush()


def main() -> None:
    from coire_core.logging import StructuredJSONFormatter
    from coire_node.otel import configure_node_telemetry

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(StructuredJSONFormatter())
    logging.basicConfig(level=logging.INFO, handlers=[handler])
    settings = Settings()
    configure_node_telemetry(
        "upgrade-smoke", settings.otlp_endpoint, diagnostics_enabled=settings.diagnostics_enabled
    )

    def interrupted(_signum: int, _frame: object) -> None:
        raise UpgradeUnavailable("Native smoke child was interrupted")

    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    try:
        smoke_child(sys.stdin.buffer, sys.stdout, settings)
    except (CoireError, OSError, ValueError):
        # The parent gets an exit status; no engine output, credentials or traceback.
        raise SystemExit(1) from None
    finally:
        # Short-lived helpers must export before process exit. Export remains
        # best effort and cannot alter the serving evidence or its exit status.
        for provider in (trace.get_tracer_provider(), metrics.get_meter_provider()):
            flush = getattr(provider, "force_flush", None)
            if flush is not None:
                try:
                    flush(timeout_millis=1000)
                except Exception:
                    logger.warning("native upgrade smoke telemetry flush unavailable")


def run_node_smoke(
    settings: Settings,
    plan: UpgradeExecutionPlan,
    runtime_sha256: str,
    deadline_at: datetime,
    client: httpx.Client,
    *,
    stage: Literal["candidate-smoke", "candidate-serving", "source-serving"] = "candidate-smoke",
) -> UpgradeEvidence:
    if (
        settings.node_name not in {"coire-edge-a", "coire-edge-b"}
        or settings.node_name != plan.operation.node
        or platform.node().lower().split(".", 1)[0] != settings.node_name
        or sys.platform != "darwin"
        or platform.machine() != "arm64"
        or os.environ.get("COIRE_ENGINE_COMMAND")
    ):
        raise UpgradeUnavailable("Upgrade serving smoke requires the intended native Studio")
    scope = {"candidate-smoke": "pre", "candidate-serving": "post", "source-serving": "source"}[
        stage
    ]
    key = f"{plan.operation.job_id}-{plan.operation.generation}-{runtime_sha256}-{scope}"
    root = PrivateUpgradeStore(Path(settings.node_state_dir) / "upgrades/smokes")
    store = PrivateUpgradeStore(root.path(key))
    with store.ownership("smoke.lock"):
        previous = store.read("owner.json", UpgradeOwnerInventory)
        if previous is not None and (previous.owner != "engines" or previous.entries):
            raise UpgradeUnavailable("Previous upgrade smoke needs owned cleanup")
        if previous is None:
            if any(path.name != "smoke.lock" for path in store.root.iterdir()):
                raise UpgradeUnavailable("Unrecorded upgrade smoke needs owned reconciliation")
            # This owner has not yet admitted an engine. Later spawn intent replaces
            # this baseline before acquiring any process or memory obligation.
            store.write(
                "owner.json",
                UpgradeOwnerInventory(owner="engines", status="complete"),
                replace=False,
            )
        try:
            evidence = _run_node_smoke(
                settings, plan, runtime_sha256, deadline_at, client, store, key
            )
            store.write("result.json", evidence)
            return evidence
        except CoireError:
            raise
        except (OSError, ValueError, RuntimeError, httpx.HTTPError):
            raise UpgradeUnavailable("Upgrade serving smoke failed verification") from None


def _run_node_smoke(
    settings: Settings,
    plan: UpgradeExecutionPlan,
    runtime_sha256: str,
    deadline_at: datetime,
    client: httpx.Client,
    smoke_store: PrivateUpgradeStore,
    key: str,
) -> UpgradeEvidence:
    started = datetime.now(UTC)
    failure_phase: NativeSmokePhase | None = None
    failure_code: Literal["engine_readiness", "ownership", "verification"] | None = None

    def progress(phase: NativeSmokePhase) -> None:
        smoke_store.write(
            "progress.json",
            NativeSmokeProgress(
                job_id=plan.operation.job_id,
                generation=plan.operation.generation,
                runtime_sha256=runtime_sha256,
                phase=phase,
                observed_at=datetime.now(UTC),
                failure_phase=failure_phase,
                failure_code=failure_code,
            ),
        )
        smoke_progress.add(1, {"phase": phase})
        logger.info(
            "native upgrade smoke progress retained",
            extra={
                "job_id": plan.operation.job_id,
                "generation": plan.operation.generation,
                "model_id": str(plan.model_id),
                "user_id": str(plan.operation.user_id),
                "phase": phase,
            },
        )

    progress("validating")
    remaining = (deadline_at - started).total_seconds()
    if not 0 < remaining <= 300:
        raise UpgradeUnavailable("Upgrade serving smoke deadline is unavailable")
    deadline = time.monotonic() + remaining
    manifests = [
        release.manifest
        for release in (plan.source, plan.target)
        if release.manifest.environment_sha256 == runtime_sha256
    ]
    if not manifests or len({manifest.environment_id for manifest in manifests}) != 1:
        raise UpgradeUnavailable("Upgrade serving runtime is outside accepted authority")
    environment_id = manifests[0].environment_id
    if environment_id is None:
        raise UpgradeUnavailable("Upgrade serving runtime identity is absent")
    envs = Path(settings.upgrade_environment_root)
    runtime = PrivateUpgradeStore(envs / ".identities").read(
        environment_id + ".json", PreparedNativeRuntime
    )
    if runtime is None or runtime.environment_sha256 != runtime_sha256:
        raise UpgradeUnavailable("Upgrade serving runtime receipt differs")
    python = verify_prepared_runtime(envs, envs.parent / "runtimes", runtime)
    if Path(sys.executable).resolve(strict=True) != python.resolve(strict=True) or Path(
        sys.prefix
    ).resolve(strict=True) != (envs / environment_id).resolve(strict=True):
        raise UpgradeUnavailable("Upgrade serving helper uses a different retained runtime")
    fence_store = PrivateUpgradeStore(Path(settings.node_state_dir) / "upgrades")

    def check_fence() -> None:
        fence = fence_store.read("maintenance.json", UpgradeMaintenance)
        if fence is None or not fence.matches_plan(plan) or settings.node_name not in fence.targets:
            raise UpgradeUnavailable("Upgrade serving maintenance ownership differs")
        if time.monotonic() >= deadline:
            raise UpgradeUnavailable("Upgrade serving smoke deadline elapsed")

    check_fence()
    store = Store(settings.node_store_dir)
    manifest = store.read_manifest(plan.model_slug)
    if (
        manifest is None
        or manifest.slug != plan.model_slug
        or manifest.sha256() != plan.model_manifest_sha256
        or not 0 < manifest.total_bytes <= 1024**3
    ):
        raise UpgradeUnavailable("Upgrade smoke asset differs from verified registry authority")
    base = store.root / plan.model_slug
    if base.is_symlink() or any(path.is_symlink() for path in base.rglob("*")):
        raise UpgradeUnavailable("Upgrade smoke asset contains uncontrolled links")
    if store.verify_against(plan.model_slug, manifest):
        raise UpgradeUnavailable("Upgrade smoke asset content differs")
    check_fence()
    engine_root = PrivateUpgradeStore(smoke_store.root / "engine")
    smoke_settings = settings.model_copy(update={"node_state_dir": str(engine_root.root)})
    engines = EngineManager(
        smoke_settings, store, "127.0.0.1", admission_check=check_fence, strict_persistence=True
    )
    if engines.upgrade_inventory().entries:
        raise UpgradeUnavailable("Upgrade smoke cannot share an unclean engine owner")
    engine_id = uuid4()
    evidence: UpgradeEvidence | None = None
    smoke_store.write(
        "owner.json",
        UpgradeOwnerInventory(
            owner="engines",
            status="complete",
            entries=(
                UpgradeInventoryEntry(
                    owner="engines",
                    kind="upgrade-smoke",
                    work_id=key,
                    state="unknown",
                    holds=("native-smoke-memory",),
                ),
            ),
        ),
    )
    with tracer.start_as_current_span(
        "coire.node.upgrade_smoke", record_exception=False, set_status_on_exception=False
    ):
        try:
            progress("starting")
            existing, status = engines.start(
                engine_id=engine_id,
                slug=plan.model_slug,
                estimate_bytes=max(1024**3, manifest.total_bytes * 2),
            )
            if existing:
                raise UpgradeUnavailable("Upgrade smoke unexpectedly reused another engine")
            smoke_store.write(
                "owner.json",
                UpgradeOwnerInventory(
                    owner="engines",
                    status="complete",
                    entries=(
                        UpgradeInventoryEntry(
                            owner="engines",
                            kind="upgrade-smoke",
                            work_id=key,
                            state="active",
                            holds=("native-smoke-memory",),
                            process_id=status.pid,
                            process_created_at=status.process_create_time,
                        ),
                    ),
                ),
            )
            while status.state is EngineState.STARTING:
                check_fence()
                time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
                status = engines.get(engine_id) or status
            if (
                status.state is not EngineState.READY
                or status.pid is None
                or status.process_create_time is None
            ):
                raise UpgradeUnavailable("Upgrade smoke engine did not become ready")
            check_fence()
            progress("generating")
            with client.stream(
                "POST",
                f"http://127.0.0.1:{status.port}/v1/completions",
                json={
                    "model": str(store.path_for(plan.model_slug)),
                    "prompt": "Count from one to twenty, spelling every number: one, two,",
                    "max_tokens": 64,
                    "temperature": 0,
                    "stream": False,
                },
                timeout=min(30.0, max(0.001, deadline - time.monotonic())),
            ) as response:
                response.raise_for_status()
                document = bytearray()
                for chunk in response.iter_bytes():
                    check_fence()
                    if len(document) + len(chunk) > 64 * 1024:
                        raise UpgradeUnavailable("Upgrade generation exceeds its response bound")
                    document.extend(chunk)
            result = TextCompletionResponse.model_validate_json(document)
            if (
                result.usage is None
                or not 10 <= result.usage.completion_tokens <= 64
                or result.usage.total_tokens
                != result.usage.prompt_tokens + result.usage.completion_tokens
                or result.model != str(store.path_for(plan.model_slug))
                or len(result.choices) != 1
                or not result.choices[0].text
            ):
                raise UpgradeUnavailable("Upgrade generation did not prove ten fresh tokens")
            evidence = UpgradeEvidence(
                check="node",
                runtime_sha256=runtime_sha256,
                model_id=plan.model_id,
                model_variant_id=plan.model_variant_id,
                model_manifest_sha256=plan.model_manifest_sha256,
                generated_tokens=result.usage.completion_tokens,
                output_sha256=hashlib.sha256(result.choices[0].text.encode()).hexdigest(),
                process_id=status.pid,
                process_created_at=status.process_create_time,
                artifacts_verified=True,
            )
            progress("generation_verified")
        except (CoireError, OSError, ValueError, RuntimeError, httpx.HTTPError) as error:
            previous = smoke_store.read("progress.json", NativeSmokeProgress)
            failure_phase = previous.phase if previous is not None else "starting"
            # Retain bounded categories only; arbitrary exception text, HTTP
            # bodies and engine stderr never enter this private diagnostic.
            failure_code = (
                "engine_readiness"
                if isinstance(error, UpgradeUnavailable)
                and error.detail == "Upgrade smoke engine did not become ready"
                else "verification"
            )
            raise
        finally:
            progress("cleaning")
            # Only this operation's freshly allocated ID is stopped, through its owner.
            owned = engines.get(engine_id)
            verify_process = None
            if owned is not None:
                if (
                    owned.engine_id != engine_id
                    or owned.slug != plan.model_slug
                    or owned.target is not None
                    or owned.pid is None
                    or owned.process_create_time is None
                ):
                    raise UpgradeUnavailable("Upgrade smoke engine ownership is uncertain")
                record = NativeSmokeEngineRecord(
                    engine_id=engine_id,
                    slug=plan.model_slug,
                    pid=owned.pid,
                    create_time=owned.process_create_time,
                    port=owned.port,
                    estimate_bytes=owned.estimate_bytes,
                    started_at=owned.started_at,
                )
                from coire_node.upgrade_cleanup import owned_smoke_engine_alive

                def verify_process() -> bool:
                    return owned_smoke_engine_alive(
                        record,
                        python,
                        envs / environment_id / "bin/python3",
                        store.path_for(plan.model_slug),
                    )

            cleanup_status = engines.stop(engine_id, verify_process=verify_process)
            cleanup_deadline = min(deadline, time.monotonic() + 15)
            while (
                cleanup_status is not None
                and cleanup_status.state is not EngineState.STOPPED
                and time.monotonic() < cleanup_deadline
            ):
                time.sleep(0.05)
                cleanup_status = engines.get(engine_id)
            if cleanup_status is not None and cleanup_status.state is not EngineState.STOPPED:
                raise UpgradeUnavailable("Upgrade smoke cleanup remains unverified")
            if cleanup_status is not None and cleanup_status.pid is not None:
                try:
                    process = psutil.Process(cleanup_status.pid)
                    if (
                        process.create_time() == cleanup_status.process_create_time
                        and process.is_running()
                        and process.status() != psutil.STATUS_ZOMBIE
                    ):
                        raise UpgradeUnavailable("Upgrade smoke process remains alive")
                except psutil.NoSuchProcess:
                    pass
                except psutil.Error:
                    raise UpgradeUnavailable(
                        "Upgrade smoke process cleanup is unreadable"
                    ) from None
            if engines.committed_bytes() != 0 or engines.upgrade_inventory().entries:
                raise UpgradeUnavailable("Upgrade smoke ownership has not been cleaned")
            smoke_store.write(
                "owner.json", UpgradeOwnerInventory(owner="engines", status="complete", entries=())
            )
            progress("cleanup_verified")
        check_fence()
        progress("verifying_assets")
        verify_prepared_runtime(envs, envs.parent / "runtimes", runtime)
        final_manifest = store.read_manifest(plan.model_slug)
        if (
            final_manifest is None
            or final_manifest.sha256() != plan.model_manifest_sha256
            or store.verify_against(plan.model_slug, manifest)
        ):
            raise UpgradeUnavailable("Upgrade smoke asset changed during generation")
        assert evidence is not None
        progress("complete")
        return evidence.model_copy(
            update={
                "cleanup_verified": True,
                "elapsed_s": (datetime.now(UTC) - started).total_seconds(),
            }
        )


if __name__ == "__main__":
    main()
