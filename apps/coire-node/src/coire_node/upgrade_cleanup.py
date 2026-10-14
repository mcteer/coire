"""Positive reconciliation of a dead serving child's private engine lifecycle."""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import psutil
from opentelemetry import trace

from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models.engine import EngineState
from coire_core.models.upgrades import (
    NativeSmokeAttempt,
    NativeSmokeEngineRecord,
    NativeUpgradeClaim,
    UpgradeEvidence,
    UpgradeMaintenance,
    UpgradeOwnerInventory,
)
from coire_core.settings import Settings
from coire_core.upgrade_journal import PrivateUpgradeStore
from coire_node.engines import EngineManager, build_engine_argv
from coire_node.store import Store
from coire_node.upgrade_inventory import native_process_entries, read_owner_json

if TYPE_CHECKING:
    from coire_node.upgrade_guard import NativeGuardJournal

tracer = trace.get_tracer("coire.node.upgrade_cleanup")
SMOKE_STAGES: tuple[Literal["candidate-smoke", "candidate-serving", "source-serving"], ...] = (
    "candidate-smoke",
    "candidate-serving",
    "source-serving",
)


def owned_smoke_engine_alive(
    record: NativeSmokeEngineRecord, executable: Path, python: Path, model: Path
) -> bool:
    """Exact PID birth and fixed bare argv; an unrelated process is never signalled."""
    try:
        process = psutil.Process(record.pid)
        if (
            process.create_time() != record.create_time
            or not process.is_running()
            or process.status() == psutil.STATUS_ZOMBIE
        ):
            return False
        users = process.uids()
        expected = build_engine_argv(
            command=[str(python), "-m", "mlx_lm.server"],
            model_path=str(model),
            host="127.0.0.1",
            port=record.port,
        )
        if (
            users.real != os.getuid()
            or users.effective != os.getuid()
            or Path(process.exe()).resolve(strict=True) != executable.resolve(strict=True)
            or process.cmdline() != expected
            or os.getpgid(record.pid) != record.pid
        ):
            raise UpgradeUnavailable("Owned smoke engine identity is uncertain")
        return True
    except psutil.NoSuchProcess:
        return False
    except (psutil.Error, OSError, ValueError):
        raise UpgradeUnavailable("Owned smoke engine identity is unreadable") from None


@tracer.start_as_current_span(
    "coire.node.upgrade_smoke_cleanup", record_exception=False, set_status_on_exception=False
)
def reconcile_smoke_cleanup(
    settings: Settings,
    journal: NativeGuardJournal,
    stage: Literal["candidate-smoke", "candidate-serving", "source-serving"],
    deadline_at: datetime,
) -> bool:
    """A live helper keeps ownership; only its strict private engine can be adopted to stop."""
    from coire_node.upgrade_guard import _owned_native_process

    plan = journal.plan
    if settings.node_name != plan.operation.node:
        raise UpgradeConflict("Serving cleanup intended Studio differs")
    bootstrap = Path(settings.upgrade_bootstrap_root)
    if not bootstrap.exists():
        raise UpgradeUnavailable("Serving cleanup independent owner is absent")
    root = PrivateUpgradeStore(bootstrap)
    fence_store = PrivateUpgradeStore(Path(settings.node_state_dir) / "upgrades")

    def authority() -> None:
        claim = root.read("claim.json", NativeUpgradeClaim)
        fence = fence_store.read("maintenance.json", UpgradeMaintenance)
        if (
            claim is None
            or claim.state != "active"
            or (claim.job_id, claim.generation, claim.node, claim.plan_sha256)
            != (
                plan.operation.job_id,
                plan.operation.generation,
                plan.operation.node,
                plan.authority_sha256(),
            )
            or fence is None
            or not fence.matches_plan(plan)
        ):
            raise UpgradeUnavailable("Serving cleanup ownership or maintenance changed")

    authority()
    current = journal.current()
    runtime = current.source.runtime if stage == "source-serving" else current.candidate
    key = stage + ".json"
    suffix = {"candidate-smoke": "pre", "candidate-serving": "post", "source-serving": "source"}[
        stage
    ]
    scope = (
        Path(settings.node_state_dir)
        / "upgrades/smokes"
        / f"{plan.operation.job_id}-{plan.operation.generation}-{runtime.environment_sha256}-{suffix}"
    )
    envs = Path(settings.upgrade_environment_root)
    executable = envs.parent / "runtimes" / runtime.interpreter_id / "bin/coire-node-python"
    python = envs / runtime.environment_id / "bin/python3"
    with journal.store.ownership(stage + ".lock"):
        attempt = journal.store.read(key, NativeSmokeAttempt)
        if attempt is None:
            if scope.exists() or scope.is_symlink():
                raise UpgradeUnavailable("Unrecorded serving scope retains ownership")
            return True
        if (
            attempt.job_id,
            attempt.generation,
            attempt.plan_sha256,
            attempt.stage,
            attempt.runtime_sha256,
        ) != (
            plan.operation.job_id,
            plan.operation.generation,
            plan.authority_sha256(),
            stage,
            runtime.environment_sha256,
        ):
            raise UpgradeConflict("Serving cleanup accepted authority differs")
        if attempt.process is not None and (
            _owned_native_process(attempt.process, executable, "coire_node.upgrade_smoke")
            is not None
        ):
            return False
        if native_process_entries("probes", ("coire_node.upgrade_smoke",)):
            return False
        if not scope.exists():
            if attempt.process is not None:
                raise UpgradeUnavailable("Admitted serving scope disappeared")
            # Input is delivered only after the process identity is fsynced.
            journal.store.write(
                key,
                NativeSmokeAttempt.model_validate(
                    attempt.model_dump(mode="json")
                    | {"state": "failed", "cleanup_verified": True, "updated_at": datetime.now(UTC)}
                ),
            )
            return True
        store = PrivateUpgradeStore(scope)
        with store.ownership("smoke.lock"):
            owner = store.read("owner.json", UpgradeOwnerInventory)
            if owner is None or owner.owner != "engines" or owner.status != "complete":
                raise UpgradeUnavailable("Serving engine ownership is unreadable")
            if attempt.cleanup_verified and owner.entries:
                raise UpgradeUnavailable(
                    "Previously cleaned serving scope acquired unknown ownership"
                )
            if attempt.process is None and (
                owner.entries
                or any(path.name not in {"owner.json", "smoke.lock"} for path in scope.iterdir())
            ):
                raise UpgradeUnavailable("Unadmitted serving scope retains unknown ownership")
            engine_root = scope / "engine"
            if owner.entries:
                if len(owner.entries) != 1:
                    raise UpgradeUnavailable("Serving engine ownership is ambiguous")
                entry = owner.entries[0]
                if (
                    entry.owner != "engines"
                    or entry.kind != "upgrade-smoke"
                    or entry.work_id != scope.name
                    or entry.holds != ("native-smoke-memory",)
                ):
                    raise UpgradeUnavailable("Serving engine owner scope differs")
                records = read_owner_json(engine_root / "engines.json")
                if records == []:
                    # The normal owner fsyncs an empty journal after proving
                    # termination. A child can then die before clearing its
                    # outer inventory. Verify its retained birth identity
                    # independently; an empty journal alone is insufficient.
                    if entry.process_id is None or entry.process_created_at is None:
                        raise UpgradeUnavailable("Stopped serving engine identity is absent")
                    try:
                        process = psutil.Process(entry.process_id)
                        if (
                            process.create_time() == entry.process_created_at
                            and process.is_running()
                            and process.status() != psutil.STATUS_ZOMBIE
                        ):
                            raise UpgradeUnavailable("Stopped serving engine remains alive")
                    except psutil.NoSuchProcess:
                        pass
                    except psutil.Error:
                        raise UpgradeUnavailable(
                            "Stopped serving engine identity is unreadable"
                        ) from None
                elif not isinstance(records, list) or len(records) != 1:
                    raise UpgradeUnavailable("Serving engine journal is ambiguous")
                else:
                    record = NativeSmokeEngineRecord.model_validate(records[0])
                    if record.slug != plan.model_slug or (
                        entry.process_id is not None
                        and (entry.process_id, entry.process_created_at)
                        != (record.pid, record.create_time)
                    ):
                        raise UpgradeUnavailable("Serving engine journal differs from its owner")
                    if datetime.now(UTC) >= deadline_at:
                        raise UpgradeUnavailable("Serving cleanup captured deadline elapsed")
                    model_store = Store(settings.node_store_dir)
                    model = model_store.path_for(plan.model_slug)

                    def verify() -> bool:
                        authority()
                        if datetime.now(UTC) >= deadline_at:
                            raise UpgradeUnavailable("Serving cleanup captured deadline elapsed")
                        return owned_smoke_engine_alive(record, executable, python, model)

                    engines = EngineManager(
                        settings.model_copy(update={"node_state_dir": str(engine_root)}),
                        model_store,
                        "127.0.0.1",
                        strict_persistence=True,
                    )
                    engines.adopt_owned_smoke(record, verify)
                    status = engines.stop(record.engine_id, verify_process=verify)
                    remaining = (deadline_at - datetime.now(UTC)).total_seconds()
                    deadline = time.monotonic() + min(15.0, max(0.0, remaining))
                    while status is not None and status.state is not EngineState.STOPPED:
                        if time.monotonic() >= deadline:
                            return False
                        time.sleep(0.05)
                        status = engines.get(record.engine_id)
                    if status is None or verify() or engines.committed_bytes() != 0:
                        raise UpgradeUnavailable("Serving engine stop remains unverified")
            journal_path = engine_root / "engines.json"
            if journal_path.exists() and read_owner_json(journal_path) != []:
                raise UpgradeUnavailable("Serving engine journal retains ownership")
            if native_process_entries(
                "engines", ("mlx_lm.server", "mlx_vlm.server", "fake_engine")
            ):
                return False
            evidence = store.read("result.json", UpgradeEvidence)
            if evidence is not None and (
                attempt.process is None
                or evidence.check != "node"
                or evidence.runtime_sha256 != runtime.environment_sha256
                or evidence.cleanup_verified is not True
                or evidence.generated_tokens is None
                or evidence.generated_tokens < 10
                or evidence.output_sha256 is None
                or (evidence.model_id, evidence.model_variant_id, evidence.model_manifest_sha256)
                != (plan.model_id, plan.model_variant_id, plan.model_manifest_sha256)
            ):
                raise UpgradeUnavailable("Serving cleanup retained result differs")
            if attempt.evidence is not None and attempt.evidence != evidence:
                raise UpgradeUnavailable("Serving cleanup changed a retained completion")
            if attempt.cleanup_verified and attempt.state in {"complete", "failed"}:
                return True
            if datetime.now(UTC) >= deadline_at:
                raise UpgradeUnavailable("Serving cleanup captured deadline elapsed")
            authority()
            if owner.entries:
                store.write("owner.json", UpgradeOwnerInventory(owner="engines", status="complete"))
            completed = NativeSmokeAttempt.model_validate(
                attempt.model_dump(mode="json")
                | {
                    "state": "complete" if evidence is not None else "failed",
                    "evidence": evidence,
                    "cleanup_verified": True,
                    "updated_at": datetime.now(UTC),
                }
            )
            journal.store.write(key, completed)
            return True


def verify_local_cleanup(
    settings: Settings,
    journal: NativeGuardJournal,
    required_stage: Literal["candidate-serving", "source-serving"] | None,
    proof: UpgradeEvidence,
) -> None:
    """Observe physical operation closure independently of a terminal assertion."""
    from coire_node.upgrade_executor import NativeDiskReservation
    from coire_node.upgrade_guard import _owned_native_process

    plan = journal.plan
    envs = Path(settings.upgrade_environment_root)
    reservation = envs / ".upgrade-reservations" / plan.operation.job_id
    if reservation.exists() or reservation.is_symlink():
        ledger = NativeDiskReservation(reservation, plan)
        hold = ledger.current()
        if hold is None or hold.state != "released" or ledger._file(hold) is not None:
            raise UpgradeUnavailable("Native operation staging cleanup is unverified")
    elif required_stage is not None:
        raise UpgradeUnavailable("Native operation staging ownership disappeared")
    current = journal.current()
    for stage in SMOKE_STAGES:
        attempt = journal.store.read(stage + ".json", NativeSmokeAttempt)
        runtime = current.source.runtime if stage == "source-serving" else current.candidate
        suffix = {
            "candidate-smoke": "pre",
            "candidate-serving": "post",
            "source-serving": "source",
        }[stage]
        scope = (
            Path(settings.node_state_dir)
            / "upgrades/smokes"
            / (
                f"{plan.operation.job_id}-{plan.operation.generation}-{runtime.environment_sha256}-{suffix}"
            )
        )
        if attempt is None:
            if stage == required_stage or scope.exists() or scope.is_symlink():
                raise UpgradeUnavailable("Native serving cleanup has no owned attempt")
            continue
        if (
            (
                attempt.job_id,
                attempt.generation,
                attempt.plan_sha256,
                attempt.stage,
                attempt.runtime_sha256,
            )
            != (
                plan.operation.job_id,
                plan.operation.generation,
                plan.authority_sha256(),
                stage,
                runtime.environment_sha256,
            )
            or attempt.state not in {"complete", "failed"}
            or not attempt.cleanup_verified
        ):
            raise UpgradeUnavailable("Native serving attempt retains ownership")
        if attempt.process is not None:
            executable = envs.parent / "runtimes" / runtime.interpreter_id / "bin/coire-node-python"
            if (
                _owned_native_process(attempt.process, executable, "coire_node.upgrade_smoke")
                is not None
            ):
                raise UpgradeUnavailable("Native serving child remains alive")
        if not scope.exists():
            if attempt.process is not None or stage == required_stage:
                raise UpgradeUnavailable("Native admitted serving scope disappeared")
            continue
        store = PrivateUpgradeStore(scope)
        owner = store.read("owner.json", UpgradeOwnerInventory)
        if owner is None or owner.owner != "engines" or owner.status != "complete" or owner.entries:
            raise UpgradeUnavailable("Native serving scope retains ownership")
        engine_journal = scope / "engine/engines.json"
        if engine_journal.exists() and read_owner_json(engine_journal) != []:
            raise UpgradeUnavailable("Native serving engine journal retains ownership")
        if stage == required_stage:
            retained = store.read("result.json", UpgradeEvidence)
            if attempt.state != "complete" or retained is None or retained != attempt.evidence:
                raise UpgradeUnavailable("Native serving completion is not retained")
            for field in (
                "check",
                "runtime_sha256",
                "model_id",
                "model_variant_id",
                "model_manifest_sha256",
                "generated_tokens",
                "output_sha256",
            ):
                if getattr(retained, field) != getattr(proof, field):
                    raise UpgradeUnavailable("Native operation differs from its serving child")
