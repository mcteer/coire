"""Smoke a staged node environment before atomically making it active."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import platform
import shutil
import stat
import struct
import subprocess
import sys
import threading
import uuid
from collections import OrderedDict
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from coire_core.models.upgrades import NativeRuntimeSelection, PreparedNativeRuntime, StudioName
    from coire_core.settings import Settings


_content_cache: OrderedDict[tuple[str, int, int, int, int, int, int, int, int, int], str] = (
    OrderedDict()
)
_content_cache_lock = threading.Lock()
# Cold continuation verifies Source, current support and predecessor support.
# Their measured 89,363-file working set must fit without repeatedly evicting
# verified bytes. This remains a fixed bounded cache, with per-path revalidation.
_CONTENT_CACHE_LIMIT = 131072


def _content_key(
    path: Path, details: os.stat_result
) -> tuple[str, int, int, int, int, int, int, int, int, int]:
    return (
        str(path),
        details.st_dev,
        details.st_ino,
        details.st_size,
        details.st_mtime_ns,
        details.st_ctime_ns,
        details.st_uid,
        details.st_gid,
        details.st_mode,
        details.st_nlink,
    )


def _cached_path_content(path: Path, before: os.stat_result) -> str | None:
    """Reuse verified bytes with two complete, matching kernel metadata reads.

    This checks each file individually on every graph walk. A changed inode,
    content ctime, mode, owner or link count cannot use the cached digest. Cache
    misses still read the actual bytes through O_NOFOLLOW below.
    """
    key = _content_key(path, before)
    with _content_cache_lock:
        cached = _content_cache.get(key)
        if cached is not None:
            _content_cache.move_to_end(key)
    if cached is not None and _content_key(path, path.lstat()) != key:
        raise ValueError("runtime changed during cached content verification")
    return cached


def _verified_file_content(fd: int, path: Path, before: os.stat_result) -> str:
    """Reuse bytes only while the kernel proves the same unchanged owned inode.

    Every graph walk still checks all paths, links, modes, ownership and file
    metadata. Both mtime and unforgeable user-level ctime participate; restoring
    mtime after a same-size edit cannot reuse prior content verification.
    """
    key = _content_key(path, before)
    with _content_cache_lock:
        cached = _content_cache.get(key)
        if cached is not None:
            _content_cache.move_to_end(key)
            return cached
    content = hashlib.sha256()
    with os.fdopen(fd, "rb", closefd=False) as handle:
        while chunk := handle.read(1024 * 1024):
            content.update(chunk)
    result = content.hexdigest()
    after = os.fstat(fd)
    if (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
        before.st_uid,
        before.st_gid,
        before.st_mode,
        before.st_nlink,
    ) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
        after.st_uid,
        after.st_gid,
        after.st_mode,
        after.st_nlink,
    ) or (path.lstat().st_dev, path.lstat().st_ino) != (before.st_dev, before.st_ino):
        raise ValueError("runtime changed during content verification")
    with _content_cache_lock:
        _content_cache[key] = result
        _content_cache.move_to_end(key)
        if len(_content_cache) > _CONTENT_CACHE_LIMIT:
            _content_cache.popitem(last=False)
    return result


def artifact_graph_sha256(paths: list[Path]) -> str:
    """Full digest of named locked inputs; filenames and byte boundaries are explicit."""
    if not paths or len({path.name for path in paths}) != len(paths):
        raise ValueError("runtime inputs must have distinct names")
    entries: list[dict[str, str | int]] = []
    for path in sorted(paths, key=lambda item: item.name):
        digest = hashlib.sha256()
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as handle:
            before = os.fstat(handle.fileno())
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_uid != os.getuid()
                or before.st_nlink != 1
            ):
                raise ValueError("runtime input ownership or link count differs")
            copied = 0
            while chunk := handle.read(1024 * 1024):
                copied += len(chunk)
                if copied > before.st_size:
                    raise ValueError("runtime input grew during hashing")
                digest.update(chunk)
            after = os.fstat(handle.fileno())
        observed = path.lstat()
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ) or (observed.st_dev, observed.st_ino) != (before.st_dev, before.st_ino):
            raise ValueError("runtime input changed during hashing")
        entries.append({"name": path.name, "bytes": before.st_size, "sha256": digest.hexdigest()})
    document = {"protocol": "coire-node-runtime-v3", "artifacts": entries}
    return hashlib.sha256(
        json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def environment_content_sha256(environment: Path, interpreter_root: Path) -> str:
    """Bind every installed file and controlled interpreter dependency to full bytes.

    Links may refer only to these two retained trees. Runtime files, including
    bytecode and native libraries, are included; there are no filename exclusions.
    """
    roots = {
        "environment": environment.resolve(strict=True),
        "interpreter": interpreter_root.resolve(strict=True),
    }
    if any(not root.is_dir() or root.is_symlink() for root in (environment, interpreter_root)):
        raise ValueError("runtime root must be an existing unlinked directory")
    digest = hashlib.sha256()
    count = 0
    for label, root in sorted(roots.items()):
        if root.stat().st_uid != os.getuid():
            raise ValueError("runtime root ownership differs")
        digest.update(
            json.dumps(
                {"root": label, "path": "", "mode": stat.S_IMODE(root.stat().st_mode)},
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        )
        digest.update(b"\n")
        for path in sorted(root.rglob("*")):
            count += 1
            if count > 250000:
                raise ValueError("runtime graph exceeds its file bound")
            details = path.lstat()
            relative = path.relative_to(root).as_posix()
            record: dict[str, str | int] = {
                "root": label,
                "path": relative,
                "mode": stat.S_IMODE(details.st_mode),
            }
            if stat.S_ISLNK(details.st_mode):
                destination = path.resolve(strict=True)
                controlled = [
                    (name, candidate)
                    for name, candidate in roots.items()
                    if destination.is_relative_to(candidate)
                ]
                if not controlled:
                    raise ValueError("runtime link points outside retained interpreter trees")
                name, candidate = max(controlled, key=lambda item: len(item[1].parts))
                record["link"] = name + ":" + destination.relative_to(candidate).as_posix()
            elif stat.S_ISREG(details.st_mode):
                if details.st_nlink != 1 or details.st_uid != os.getuid():
                    raise ValueError("runtime file ownership or link count differs")
                cached = _cached_path_content(path, details)
                if cached is not None:
                    record.update(bytes=details.st_size, sha256=cached)
                    digest.update(
                        json.dumps(record, sort_keys=True, separators=(",", ":")).encode()
                    )
                    digest.update(b"\n")
                    continue
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                try:
                    before = os.fstat(fd)
                    if _content_key(path, details) != _content_key(path, before):
                        raise ValueError("runtime changed before content verification")
                    content = _verified_file_content(fd, path, before)
                    after = os.fstat(fd)
                    if (before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                        after.st_ino,
                        after.st_size,
                        after.st_mtime_ns,
                        after.st_ctime_ns,
                    ) or path.lstat().st_ino != before.st_ino:
                        raise ValueError("runtime changed during content verification")
                    record.update(bytes=before.st_size, sha256=content)
                finally:
                    os.close(fd)
            elif not stat.S_ISDIR(details.st_mode) or details.st_uid != os.getuid():
                raise ValueError("runtime graph contains a special file")
            digest.update(json.dumps(record, sort_keys=True, separators=(",", ":")).encode())
            digest.update(b"\n")
    return digest.hexdigest()


def retain_interpreter(source: Path, target: Path) -> None:
    """Copy the complete interpreter graph, rebasing internal absolute links.

    The source is never modified. No destination is published until every link
    has been proven to stay inside the independent retained tree.
    """
    if target.exists() or target.is_symlink():
        raise FileExistsError("retained interpreter already exists")
    if source.is_symlink() or not source.is_dir():
        raise ValueError("interpreter root must be an unlinked directory")
    source = source.resolve(strict=True)
    target = target.absolute()
    if target.is_relative_to(source):
        raise ValueError("retained interpreter cannot be inside its source")
    source_digest = environment_content_sha256(source, source)
    temporary = target.with_name(f".{target.name}.staging.{uuid.uuid4().hex}")
    try:
        shutil.copytree(source, temporary, symlinks=True)
        for original in sorted(source.rglob("*")):
            details = original.lstat()
            copied = temporary / original.relative_to(source)
            if original.is_symlink():
                destination = original.resolve(strict=True)
                if not destination.is_relative_to(source):
                    raise ValueError("interpreter link points outside its retained tree")
                copied.unlink()
                # Relative links survive the atomic directory rename below.
                relative = os.path.relpath(
                    temporary / destination.relative_to(source), copied.parent
                )
                copied.symlink_to(relative)
            elif stat.S_ISREG(details.st_mode):
                if details.st_uid != os.getuid() or details.st_nlink != 1:
                    raise ValueError("interpreter ownership or link count differs")
                fd = os.open(copied, os.O_RDONLY | os.O_NOFOLLOW)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
            elif not stat.S_ISDIR(details.st_mode):
                raise ValueError("interpreter graph contains a special file")
        # The same strict graph walk used by release verification catches dangling
        # or escaped links after rebasing, before any public selection can reference it.
        if (
            environment_content_sha256(temporary, temporary) != source_digest
            or environment_content_sha256(source, source) != source_digest
        ):
            raise ValueError("interpreter changed during retention")
        directories = sorted(
            (item for item in temporary.rglob("*") if item.is_dir() and not item.is_symlink()),
            key=lambda item: len(item.parts),
            reverse=True,
        )
        for directory in [*directories, temporary]:
            fd = os.open(directory, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        os.rename(temporary, target)
        fd = os.open(target.parent, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def build_uuid(executable: Path) -> tuple[int, bytes]:
    """Read the UUID location from the supported thin arm64 Mach-O format."""
    with executable.open("rb") as binary:
        header = binary.read(32)
        if len(header) != 32:
            raise ValueError("truncated Mach-O header")
        magic, cpu, _subtype, _kind, commands, command_bytes, _flags, _reserved = struct.unpack(
            "<8I", header
        )
        if magic != 0xFEEDFACF or cpu != 0x0100000C:
            raise ValueError("Coire runtime requires a thin arm64 Mach-O executable")
        end = 32 + command_bytes
        if end > executable.stat().st_size:
            raise ValueError("truncated Mach-O load commands")
        offset = 32
        for _ in range(commands):
            binary.seek(offset)
            command_header = binary.read(8)
            if len(command_header) != 8 or offset + 8 > end:
                raise ValueError("truncated Mach-O load command")
            command, size = struct.unpack("<2I", command_header)
            if size < 8 or offset + size > end:
                raise ValueError("invalid Mach-O load command size")
            if command == 0x1B:  # LC_UUID
                if size != 24:
                    raise ValueError("invalid Mach-O build UUID")
                return offset + 8, binary.read(16)
            offset += size
        raise ValueError("Mach-O build UUID missing")


def runtime_uuid(original: bytes) -> bytes:
    return uuid.uuid5(uuid.NAMESPACE_URL, "com.coire.node.runtime:" + original.hex()).bytes


def isolate_build_uuid(executable: Path) -> None:
    """Separate the copied arm64 Mach-O identity before signing (Apple TN3178)."""
    offset, original = build_uuid(executable)
    with executable.open("r+b") as binary:
        binary.seek(offset)
        binary.write(runtime_uuid(original))


def network_python(source: Path) -> Path:
    """Give Coire a distinct executable without changing uv's shared interpreter."""
    source = source.resolve(strict=True)
    target = source.with_name("coire-node-python")
    temporary = target.with_name(f".coire-node-python.{os.getpid()}")
    identity = "com.coire.node.runtime"
    try:
        if not target.exists():
            shutil.copy2(source, temporary)
            isolate_build_uuid(temporary)
            subprocess.run(
                ["codesign", "--force", "--sign", "-", "--identifier", identity, str(temporary)],
                check=True,
                timeout=30,
            )
            candidate = temporary
        else:
            candidate = target
        subprocess.run(["codesign", "--verify", str(candidate)], check=True, timeout=30)
        signature = subprocess.run(
            ["codesign", "--display", "--verbose=4", str(candidate)],
            check=True,
            timeout=30,
            capture_output=True,
            text=True,
        )
        if f"Identifier={identity}" not in signature.stderr.splitlines():
            raise ValueError("unexpected Coire runtime signing identity")
        if build_uuid(candidate)[1] != runtime_uuid(build_uuid(source)[1]):
            raise ValueError("unexpected Coire runtime build UUID")
        if candidate == temporary:
            os.replace(temporary, target)
        return target
    finally:
        temporary.unlink(missing_ok=True)


def seal_tree(root: Path) -> None:
    """Seal owned runtime files; caches and mutable state belong outside this tree."""
    for path in sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        details = path.lstat()
        if path.is_symlink():
            continue
        if details.st_uid != os.getuid():
            raise ValueError("runtime ownership differs")
        if stat.S_ISDIR(details.st_mode):
            path.chmod(0o500)
        elif stat.S_ISREG(details.st_mode) and details.st_nlink == 1:
            path.chmod(0o400 | (stat.S_IMODE(details.st_mode) & 0o100))
        else:
            raise ValueError("runtime contains an uncontrolled file")
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    root.chmod(0o500)


def prepare_interpreter(
    source: Path,
    executable: Path,
    store: Path,
    *,
    prepare_binary: Callable[[Path], Path] | None = None,
) -> Path:
    """Retain a sealed interpreter and a private full-content verification receipt."""
    if source.is_symlink():
        raise ValueError("interpreter root is linked")
    source = source.resolve(strict=True)
    executable = executable.resolve(strict=True)
    if not executable.is_relative_to(source):
        raise ValueError("interpreter executable is outside its root")
    store.mkdir(mode=0o700, parents=True, exist_ok=True)
    details = store.lstat()
    if store.is_symlink() or details.st_uid != os.getuid() or details.st_mode & 0o077:
        raise ValueError("retained interpreter store is not private")
    store_fd = os.open(store, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        fcntl.flock(store_fd, fcntl.LOCK_EX)
        source_digest = environment_content_sha256(source, source)
        target = store / source_digest
        receipt = store / (source_digest + ".json")
        if target.exists() or receipt.exists():
            fd = os.open(receipt, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                info = os.fstat(fd)
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_nlink != 1
                    or info.st_uid != os.getuid()
                    or info.st_mode & 0o077
                    or info.st_size > 4096
                ):
                    raise ValueError("interpreter receipt ownership differs")
                with os.fdopen(fd, "rb", closefd=False) as receipt_reader:
                    payload = receipt_reader.read(4097)
                if len(payload) > 4096:
                    raise ValueError("interpreter receipt exceeds its bound")

                def unique_fields(pairs: list[tuple[str, object]]) -> dict[str, object]:
                    result: dict[str, object] = {}
                    for key, item in pairs:
                        if key in result:
                            raise ValueError("interpreter receipt contains duplicate fields")
                        result[key] = item
                    return result

                value = json.loads(payload, object_pairs_hook=unique_fields)
                after = os.fstat(fd)
                observed = receipt.lstat()
                if (info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns) != (
                    after.st_ino,
                    after.st_size,
                    after.st_mtime_ns,
                    after.st_ctime_ns,
                ) or (observed.st_dev, observed.st_ino) != (info.st_dev, info.st_ino):
                    raise ValueError("interpreter receipt changed during verification")
            finally:
                os.close(fd)
            if not isinstance(value, dict) or set(value) != {
                "source_sha256",
                "content_sha256",
                "executable",
            }:
                raise ValueError("interpreter receipt differs")
            if (
                value["source_sha256"] != source_digest
                or environment_content_sha256(target, target) != value["content_sha256"]
            ):
                raise ValueError("retained interpreter content differs")
            executable_key = value["executable"]
            if (
                not isinstance(executable_key, str)
                or len(executable_key) > 512
                or Path(executable_key).is_absolute()
            ):
                raise ValueError("interpreter receipt executable differs")
            selected = target / executable_key
            if not selected.resolve(strict=True).is_relative_to(target.resolve(strict=True)):
                raise ValueError("retained interpreter executable escapes its root")
            return selected
        staged = store / (".prepare-" + uuid.uuid4().hex)
        try:
            retain_interpreter(source, staged)
            copied = staged / executable.relative_to(source)
            selected = prepare_binary(copied) if prepare_binary is not None else copied
            if not selected.resolve(strict=True).is_relative_to(staged):
                raise ValueError("prepared interpreter executable escapes its root")
            relative = selected.relative_to(staged)
            seal_tree(staged)
            content = environment_content_sha256(staged, staged)
            os.rename(staged, target)
            receipt_temp = store / (".receipt-" + uuid.uuid4().hex)
            try:
                fd = os.open(
                    receipt_temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
                )
                with os.fdopen(fd, "w") as handle:
                    json.dump(
                        {
                            "source_sha256": source_digest,
                            "content_sha256": content,
                            "executable": relative.as_posix(),
                        },
                        handle,
                        sort_keys=True,
                    )
                    handle.flush()
                    os.fsync(handle.fileno())
                os.rename(receipt_temp, receipt)
                os.fsync(store_fd)
            finally:
                receipt_temp.unlink(missing_ok=True)
            return target / relative
        finally:
            if staged.exists():
                for directory in staged.rglob("*"):
                    if directory.is_dir() and not directory.is_symlink():
                        directory.chmod(0o700)
                staged.chmod(0o700)
                shutil.rmtree(staged)
    finally:
        os.close(store_fd)


def smoke(python: Path) -> None:
    if (
        platform.system() != "Darwin"
        or platform.machine() != "arm64"
        or (platform.node().lower().split(".", 1)[0] not in {"coire-edge-a", "coire-edge-b"})
    ):
        raise ValueError("Native import smoke requires a registered Apple Silicon Studio")
    offline = {
        key: value
        for key, value in os.environ.items()
        if key in {"PATH", "LANG", "LC_ALL", "TMPDIR", "HOME", "HF_HOME", "XDG_CACHE_HOME"}
    }
    offline.update(
        HF_HUB_OFFLINE="1",
        TRANSFORMERS_OFFLINE="1",
        WANDB_MODE="disabled",
        PYTHONDONTWRITEBYTECODE="1",
        PYTHONNOUSERSITE="1",
    )
    subprocess.run(
        [
            str(python),
            "-I",
            "-B",
            "-c",
            (
                "import coire_core, coire_node, mlx_lm, mlx_vlm, mflux; "
                "import coire_node.upgrade_guard, coire_node.expired_recovery_support, "
                "coire_node.expired_recovery_successor_session"
            ),
        ],
        check=True,
        timeout=30,
        stdout=subprocess.DEVNULL,
        env=offline,
    )
    for module in ("mlx_lm.server", "mlx_vlm.server"):
        subprocess.run(
            [str(python), "-I", "-B", "-m", module, "--help"],
            check=True,
            timeout=30,
            stdout=subprocess.DEVNULL,
            env=offline,
        )
    # Import the pinned native entry point without loading weights or contacting the Hub.
    subprocess.run(
        [str(python), "-I", "-B", "-c", "from mflux.models.z_image.variants.z_image import ZImage"],
        check=True,
        timeout=30,
        stdout=subprocess.DEVNULL,
        env=offline,
    )
    # Validate the exact bare API hooks without loading any model/tokenizer assets.
    subprocess.run(
        [
            str(python),
            "-I",
            "-B",
            "-c",
            (
                "import inspect; from importlib.metadata import version; "
                "from mlx_lm.tuner.trainer import train, evaluate; "
                "from mlx_lm.tuner.utils import linear_to_lora_layers; "
                "import yaml, safetensors; "
                "import coire_node.training.worker, coire_node.training.analysis_worker; "
                "assert version('mlx') == '0.32.2'; "
                "assert version('mlx-lm') == '0.31.3'; "
                "assert {'model', 'optimizer', 'train_dataset', 'loss', 'training_callback'} "
                "<= set(inspect.signature(train).parameters); "
                "assert callable(evaluate) and callable(linear_to_lora_layers)"
            ),
        ],
        check=True,
        timeout=30,
        stdout=subprocess.DEVNULL,
        env=offline,
    )


def stage_environment(
    source: Path,
    target: Path,
    *,
    verify: Callable[[Path], None] = smoke,
    expected_python: Path | None = None,
) -> None:
    """Verify/install an immutable candidate without selecting or starting it."""
    if not source.is_dir() or not (source / "bin/python3").is_file():
        raise ValueError("node environment is incomplete")
    if expected_python is not None and (
        not expected_python.is_file()
        or (source / "bin/python3").resolve(strict=True) != expected_python.resolve(strict=True)
    ):
        raise ValueError("node environment interpreter identity differs from the selected runtime")
    verify(source / "bin/python3")
    if source != target:
        if target.exists():
            raise FileExistsError("immutable node environment already exists")
        os.replace(source, target)
        directory = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


def publish_environment(
    source: Path,
    target: Path,
    current: Path,
    *,
    verify: Callable[[Path], None] = smoke,
    expected_python: Path | None = None,
) -> None:
    """Keep the prior link on any smoke failure; never mutate an active environment."""
    stage_environment(source, target, verify=verify, expected_python=expected_python)
    temporary = current.with_name(f".current.{os.getpid()}")
    try:
        os.symlink(target, temporary)
        os.replace(temporary, current)
    finally:
        temporary.unlink(missing_ok=True)


def stage_sealed_environment(
    source: Path,
    target: Path,
    expected_python: Path,
    *,
    verify: Callable[[Path], None] = smoke,
) -> PreparedNativeRuntime:
    """Import-smoke, seal and record actual installed content without selecting it."""
    from coire_core.models.upgrades import PreparedNativeRuntime
    from coire_core.upgrade_journal import PrivateUpgradeStore

    interpreter = expected_python.resolve(strict=True).parent.parent
    identity_store = PrivateUpgradeStore(target.parent / ".identities")
    key = target.name + ".json"
    with identity_store.ownership("prepare.lock"):
        existing = identity_store.read(key, PreparedNativeRuntime)
        if source == target:
            if existing is None:
                raise ValueError("sealed environment content receipt is absent")
            if (
                existing.environment_id != target.name
                or existing.interpreter_id != interpreter.name
                or existing.environment_sha256 != environment_content_sha256(target, interpreter)
                or existing.interpreter_sha256
                != environment_content_sha256(interpreter, interpreter)
                or (target / "bin/python3").resolve(strict=True)
                != expected_python.resolve(strict=True)
            ):
                raise ValueError("sealed environment content differs")
            verify(target / "bin/python3")
            if existing.environment_sha256 != environment_content_sha256(target, interpreter):
                raise ValueError("sealed environment content changed during verification")
            return existing
        if existing is not None or target.exists() or target.is_symlink():
            raise FileExistsError("sealed environment already exists")

        def verify_and_seal(python: Path) -> None:
            verify(python)
            environment_content_sha256(source, interpreter)
            seal_tree(source)

        stage_environment(source, target, verify=verify_and_seal, expected_python=expected_python)
        result = PreparedNativeRuntime(
            environment_id=target.name,
            environment_sha256=environment_content_sha256(target, interpreter),
            interpreter_id=interpreter.name,
            interpreter_sha256=environment_content_sha256(interpreter, interpreter),
        )
        identity_store.write(key, result, replace=False)
        return result


def verify_prepared_runtime(
    environment_root: Path, interpreter_root: Path, expected: PreparedNativeRuntime
) -> Path:
    """Prove the retained receipt, interpreter and every environment byte before selection."""
    from coire_core.errors import UpgradeUnavailable
    from coire_core.models.upgrades import PreparedNativeRuntime
    from coire_core.upgrade_journal import PrivateUpgradeStore

    try:
        expected = PreparedNativeRuntime.model_validate(expected.model_dump(mode="json"))
        for root in (environment_root, interpreter_root):
            details = root.lstat()
            if (
                not stat.S_ISDIR(details.st_mode)
                or details.st_uid != os.getuid()
                or details.st_mode & 0o022
            ):
                raise ValueError("retained runtime root differs")
        environment = environment_root / expected.environment_id
        interpreter = interpreter_root / expected.interpreter_id
        for root in (environment, interpreter):
            details = root.lstat()
            if (
                not stat.S_ISDIR(details.st_mode)
                or details.st_uid != os.getuid()
                or details.st_mode & 0o277
            ):
                raise ValueError("retained runtime is not sealed")
        identities = environment_root / ".identities"
        if not identities.is_dir() or identities.is_symlink():
            raise ValueError("retained environment receipt is absent")
        receipt = PrivateUpgradeStore(identities).read(
            expected.environment_id + ".json", PreparedNativeRuntime
        )
        if receipt != expected:
            raise ValueError("retained environment receipt differs")
        python = environment / "bin/python3"
        resolved = python.resolve(strict=True)
        if (
            not resolved.is_relative_to(interpreter.resolve(strict=True))
            or not resolved.stat().st_mode & 0o100
        ):
            raise ValueError("retained runtime interpreter differs")
        if (
            environment_content_sha256(environment, interpreter) != expected.environment_sha256
            or environment_content_sha256(interpreter, interpreter) != expected.interpreter_sha256
        ):
            raise ValueError("retained runtime content differs")
        return python
    except (OSError, ValueError):
        raise UpgradeUnavailable("Retained native runtime verification failed") from None


def reconcile_native_selection(
    environment_root: Path,
    interpreter_root: Path,
    recovery_root: Path,
    node: StudioName,
    *,
    before_replace: Callable[[], None] | None = None,
) -> NativeRuntimeSelection:
    """Repair only the pointer from an independently retained, fully verified selection.

    This does not signal processes or release maintenance. The recovery owner must prove
    process/drain authority separately before changing the running node generation.
    """
    from coire_core.errors import UpgradeUnavailable
    from coire_core.models.upgrades import NativeRuntimeSelection
    from coire_core.upgrade_journal import PrivateUpgradeStore

    store = PrivateUpgradeStore(recovery_root)
    with store.ownership("selection.lock"):
        selected = store.read("selection.json", NativeRuntimeSelection)
        if selected is None or selected.node != node:
            raise UpgradeUnavailable("Independent native source selection is unavailable")
        python = verify_prepared_runtime(environment_root, interpreter_root, selected.runtime)
        destination = python.parent.parent
        current = environment_root / "current"
        try:
            existing = current.lstat()
        except FileNotFoundError:
            existing = None
        if existing is not None:
            if (
                not stat.S_ISLNK(existing.st_mode)
                or existing.st_uid != os.getuid()
                or existing.st_nlink != 1
            ):
                raise UpgradeUnavailable("Native selection pointer is not owned")
            target = current.resolve(strict=False)
            if target.parent != environment_root.resolve(strict=True):
                raise UpgradeUnavailable("Native selection pointer escapes retained environments")
            if target == destination.resolve(strict=True):
                return selected
        if before_replace is not None:
            before_replace()
        if store.read("selection.json", NativeRuntimeSelection) != selected:
            raise UpgradeUnavailable("Native selection authority changed during verification")
        pending = environment_root / (".current-" + uuid.uuid4().hex)
        try:
            pending.symlink_to(destination)
            # Recheck the exact prior pointer before replacing it, including absence.
            try:
                latest = current.lstat()
            except FileNotFoundError:
                latest = None
            if (latest is None) != (existing is None) or (
                latest is not None
                and existing is not None
                and (latest.st_dev, latest.st_ino, latest.st_ctime_ns)
                != (existing.st_dev, existing.st_ino, existing.st_ctime_ns)
            ):
                raise UpgradeUnavailable("Native selection pointer changed during reconciliation")
            os.replace(pending, current)
            fd = os.open(environment_root, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError:
            raise UpgradeUnavailable("Native source pointer reconciliation failed") from None
        finally:
            pending.unlink(missing_ok=True)
        return selected


def main() -> None:
    if len(sys.argv) in {5, 6} and sys.argv[1] == "--seal-stage":
        candidate = Path(sys.argv[3])
        staged_identity = stage_sealed_environment(Path(sys.argv[2]), candidate, Path(sys.argv[4]))
        if len(sys.argv) == 6:
            selected_link = Path(sys.argv[5])
            pending_link = selected_link.with_name(".current-" + uuid.uuid4().hex)
            try:
                pending_link.symlink_to(candidate)
                os.replace(pending_link, selected_link)
                parent_fd = os.open(selected_link.parent, os.O_RDONLY | os.O_NOFOLLOW)
                try:
                    os.fsync(parent_fd)
                finally:
                    os.close(parent_fd)
            finally:
                pending_link.unlink(missing_ok=True)
        print(staged_identity.environment_sha256)
        raise SystemExit(0)
    if len(sys.argv) == 4 and sys.argv[1] == "--prepare-python":
        original = Path(sys.argv[2]).resolve(strict=True)
        base = subprocess.run(
            [str(original), "-I", "-c", "import sys; print(sys.base_prefix)"],
            check=True,
            timeout=30,
            capture_output=True,
            text=True,
        )
        print(
            prepare_interpreter(
                Path(base.stdout.strip()),
                original,
                Path(sys.argv[3]),
                prepare_binary=network_python if sys.platform == "darwin" else None,
            )
        )
        raise SystemExit(0)
    if len(sys.argv) >= 4 and sys.argv[1] == "--runtime-input-id":
        root = Path(sys.argv[2])
        print(
            hashlib.sha256(
                json.dumps(
                    {
                        "inputs_sha256": artifact_graph_sha256(
                            [Path(value) for value in sys.argv[3:]]
                        ),
                        "interpreter_sha256": environment_content_sha256(root, root),
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
        )
        raise SystemExit(0)
    if len(sys.argv) >= 3 and sys.argv[1] == "--content-id":
        print(artifact_graph_sha256([Path(value) for value in sys.argv[2:]]))
        raise SystemExit(0)
    if len(sys.argv) in {4, 5} and sys.argv[1] == "--stage":
        stage_environment(
            Path(sys.argv[2]),
            Path(sys.argv[3]),
            expected_python=Path(sys.argv[4]) if len(sys.argv) == 5 else None,
        )
        raise SystemExit(0)
    if len(sys.argv) == 3 and sys.argv[1] == "--network-python":
        print(network_python(Path(sys.argv[2])))
        raise SystemExit(0)
    if len(sys.argv) not in {4, 5}:
        raise SystemExit("usage: install_runtime.py SOURCE TARGET CURRENT [EXPECTED_PYTHON]")
    publish_environment(
        *(Path(value) for value in sys.argv[1:4]),
        expected_python=Path(sys.argv[4]) if len(sys.argv) == 5 else None,
    )


def managed_worker_environment(settings: Settings) -> dict[str, str]:
    """Carry only runtime identity configuration into isolated node workers."""
    return {
        "UPGRADES_ENABLED": "true" if settings.upgrades_enabled else "false",
        "NODE_NAME": settings.node_name,
        "UPGRADE_ENVIRONMENT_ROOT": str(settings.upgrade_environment_root),
    }


def running_runtime_sha256(settings: Settings) -> str | None:
    """Identify this interpreter's sealed installed graph, never a version string."""
    from coire_core.errors import UpgradeUnavailable
    from coire_core.models.upgrades import PreparedNativeRuntime
    from coire_core.upgrade_journal import PrivateUpgradeStore

    if not settings.upgrades_enabled:
        return None
    if settings.node_name not in {"coire-edge-a", "coire-edge-b"}:
        raise UpgradeUnavailable("Native runtime identity requires a Studio")
    envs = Path(settings.upgrade_environment_root)
    environment = Path(sys.prefix).resolve(strict=True)
    if environment.parent != envs.resolve(strict=True):
        raise UpgradeUnavailable("Running interpreter escapes retained environments")
    identities = envs / ".identities"
    if not identities.exists():
        raise UpgradeUnavailable("Running runtime has no sealed content receipt")
    runtime = PrivateUpgradeStore(identities).read(
        environment.name + ".json", PreparedNativeRuntime
    )
    if runtime is None or runtime.environment_id != environment.name:
        raise UpgradeUnavailable("Running runtime content receipt differs")
    executable = verify_prepared_runtime(envs, envs.parent / "runtimes", runtime)
    if Path(sys.executable).resolve(strict=True) != executable.resolve(strict=True):
        raise UpgradeUnavailable("Running interpreter differs from sealed runtime")
    return runtime.environment_sha256
