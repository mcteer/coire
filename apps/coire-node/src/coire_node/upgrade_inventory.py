"""Strict OS process discovery for upgrade drain; unreadable hosts are never idle."""

import os
from collections.abc import Iterable
from pathlib import Path

import psutil
from pydantic import TypeAdapter

from coire_core.errors import UpgradeUnavailable
from coire_core.models.upgrade_collective import NodeUpgradeCollectiveResult
from coire_core.models.upgrades import (
    CoreNodeSmokeLifecycle,
    NodeUpgradeCollectiveScope,
    UpgradeId,
    UpgradeInventoryEntry,
    UpgradeOwnerInventory,
)
from coire_core.upgrade_journal import PrivateUpgradeStore
from coire_node.training.process_inventory import verified_native_system_process


def native_process_entries(owner: str, markers: Iterable[str]) -> tuple[UpgradeInventoryEntry, ...]:
    needles = tuple(markers)
    entries: list[UpgradeInventoryEntry] = []
    for index, process in enumerate(psutil.process_iter()):
        if index >= 4096:
            raise UpgradeUnavailable("Native process scan exceeds its bound")
        try:
            argv = process.cmdline()
            if not any(marker in argument for marker in needles for argument in argv):
                continue
            born = process.create_time()
            if psutil.Process(process.pid).create_time() != born:
                raise UpgradeUnavailable("Native process identity changed during inventory")
            entries.append(
                UpgradeInventoryEntry(
                    owner=owner,
                    kind="native-process",
                    work_id=f"pid-{process.pid}",
                    state="active",
                    process_id=process.pid,
                    process_created_at=born,
                )
            )
            if len(entries) > 4096:
                raise UpgradeUnavailable("Native process inventory exceeds its bound")
        except psutil.NoSuchProcess:
            continue
        except psutil.AccessDenied:
            if not verified_native_system_process(process):
                raise UpgradeUnavailable("Native process inventory is incomplete") from None
    # The inventory never emits argv, executable paths, tokens or model contents.
    return tuple(entries)


def upgrade_smoke_entries(state_root: Path) -> tuple[UpgradeInventoryEntry, ...]:
    root = state_root / "upgrades/smokes"
    if not root.exists() and not root.is_symlink():
        return ()
    store = PrivateUpgradeStore(root)
    children = list(store.root.iterdir())
    if len(children) > 64:
        raise UpgradeUnavailable("Upgrade smoke ownership exceeds its bound")
    entries: list[UpgradeInventoryEntry] = []
    for child in children:
        if not child.is_dir() or child.is_symlink():
            raise UpgradeUnavailable("Upgrade smoke owner is unreconciled")
        owner = PrivateUpgradeStore(store.path(child.name)).read(
            "owner.json", UpgradeOwnerInventory
        )
        if owner is None or owner.owner != "engines" or owner.status != "complete":
            raise UpgradeUnavailable("Upgrade smoke ownership is incomplete")
        entries.extend(owner.entries)
    return tuple(entries)


def core_smoke_entries(state_root: Path) -> tuple[UpgradeInventoryEntry, ...]:
    root = state_root / "upgrades/core-smokes"
    if not root.exists() and not root.is_symlink():
        return ()
    store = PrivateUpgradeStore(root)
    children = list(store.root.iterdir())
    if len(children) > 4096:
        raise UpgradeUnavailable("Core smoke ownership exceeds its bound")
    entries: list[UpgradeInventoryEntry] = []
    for child in children:
        if not child.is_dir() or child.is_symlink():
            raise UpgradeUnavailable("Core smoke ownership is unreconciled")
        lifecycle = PrivateUpgradeStore(store.path(child.name)).read(
            "lifecycle.json", CoreNodeSmokeLifecycle
        )
        if lifecycle is None or str(lifecycle.scope.engine_id) != child.name:
            raise UpgradeUnavailable("Core smoke ownership is absent or changed")
        if lifecycle.state != "released":
            entries.append(
                UpgradeInventoryEntry(
                    owner="engines",
                    kind="core-upgrade-smoke",
                    work_id=child.name,
                    state="active" if lifecycle.state == "active" else "cleaning",
                    holds=("core-smoke-memory",),
                )
            )
    return tuple(entries)


def collective_entries(state_root: Path) -> tuple[UpgradeInventoryEntry, ...]:
    root = state_root / "upgrades/collectives"
    if not root.exists() and not root.is_symlink():
        return ()
    store = PrivateUpgradeStore(root)
    entries: list[UpgradeInventoryEntry] = []
    count = 0

    def bounded() -> None:
        nonlocal count
        count += 1
        if count > 4096:
            raise UpgradeUnavailable("Collective ownership exceeds its bound")

    for job in store.root.iterdir():
        bounded()
        for stage in PrivateUpgradeStore(store.path(job.name)).root.iterdir():
            bounded()
            for index in PrivateUpgradeStore(stage).root.iterdir():
                bounded()
                owner = PrivateUpgradeStore(index)
                scope = owner.read("scope.json", NodeUpgradeCollectiveScope)
                result = owner.read("result.json", NodeUpgradeCollectiveResult)
                if scope is None:
                    # A refused pre-dispatch authority check can leave only its
                    # reservation lock. No worker may start before scope.json is
                    # durable. Prove that absence under the same exclusive lock;
                    # a held lock, result or foreign file still blocks drain.
                    try:
                        TypeAdapter(UpgradeId).validate_python(job.name)
                    except ValueError:
                        raise UpgradeUnavailable("Collective ownership is incomplete") from None
                    if stage.name not in {"candidate", "source"} or index.name not in {
                        "0",
                        "1",
                        "2",
                    }:
                        raise UpgradeUnavailable("Collective ownership is incomplete")
                    with owner.ownership("collective.lock"):
                        if (
                            owner.read("scope.json", NodeUpgradeCollectiveScope) is not None
                            or owner.read("result.json", NodeUpgradeCollectiveResult) is not None
                            or any(path.name != "collective.lock" for path in owner.root.iterdir())
                        ):
                            raise UpgradeUnavailable("Collective ownership is incomplete")
                    continue
                from coire_node.collective_cleanup import retained_cleanup_receipt

                if (job.name, stage.name, index.name) != (
                    scope.plan.operation.job_id,
                    scope.request.stage,
                    str(scope.index),
                ):
                    raise UpgradeUnavailable("Collective retained scope path authority differs")
                cleanup = retained_cleanup_receipt(owner, scope)
                if result is not None and cleanup is not None:
                    raise UpgradeUnavailable(
                        "Collective retains conflicting worker and cleanup outcomes"
                    )
                if result is None and cleanup is not None:
                    continue  # Logical dispatch closed; physical owners are still independently scanned.
                if result is None:
                    entries.append(
                        UpgradeInventoryEntry(
                            owner="probes",
                            kind="upgrade-collective",
                            work_id=str(scope.command_id),
                            state="cleaning",
                            holds=("unresolved-collective",),
                        )
                    )
                elif (
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
                ) or len(result.observations) != 1:
                    raise UpgradeUnavailable("Collective retained result authority differs")
    return tuple(entries)


def read_owner_json(path: Path, maximum: int = 512 * 1024) -> object:
    """Read one bounded private owner file, refusing links and silent decoding losses."""
    import json
    import stat

    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        details = os.fstat(fd)
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_uid != os.getuid()
            or details.st_mode & 0o077
            or details.st_nlink != 1
            or details.st_size > maximum
        ):
            raise UpgradeUnavailable("Owner journal cannot be trusted")
        with os.fdopen(fd, "rb", closefd=False) as handle:
            data = handle.read(maximum + 1)
        if len(data) > maximum:
            raise UpgradeUnavailable("Owner journal exceeds its bound")
        after = os.fstat(fd)
        current = path.lstat()
        fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
        if (
            any(getattr(details, key) != getattr(after, key) for key in fields)
            or any(getattr(current, key) != getattr(after, key) for key in fields)
            or not stat.S_ISREG(current.st_mode)
            or current.st_uid != os.getuid()
            or current.st_mode & 0o077
            or current.st_nlink != 1
        ):
            raise UpgradeUnavailable("Owner journal changed during verification")

        def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
            result: dict[str, object] = {}
            for key, value in pairs:
                if key in result:
                    raise UpgradeUnavailable("Owner journal contains duplicate fields")
                result[key] = value
            return result

        return json.loads(data, object_pairs_hook=unique_object)
    finally:
        os.close(fd)
