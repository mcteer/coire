"""Independent guard owns a retained serving child before granting it input."""

from __future__ import annotations

import logging
import os
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import psutil
from opentelemetry import metrics, trace

from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrades import (
    NativeSmokeAttempt,
    NativeSmokeCommand,
    NodeAgentProcessIdentity,
    UpgradeEvidence,
    UpgradeOwnerInventory,
)
from coire_core.settings import Settings
from coire_core.upgrade_journal import PrivateUpgradeStore
from coire_node.upgrade_runtime import verify_prepared_runtime

if TYPE_CHECKING:
    from coire_node.upgrade_guard import NativeGuardJournal


logger = logging.getLogger(__name__)
tracer = trace.get_tracer("coire.node.upgrade_child")
child_stages = metrics.get_meter("coire.node.upgrade_child").create_counter(
    "coire_upgrade_smoke_child_stages_total"
)


def record_child(attempt: NativeSmokeAttempt) -> None:
    child_stages.add(1, {"stage": attempt.stage, "state": attempt.state})
    logger.info(
        "native upgrade serving child persisted",
        extra={
            "job_id": attempt.job_id,
            "generation": attempt.generation,
            "stage": attempt.stage,
            "state": attempt.state,
            "process_id": attempt.process.pid if attempt.process is not None else None,
        },
    )


@tracer.start_as_current_span(
    "coire.node.upgrade_smoke_child", record_exception=False, set_status_on_exception=False
)
def run_guard_smoke(
    settings: Settings,
    journal: NativeGuardJournal,
    command: NativeSmokeCommand,
    *,
    authority: Callable[[], None] | None = None,
) -> UpgradeEvidence:
    from coire_node.upgrade_guard import _owned_native_process, exchange_smoke_child
    from coire_node.upgrade_inventory import native_process_entries

    command = NativeSmokeCommand.model_validate(command.model_dump(mode="json"))
    if authority is not None:
        authority()
    plan = journal.plan
    if (
        command.plan.authority_sha256() != plan.authority_sha256()
        or settings.node_name != plan.operation.node
    ):
        raise UpgradeConflict("Native smoke child accepted authority differs")
    intent = journal.current()
    runtime = intent.source.runtime if command.stage == "source-serving" else intent.candidate
    if runtime.environment_sha256 != command.runtime_sha256:
        raise UpgradeConflict("Native smoke child retained runtime differs")
    envs = Path(settings.upgrade_environment_root)
    python = verify_prepared_runtime(envs, envs.parent / "runtimes", runtime)
    key = command.stage + ".json"
    retained = journal.store.read(key, NativeSmokeAttempt)
    if retained is not None and retained.state in {"running", "uncertain"}:
        from coire_node.upgrade_cleanup import reconcile_smoke_cleanup

        if (
            retained.job_id,
            retained.generation,
            retained.plan_sha256,
            retained.stage,
            retained.runtime_sha256,
            retained.deadline_at,
        ) != (
            plan.operation.job_id,
            plan.operation.generation,
            plan.authority_sha256(),
            command.stage,
            command.runtime_sha256,
            command.deadline_at,
        ):
            raise UpgradeConflict("Native smoke child replay authority differs")
        # A lost child response may leave the normal owner's durable stop
        # ahead of its outer inventory. Reconcile without respawning or
        # manufacturing generation evidence, using the original deadline.
        if not reconcile_smoke_cleanup(settings, journal, command.stage, command.deadline_at):
            raise UpgradeUnavailable("Native smoke child cleanup remains owned")
    scope_suffix = {
        "candidate-smoke": "pre",
        "candidate-serving": "post",
        "source-serving": "source",
    }[command.stage]
    scope = (
        Path(settings.node_state_dir)
        / "upgrades/smokes"
        / (
            f"{plan.operation.job_id}-{plan.operation.generation}-{command.runtime_sha256}-{scope_suffix}"
        )
    )

    def result() -> UpgradeEvidence:
        if not scope.exists():
            raise UpgradeUnavailable("Native smoke child evidence is absent")
        stored = PrivateUpgradeStore(scope)
        owner = stored.read("owner.json", UpgradeOwnerInventory)
        evidence = stored.read("result.json", UpgradeEvidence)
        if (
            owner is None
            or owner.owner != "engines"
            or owner.status != "complete"
            or owner.entries
            or evidence is None
            or evidence.cleanup_verified is not True
            or evidence.runtime_sha256 != command.runtime_sha256
            or (evidence.model_id, evidence.model_variant_id, evidence.model_manifest_sha256)
            != (plan.model_id, plan.model_variant_id, plan.model_manifest_sha256)
            or evidence.generated_tokens is None
            or evidence.generated_tokens < 10
            or evidence.output_sha256 is None
            or evidence.check != "node"
        ):
            raise UpgradeUnavailable("Native smoke child serving or cleanup remains unverified")
        return evidence

    with journal.store.ownership(command.stage + ".lock"):
        previous = journal.store.read(key, NativeSmokeAttempt)
        if previous is not None and (
            previous.job_id,
            previous.generation,
            previous.plan_sha256,
            previous.stage,
            previous.runtime_sha256,
            previous.deadline_at,
        ) != (
            plan.operation.job_id,
            plan.operation.generation,
            plan.authority_sha256(),
            command.stage,
            command.runtime_sha256,
            command.deadline_at,
        ):
            raise UpgradeConflict("Native smoke child replay authority differs")
        if previous is not None and previous.process is not None:
            if (
                _owned_native_process(previous.process, python, "coire_node.upgrade_smoke")
                is not None
            ):
                raise UpgradeUnavailable("Native smoke child is still owned")
            evidence = result()
            if previous.evidence is not None and previous.evidence != evidence:
                raise UpgradeUnavailable("Native smoke child retained evidence differs")
            completed = previous.model_copy(
                update={
                    "state": "complete",
                    "evidence": evidence,
                    "cleanup_verified": True,
                    "updated_at": datetime.now(UTC),
                }
            )
            journal.store.write(
                key, NativeSmokeAttempt.model_validate(completed.model_dump(mode="json"))
            )
            record_child(completed)
            return evidence
        if (command.deadline_at - datetime.now(UTC)).total_seconds() <= 0:
            raise UpgradeUnavailable("Native smoke child captured deadline elapsed")
        if native_process_entries("probes", ("coire_node.upgrade_smoke",)):
            raise UpgradeUnavailable("Unrecorded native smoke needs owned reconciliation")
        if scope.exists():
            if previous is None or previous.state != "intent":
                raise UpgradeUnavailable("Unrecorded native smoke needs owned reconciliation")
            retained_scope = PrivateUpgradeStore(scope)
            baseline = retained_scope.read("owner.json", UpgradeOwnerInventory)
            if (
                baseline is None
                or baseline.owner != "engines"
                or baseline.status != "complete"
                or baseline.entries
                or any(path.name not in {"owner.json", "smoke.lock"} for path in scope.iterdir())
            ):
                raise UpgradeUnavailable("Native smoke unadmitted scope is unreconciled")
        attempt = previous or NativeSmokeAttempt(
            job_id=plan.operation.job_id,
            generation=plan.operation.generation,
            plan_sha256=plan.authority_sha256(),
            stage=command.stage,
            runtime_sha256=command.runtime_sha256,
            deadline_at=command.deadline_at,
            updated_at=datetime.now(UTC),
        )
        if previous is None:
            if authority is not None:
                authority()
            journal.store.write(key, attempt, replace=False)
            record_child(attempt)
        # Publish no-admission ownership before creating the child. A restart
        # before input delivery then has positive scope evidence, rather than
        # an ambiguous missing directory after a recorded process identity.
        PrivateUpgradeStore(scope.parent)
        retained_scope = PrivateUpgradeStore(scope)
        if retained_scope.read("owner.json", UpgradeOwnerInventory) is None:
            retained_scope.write(
                "owner.json",
                UpgradeOwnerInventory(owner="engines", status="complete"),
                replace=False,
            )
        payload = command.model_dump_json().encode()
        if len(payload) > 512 * 1024:
            raise UpgradeUnavailable("Native smoke child input exceeds its bound")
        environment = dict(os.environ)
        for name in tuple(environment):
            if name.upper().endswith(("_TOKEN", "_TOKENS", "_KEY", "_SECRET", "_PASSWORD")):
                environment.pop(name)
        for name in (
            "PYTHONPATH",
            "PYTHONHOME",
            "HF_TOKEN",
            "HUGGING_FACE_HUB_TOKEN",
            "WANDB_API_KEY",
            "DATABASE_URL",
            "REDIS_URL",
            "DYLD_INSERT_LIBRARIES",
            "LD_PRELOAD",
            "GOOGLE_APPLICATION_CREDENTIALS",
        ):
            environment.pop(name, None)
        environment.update(
            {
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONNOUSERSITE": "1",
                "HF_HUB_OFFLINE": "1",
                "TRANSFORMERS_OFFLINE": "1",
                "WANDB_MODE": "offline",
                "NODE_NAME": settings.node_name,
                "NODE_STATE_DIR": settings.node_state_dir,
                "NODE_STORE_DIR": settings.node_store_dir,
                "UPGRADE_ENVIRONMENT_ROOT": settings.upgrade_environment_root,
            }
        )
        if authority is not None:
            authority()
        child = subprocess.Popen(
            [str(python), "-I", "-B", "-m", "coire_node.upgrade_smoke"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
            env=environment,
        )
        try:
            process = NodeAgentProcessIdentity(
                pid=child.pid,
                create_time_s=psutil.Process(child.pid).create_time(),
                environment_sha256=command.runtime_sha256,
            )
            attempt = attempt.model_copy(
                update={"state": "running", "process": process, "updated_at": datetime.now(UTC)}
            )
            journal.store.write(key, attempt)
            record_child(attempt)
            # The child waits for EOF before parsing authority. It receives no input
            # until its exact process identity has been fsynced by the independent owner.
            document = (
                exchange_smoke_child(child, payload, command.deadline_at)
                if authority is None
                else exchange_smoke_child(child, payload, command.deadline_at, authority=authority)
            )
            evidence = UpgradeEvidence.model_validate_json(document)
            if (
                _owned_native_process(process, python, "coire_node.upgrade_smoke") is not None
                or result() != evidence
            ):
                raise UpgradeUnavailable("Native smoke child outcome differs from durable cleanup")
            attempt = NativeSmokeAttempt.model_validate(
                attempt.model_dump(mode="json")
                | {
                    "state": "complete",
                    "evidence": evidence,
                    "cleanup_verified": True,
                    "updated_at": datetime.now(UTC),
                }
            )
            journal.store.write(key, attempt)
            record_child(attempt)
            return evidence
        except (OSError, ValueError, psutil.Error, subprocess.SubprocessError):
            raise UpgradeUnavailable("Native smoke child remains unverified") from None
        finally:
            if child.stdin is not None:
                child.stdin.close()
            if child.stdout is not None:
                child.stdout.close()
