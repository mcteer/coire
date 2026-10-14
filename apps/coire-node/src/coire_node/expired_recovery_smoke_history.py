"""Preserve positively completed smoke evidence for sealed legacy Source readers."""

import os
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path

from opentelemetry import trace

from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrade_recovery import (
    UpgradeExpiredRecoveryColdSourceReceipt,
    UpgradeRecoverySmokeHistoryArchive,
    UpgradeRecoverySmokeHistoryEntry,
)
from coire_core.models.upgrades import NativeSmokeProgress, UpgradeEvidence, UpgradeOwnerInventory
from coire_core.settings import Settings
from coire_core.upgrade_journal import PrivateUpgradeStore
from coire_node.expired_recovery_cold_archive import history_sha256
from coire_node.expired_recovery_cold_source import stores, verify_projection_boundary
from coire_node.upgrade_guard import NativeGuardTransport
from coire_node.upgrade_inventory import native_process_entries, read_owner_json

tracer = trace.get_tracer("coire.node.cold_recovery")


@tracer.start_as_current_span("coire.node.cold_recovery_archive_smoke_history")
def archive_completed_smokes(
    settings: Settings,
    selection: UpgradeExpiredRecoveryColdSourceReceipt,
    control: NativeGuardTransport,
) -> UpgradeRecoverySmokeHistoryArchive | None:
    effect = selection.projection
    _, run, _ = stores(settings, effect.owner)
    node = PrivateUpgradeStore(Path(settings.node_state_dir) / "upgrades", create=False)
    root = node.path("smokes")
    if not root.exists() and not root.is_symlink():
        return None
    active = PrivateUpgradeStore(root, create=False)
    with (
        run.ownership("cold.smoke.history.lock"),
        node.ownership("maintenance.lock"),
        ExitStack() as locks,
    ):
        verify_projection_boundary(settings, effect, control)
        if native_process_entries("engines", ("mlx_lm.server", "mlx_vlm.server", "fake_engine")):
            raise UpgradeUnavailable("Legacy smoke archive has actual engine ownership")
        proof = run.read("cold.smoke.history.intent.json", UpgradeRecoverySmokeHistoryArchive)
        paths = sorted(active.root.iterdir())
        if len(paths) > 4096:
            raise UpgradeUnavailable("Legacy smoke history exceeds bounded ownership")
        archive = PrivateUpgradeStore(run.path("completed-smoke-history"))
        locked: set[str] = set()
        if proof is None:
            if len(paths) <= 60:
                return None
            entries = []
            for path in paths:
                scope = PrivateUpgradeStore(path, create=False)
                locks.enter_context(scope.ownership("smoke.lock"))
                locked.add(path.name)
                owner = scope.read("owner.json", UpgradeOwnerInventory)
                progress = scope.read("progress.json", NativeSmokeProgress)
                result = scope.read("result.json", UpgradeEvidence)
                if owner != UpgradeOwnerInventory(owner="engines", status="complete"):
                    raise UpgradeUnavailable("Legacy smoke history ownership is incomplete")
                if (
                    progress is None
                    or result is None
                    or progress.phase != "complete"
                    or result.cleanup_verified is not True
                ):
                    continue
                if progress.job_id == effect.owner.intent.request.original_job_id:
                    continue
                if read_owner_json(scope.root / "engine/engines.json") != []:
                    raise UpgradeUnavailable("Legacy smoke history retains an engine journal")
                if (
                    path.name
                    != f"{progress.job_id}-{progress.generation}-{progress.runtime_sha256}-"
                    + path.name.rsplit("-", 1)[-1]
                    or path.name.rsplit("-", 1)[-1] not in {"pre", "post", "source"}
                ):
                    raise UpgradeConflict("Legacy smoke history path differs from evidence")
                if result.runtime_sha256 != progress.runtime_sha256:
                    raise UpgradeConflict("Legacy smoke history runtime differs")
                entries.append(
                    UpgradeRecoverySmokeHistoryEntry(
                        namespace=path.name,
                        job_id=progress.job_id,
                        generation=progress.generation,
                        runtime_sha256=progress.runtime_sha256,
                        history_sha256=history_sha256(scope),
                    )
                )
                if len(paths) - len(entries) <= 60:
                    break
                if len(entries) == 64:
                    raise UpgradeUnavailable("Legacy smoke archive exceeds bounded transition")
            if len(paths) - len(entries) > 60:
                raise UpgradeUnavailable(
                    "Legacy Source requires more positively completed smoke evidence"
                )
            proof = UpgradeRecoverySmokeHistoryArchive(
                owner_sha256=effect.owner.sha256(),
                entries=tuple(entries),
                captured_at=datetime.now(UTC),
            )
            verify_projection_boundary(settings, effect, control)
            run.write("cold.smoke.history.intent.json", proof, replace=False)
        if proof.owner_sha256 != effect.owner.sha256():
            raise UpgradeConflict("Legacy smoke archive owner differs")
        for entry in proof.entries:
            old, new = active.path(entry.namespace), archive.path(entry.namespace)
            if old.exists() == new.exists() or old.is_symlink() or new.is_symlink():
                raise UpgradeConflict("Legacy smoke evidence does not have one namespace owner")
            scope = PrivateUpgradeStore(old if old.exists() else new, create=False)
            if entry.namespace not in locked:
                locks.enter_context(scope.ownership("smoke.lock"))
            if history_sha256(scope) != entry.history_sha256:
                raise UpgradeConflict("Legacy smoke archived evidence bytes differ")
            if old.exists():
                verify_projection_boundary(settings, effect, control)
                os.rename(old, new)
                for parent in (active.root, archive.root):
                    descriptor = os.open(parent, os.O_RDONLY | os.O_NOFOLLOW)
                    try:
                        os.fsync(descriptor)
                    finally:
                        os.close(descriptor)
            if history_sha256(PrivateUpgradeStore(new, create=False)) != entry.history_sha256:
                raise UpgradeConflict("Legacy smoke bytes changed during archive")
        verify_projection_boundary(settings, effect, control)
        retained = run.read("cold.smoke.history.receipt.json", UpgradeRecoverySmokeHistoryArchive)
        if retained is None:
            run.write("cold.smoke.history.receipt.json", proof, replace=False)
        elif retained != proof:
            raise UpgradeConflict("Legacy smoke archive receipt differs")
        return proof
