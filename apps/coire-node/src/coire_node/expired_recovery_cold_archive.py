"""Journaled legacy inventory projection after actual collective cleanup proof."""

import hashlib
import os
import stat
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path

from opentelemetry import trace

from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrade_collective import NodeUpgradeCollectiveResult
from coire_core.models.upgrade_recovery import (
    UpgradeExpiredRecoveryColdCollectiveArchive,
    UpgradeExpiredRecoveryColdCollectiveArchiveReceipt,
    UpgradeExpiredRecoveryColdSourceProcess,
)
from coire_core.models.upgrades import NODE_UPGRADE_OWNERS, NodeUpgradeCollectiveScope
from coire_core.settings import Settings
from coire_core.upgrade_journal import PrivateUpgradeStore
from coire_node.expired_recovery_cold_launch import require_live_cold_source
from coire_node.expired_recovery_cold_source import stores, verify_projection_boundary
from coire_node.upgrade_guard import NativeGuardTransport

tracer = trace.get_tracer("coire.node.cold_recovery")


def archive_absent_dispatches(
    settings: Settings,
    source: UpgradeExpiredRecoveryColdSourceProcess,
    control: NativeGuardTransport,
    collectives: PrivateUpgradeStore,
    original: Path,
    destination: Path,
) -> UpgradeExpiredRecoveryColdCollectiveArchiveReceipt:
    """Retain a positive no-dispatch observation, never create fictitious history."""
    effect = source.launch.source.projection
    _, run, _ = stores(settings, effect.owner)
    node = PrivateUpgradeStore(Path(settings.node_state_dir) / "upgrades", create=False)
    with run.ownership("cold.collective.archive.lock"), node.ownership("maintenance.lock"):
        verify_projection_boundary(settings, effect, control)
        require_live_cold_source(settings, source)
        if (
            original.exists()
            or original.is_symlink()
            or destination.exists()
            or destination.is_symlink()
        ):
            raise UpgradeConflict("Declared absent collective dispatch has a namespace")
        previous = run.read(
            "cold.collective.archive.receipt.json",
            UpgradeExpiredRecoveryColdCollectiveArchiveReceipt,
        )
        if previous is not None:
            if previous.archive.source != source or previous.archive.scopes:
                raise UpgradeConflict("Absent collective archive replay differs")
            verify_archive_cleanup(previous.archive)
            return previous
        plan = effect.owner.intent.original_plan
        retained = run.read(
            "cold.collective.archive.intent.json", UpgradeExpiredRecoveryColdCollectiveArchive
        )
        proof = retained or UpgradeExpiredRecoveryColdCollectiveArchive(
            source=source,
            scopes=(),
            interrupted_scope_sha256s=(),
            previous_archive_sha256=(
                effect.owner.continuation.previous_archive_sha256
                or effect.owner.continuation.untransferred_archive_sha256
            )
            if effect.owner.continuation is not None
            else None,
            local=control.local_inventory(
                effect.owner.intent.local_inventory_plan(),
                deadline_at=effect.owner.intent.deadline_at,
            ),
            peer=control.peer_inventory(plan, deadline_at=effect.owner.intent.deadline_at),
            history_sha256=hashlib.sha256(b"coire-cold-no-original-dispatch-v1").hexdigest(),
            prepared_at=datetime.now(UTC),
        )
        if (
            proof.source != source
            or proof.scopes
            or proof.interrupted_scope_sha256s
            or proof.history_sha256
            != hashlib.sha256(b"coire-cold-no-original-dispatch-v1").hexdigest()
        ):
            raise UpgradeConflict("Absent collective archive retained intent differs")
        verify_archive_cleanup(proof)
        if retained is None:
            run.write("cold.collective.archive.intent.json", proof, replace=False)
        verify_projection_boundary(settings, effect, control)
        if original.exists() or original.is_symlink():
            raise UpgradeConflict("Collective namespace appeared after absence capture")
        effect_proof = run.read(
            "cold.collective.archive.effect.json", UpgradeExpiredRecoveryColdCollectiveArchive
        )
        if effect_proof is None:
            run.write("cold.collective.archive.effect.json", proof, replace=False)
        elif effect_proof != proof:
            raise UpgradeConflict("Absent collective archive effect differs")
        receipt = UpgradeExpiredRecoveryColdCollectiveArchiveReceipt(
            initial_archive_sha256=proof.sha256(), archive=proof, archived_at=datetime.now(UTC)
        )
        run.write("cold.collective.archive.receipt.json", receipt, replace=False)
        return receipt


def absent_history_sha256(node: PrivateUpgradeStore, run: PrivateUpgradeStore, job_id: str) -> str:
    parent = run.path("collective-history")
    if parent.is_symlink():
        raise UpgradeConflict("Absent archive parent is linked")
    if parent.exists():
        PrivateUpgradeStore(parent, create=False)
    collectives = PrivateUpgradeStore(node.path("collectives"), create=False)
    for path in (collectives.path(job_id), parent / job_id):
        if path.exists() or path.is_symlink():
            raise UpgradeConflict("Declared absent dispatch history has a namespace")
    return hashlib.sha256(b"coire-cold-no-original-dispatch-v1").hexdigest()


def retained_dispatches(
    history: PrivateUpgradeStore,
) -> tuple[tuple[NodeUpgradeCollectiveScope, ...], tuple[str, ...]]:
    scopes = []
    interrupted = []
    for stage_path in sorted(history.root.iterdir()):
        if stage_path.name not in {"candidate", "source"}:
            raise UpgradeUnavailable("Cold collective history has an unknown stage")
        stage = PrivateUpgradeStore(stage_path, create=False)
        for slot_path in sorted(stage.root.iterdir()):
            if slot_path.name not in {"0", "1", "2"}:
                raise UpgradeUnavailable("Cold collective history has an unknown slot")
            slot = PrivateUpgradeStore(slot_path, create=False)
            if any(
                p.name not in {"scope.json", "result.json", "collective.lock"}
                for p in slot.root.iterdir()
            ):
                raise UpgradeUnavailable("Cold collective history contains an unknown record")
            scope = slot.read("scope.json", NodeUpgradeCollectiveScope)
            result = slot.read("result.json", NodeUpgradeCollectiveResult)
            if scope is None:
                if result is not None or any(
                    p.name != "collective.lock" for p in slot.root.iterdir()
                ):
                    raise UpgradeUnavailable("Cold collective history has no dispatch authority")
                continue
            if (scope.plan.operation.job_id, scope.request.stage, str(scope.index)) != (
                history.root.name,
                stage.root.name,
                slot.root.name,
            ):
                raise UpgradeConflict("Cold collective history path differs from scope")
            if result is not None and (
                result.job_id,
                result.generation,
                result.plan_sha256,
                result.stage,
                result.deadline_at,
            ) != (
                scope.plan.operation.job_id,
                scope.request.generation,
                scope.request.plan_sha256,
                scope.request.stage,
                scope.request.deadline_at,
            ):
                raise UpgradeConflict("Cold collective result authority differs")
            scopes.append(scope)
            if result is None:
                interrupted.append(scope.sha256())
    return tuple(scopes), tuple(interrupted)


def history_sha256(history: PrivateUpgradeStore) -> str:
    """Hash exact retained bytes, refusing links, changed paths and oversized trees."""
    digest = hashlib.sha256()
    total = 0
    paths = sorted(history.root.rglob("*"))
    if len(paths) > 64:
        raise UpgradeUnavailable("Cold collective history exceeds its file bound")
    for path in paths:
        details = path.lstat()
        if stat.S_ISDIR(details.st_mode):
            PrivateUpgradeStore(path, create=False)
            continue
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_uid != os.getuid()
            or details.st_mode & 0o077
            or details.st_nlink != 1
        ):
            raise UpgradeUnavailable("Cold collective history is not private regular data")
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            before = os.fstat(descriptor)
            with os.fdopen(descriptor, "rb", closefd=False) as stream:
                payload = stream.read(2 * 1024 * 1024 + 1)
            after = os.fstat(descriptor)
            current = path.lstat()
            fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
            if any(
                getattr(before, key) != getattr(after, key)
                or getattr(current, key) != getattr(after, key)
                for key in fields
            ):
                raise UpgradeUnavailable("Cold collective history changed while observed")
            total += len(payload)
            if len(payload) > 2 * 1024 * 1024 or total > 8 * 1024 * 1024:
                raise UpgradeUnavailable("Cold collective history exceeds its byte bound")
            name = path.relative_to(history.root).as_posix().encode()
            digest.update(
                len(name).to_bytes(4, "big") + name + len(payload).to_bytes(8, "big") + payload
            )
        finally:
            os.close(descriptor)
    return digest.hexdigest()


def verify_archive_cleanup(proof: UpgradeExpiredRecoveryColdCollectiveArchive) -> None:
    """Remove only exact captured logical obligations from actual canonical reads."""
    proof = UpgradeExpiredRecoveryColdCollectiveArchive.model_validate_json(proof.model_dump_json())
    commands = {
        str(scope.command_id)
        for scope in proof.scopes
        if scope.sha256() in proof.interrupted_scope_sha256s
    }
    if proof.previous_archive_sha256 is not None:
        # The accepted flat binding pins the predecessor's completed archive.
        # These are preserved historical gaps, not current logical obligations.
        commands = set()
    matched: set[str] = set()
    owners = []
    for owner in proof.local.owners:
        entries = []
        for entry in owner.entries:
            if (
                owner.owner == "probes"
                and entry.kind == "upgrade-collective"
                and entry.work_id in commands
                and entry.state == "cleaning"
                and entry.holds == ("unresolved-collective",)
                and entry.process_id is None
                and entry.process_created_at is None
            ):
                if entry.work_id in matched:
                    raise UpgradeUnavailable(
                        "Cold collective cleanup inventory repeats an obligation"
                    )
                matched.add(entry.work_id)
            else:
                entries.append(entry)
        owners.append(owner.model_copy(update={"entries": tuple(entries)}))
    if (
        matched != commands
        or not proof.local.model_copy(update={"owners": tuple(owners)}).idle(NODE_UPGRADE_OWNERS)
        or not proof.peer.idle(NODE_UPGRADE_OWNERS)
    ):
        raise UpgradeUnavailable("Cold collective cleanup retains physical or unknown ownership")


@tracer.start_as_current_span("coire.node.cold_recovery_archive_collectives")
def archive_cold_collectives(
    settings: Settings,
    source: UpgradeExpiredRecoveryColdSourceProcess,
    control: NativeGuardTransport,
) -> UpgradeExpiredRecoveryColdCollectiveArchiveReceipt:
    """Move unchanged history only after new-authority physical cleanup verification.

    Original scope/results/deadlines are byte-identical in a protected archive.
    No old result is invented and no interrupted command can be relaunched.
    The original Core obligation and both maintenance fences remain active.
    """
    effect = source.launch.source.projection
    if settings.node_name != "coire-edge-a":
        raise UpgradeUnavailable("Cold archive requires declared collective rank zero")
    _, run, _ = stores(settings, effect.owner)
    node = PrivateUpgradeStore(Path(settings.node_state_dir) / "upgrades", create=False)
    collectives = PrivateUpgradeStore(node.path("collectives"), create=False)
    archives = PrivateUpgradeStore(run.path("collective-history"))
    job_id = effect.owner.intent.request.original_job_id
    old_path, archive_path = collectives.path(job_id), archives.path(job_id)
    if not effect.owner.intent.request.original_scope_sha256s:
        return archive_absent_dispatches(
            settings, source, control, collectives, old_path, archive_path
        )
    old_parent = collectives
    continuation = effect.owner.continuation
    predecessor_archive = None
    if continuation is not None and (
        continuation.previous_archive_sha256 is not None
        or continuation.untransferred_archive_custodian is not None
    ):
        from coire_node.expired_recovery_cold_continuation import previous_source

        previous_source(settings, continuation)
        if old_path.exists() or old_path.is_symlink():
            raise UpgradeConflict("Cold continuation original history unexpectedly reappeared")
        old_parent = PrivateUpgradeStore(
            run.root.parent
            / (continuation.untransferred_archive_custodian or continuation.previous_recovery_id)
            / "collective-history",
            create=False,
        )
        old_path = old_parent.path(job_id)
        predecessor_archive = continuation.previous_archive_sha256
        if continuation.untransferred_archive_custodian is not None:
            old_parent = PrivateUpgradeStore(
                run.root.parent
                / continuation.untransferred_archive_custodian
                / "collective-history",
                create=False,
            )
            old_path = old_parent.path(job_id)
            predecessor_archive = continuation.untransferred_archive_sha256
    with (
        run.ownership("cold.collective.archive.lock"),
        node.ownership("maintenance.lock"),
        ExitStack() as stack,
    ):
        verify_projection_boundary(settings, effect, control)
        require_live_cold_source(settings, source)
        proof = run.read(
            "cold.collective.archive.intent.json", UpgradeExpiredRecoveryColdCollectiveArchive
        )
        if old_path.exists() and archive_path.exists():
            raise UpgradeConflict("Cold collective history has two namespace owners")
        history = PrivateUpgradeStore(old_path if old_path.exists() else archive_path, create=False)
        # Pin every original dispatch before observing or changing its namespace.
        for stage in sorted(history.root.iterdir()):
            stage_store = PrivateUpgradeStore(stage, create=False)
            for slot in sorted(stage_store.root.iterdir()):
                stack.enter_context(
                    PrivateUpgradeStore(slot, create=False).ownership("collective.lock")
                )
        scopes, interrupted = retained_dispatches(history)
        digest = history_sha256(history)
        if (
            continuation is not None
            and predecessor_archive is not None
            and digest
            != (continuation.previous_history_sha256 or continuation.untransferred_history_sha256)
        ):
            raise UpgradeConflict("Cold continuation archive bytes differ from accepted binding")
        if proof is None:
            if history.root != old_path.resolve() or not old_path.exists():
                raise UpgradeConflict("Unrecorded cold collective archive has no retained intent")
            plan = effect.owner.intent.original_plan
            local = control.local_inventory(
                effect.owner.intent.local_inventory_plan(),
                deadline_at=effect.owner.intent.deadline_at,
            )
            peer = control.peer_inventory(plan, deadline_at=effect.owner.intent.deadline_at)
            proof = UpgradeExpiredRecoveryColdCollectiveArchive(
                source=source,
                scopes=scopes,
                interrupted_scope_sha256s=interrupted,
                local=local,
                peer=peer,
                history_sha256=digest,
                previous_archive_sha256=predecessor_archive,
                prepared_at=datetime.now(UTC),
            )
            verify_archive_cleanup(proof)
            verify_projection_boundary(settings, effect, control)
            if history_sha256(history) != digest:
                raise UpgradeConflict("Cold collective history changed before intent retention")
            run.write("cold.collective.archive.intent.json", proof, replace=False)
        if (proof.source, proof.scopes, proof.interrupted_scope_sha256s, proof.history_sha256) != (
            source,
            scopes,
            interrupted,
            digest,
        ):
            raise UpgradeConflict("Cold collective archive history differs")
        verify_archive_cleanup(proof)
        verify_projection_boundary(settings, effect, control)
        require_live_cold_source(settings, source)
        actual = run.read(
            "cold.collective.archive.effect.json", UpgradeExpiredRecoveryColdCollectiveArchive
        )
        if old_path.exists():
            # Unconsumed observations may refresh after a crash; they never
            # renew the intent's root budget or replace its initial history.
            # Fetch after expensive graph checks, then reauthorize immediately
            # before publication and the namespace effect.
            plan = effect.owner.intent.original_plan
            local = control.local_inventory(
                effect.owner.intent.local_inventory_plan(),
                deadline_at=effect.owner.intent.deadline_at,
            )
            peer = control.peer_inventory(plan, deadline_at=effect.owner.intent.deadline_at)
            actual = UpgradeExpiredRecoveryColdCollectiveArchive.model_validate(
                proof.model_dump(mode="json")
                | {
                    "local": local,
                    "peer": peer,
                    "prepared_at": datetime.now(UTC),
                }
            )
            verify_archive_cleanup(actual)
            if control.verify_cold_recovery_owner(effect.owner) != effect.owner:
                raise UpgradeUnavailable("Cold archive authority changed before namespace effect")
            run.write("cold.collective.archive.effect.json", actual)
            before = old_path.lstat()
            if (
                before.st_dev != archives.root.stat().st_dev
                or archive_path.exists()
                or old_path.is_symlink()
            ):
                raise UpgradeUnavailable("Cold collective archive is not one owned filesystem")
            if history_sha256(history) != proof.history_sha256:
                raise UpgradeConflict("Cold collective history changed before namespace projection")
            os.rename(old_path, archive_path)
        if actual is None or (
            actual.source,
            actual.scopes,
            actual.interrupted_scope_sha256s,
            actual.history_sha256,
        ) != (proof.source, proof.scopes, proof.interrupted_scope_sha256s, proof.history_sha256):
            raise UpgradeConflict("Cold archive has no exact retained effect observation")
        verify_archive_cleanup(actual)
        for directory in (old_parent.root, archives.root):
            descriptor = os.open(directory, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        if history_sha256(PrivateUpgradeStore(archive_path, create=False)) != proof.history_sha256:
            raise UpgradeConflict("Cold archived bytes differ from preserved history")
        verify_projection_boundary(settings, effect, control)
        previous = run.read(
            "cold.collective.archive.receipt.json",
            UpgradeExpiredRecoveryColdCollectiveArchiveReceipt,
        )
        if previous is not None:
            if previous.archive != actual or previous.initial_archive_sha256 != proof.sha256():
                raise UpgradeConflict("Cold collective archive receipt differs")
            return previous
        receipt = UpgradeExpiredRecoveryColdCollectiveArchiveReceipt(
            initial_archive_sha256=proof.sha256(), archive=actual, archived_at=datetime.now(UTC)
        )
        run.write("cold.collective.archive.receipt.json", receipt, replace=False)
        return receipt
