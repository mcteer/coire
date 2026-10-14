"""Read-only fixed native data stores; never expose names or allocate model weights."""

import hashlib
import os
import stat
import time
from datetime import UTC, datetime
from pathlib import Path

from opentelemetry import trace

from coire_core.errors import UpgradeUnavailable
from coire_core.models.upgrade_recovery import UpgradeExpiredRecoveryNativeDataSnapshot
from coire_core.models.upgrades import UpgradeExecutionPlan
from coire_core.settings import Settings

tracer = trace.get_tracer("coire.node.recovery_data")


def fingerprint_store(root: Path, deadline_tick: float) -> tuple[str, int, int]:
    """Stream exact stable regular bytes, including pending local obligation files."""
    digest = hashlib.sha256()
    try:
        original_root = root.lstat()
    except FileNotFoundError:
        return digest.hexdigest(), 0, 0
    if (
        not stat.S_ISDIR(original_root.st_mode)
        or original_root.st_uid != os.getuid()
        or original_root.st_mode & 0o022
    ):
        raise UpgradeUnavailable("Native preserved store ownership is uncertain")
    paths = []
    count = total = 0
    # Bound discovery rather than first materializing an unbounded rglob.
    for index, path in enumerate(root.rglob("*")):
        if index >= 8192 or time.monotonic() >= deadline_tick:
            raise UpgradeUnavailable("Native preserved store discovery exceeded its bound")
        paths.append(path)
    for path in sorted(paths):
        details = path.lstat()
        if stat.S_ISDIR(details.st_mode):
            if details.st_uid != os.getuid() or details.st_mode & 0o022:
                raise UpgradeUnavailable("Native preserved directory ownership differs")
            continue
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_uid != os.getuid()
            or details.st_nlink != 1
            or details.st_mode & 0o022
        ):
            raise UpgradeUnavailable("Native preserved file is linked or unsafe")
        count += 1
        total += details.st_size
        if count > 4096 or total > 1024**4:
            raise UpgradeUnavailable("Native preserved store exceeds its bounded size")
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            before = os.fstat(descriptor)
            content = hashlib.sha256()
            read_bytes = 0
            while True:
                if time.monotonic() >= deadline_tick:
                    raise UpgradeUnavailable(
                        "Native preserved content exceeded its observation budget"
                    )
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                content.update(chunk)
                read_bytes += len(chunk)
                if read_bytes > details.st_size:
                    raise UpgradeUnavailable("Native preserved file grew during observation")
            after = os.fstat(descriptor)
            current = path.lstat()
            fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
            if read_bytes != details.st_size or any(
                getattr(details, key) != getattr(before, key)
                or getattr(before, key) != getattr(after, key)
                or getattr(current, key) != getattr(after, key)
                for key in fields
            ):
                raise UpgradeUnavailable("Native preserved content changed during observation")
            name = path.relative_to(root).as_posix().encode()
            digest.update(len(name).to_bytes(4, "big") + name + content.digest())
        finally:
            os.close(descriptor)
    current_root = root.lstat()
    if (current_root.st_dev, current_root.st_ino) != (original_root.st_dev, original_root.st_ino):
        raise UpgradeUnavailable("Native preserved store root changed during observation")
    final_paths = []
    for index, path in enumerate(root.rglob("*")):
        if index >= 8192 or time.monotonic() >= deadline_tick:
            raise UpgradeUnavailable("Native preserved final census exceeded its bound")
        final_paths.append(path)
    if sorted(paths) != sorted(final_paths):
        raise UpgradeUnavailable("Native preserved namespace changed during observation")
    return digest.hexdigest(), count, total


@tracer.start_as_current_span("coire.node.recovery_native_data_snapshot", record_exception=False)
def capture_native_data(
    settings: Settings, plan: UpgradeExecutionPlan, *, deadline_at: datetime
) -> UpgradeExpiredRecoveryNativeDataSnapshot:
    if (
        settings.node_name not in {"coire-edge-a", "coire-edge-b"}
        or settings.node_name != plan.operation.node
    ):
        raise UpgradeUnavailable("Native preservation requires the declared original Studio")
    remaining = min(30.0, (deadline_at - datetime.now(UTC)).total_seconds())
    if remaining <= 0:
        raise UpgradeUnavailable("Native preservation observation has no captured budget")
    deadline_tick = time.monotonic() + remaining
    roots = {
        "datasets": Path(settings.training_dataset_dir),
        "artifacts": Path(settings.node_state_dir) / "training/artifacts",
        "components": Path(settings.node_state_dir) / "training/components",
    }
    hashes = {}
    counts = {}
    total = 0
    try:
        for name, root in roots.items():
            fingerprint, count, size = fingerprint_store(root, deadline_tick)
            hashes[name] = fingerprint
            counts[name] = count
            total += size
    except (OSError, ValueError):
        raise UpgradeUnavailable(
            "Native preservation cannot verify configured stable content"
        ) from None
    if time.monotonic() >= deadline_tick or datetime.now(UTC) >= deadline_at:
        raise UpgradeUnavailable("Native preservation exceeded its captured observation budget")
    return UpgradeExpiredRecoveryNativeDataSnapshot.model_validate(
        {
            "original_job_id": plan.operation.job_id,
            "original_generation": plan.operation.generation,
            "source_sha256": plan.source.manifest_sha256,
            "node": settings.node_name,
            "root_sha256s": hashes,
            "file_counts": counts,
            "total_bytes": total,
            "observed_at": datetime.now(UTC),
        }
    )
