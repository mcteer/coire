"""Host-side preparation of complete, immutable credential generations."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import ExitStack, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast
from uuid import uuid4

from coire_core.errors import CoireError, UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrades import (
    DeploymentLease,
    DeploymentOwnership,
    DeploymentSelectionReceipt,
)
from coire_core.upgrade_journal import PrivateUpgradeStore

REQUIRED_SECRET_FILES = (
    "postgres_password",
    "key_signing_secret",
    "node_tokens",
    "admin_token",
    "bootstrap_admin_email",
)
OPTIONAL_SECRET_FILES = (
    "upgrade_executor_token",
    "ops_service_token",
    "file_worker_service_token",
    "failover_peer_key",
    "failover_relay_token",
    "openai_api_key",
    "anthropic_api_key",
)
KEYCHAIN_ITEMS = {
    "upgrade_executor_token": "coire-upgrade-executor-token",
    "postgres_password": "coire-postgres-password",
    "key_signing_secret": "coire-key-signing-secret",
    "node_tokens": "coire-node-tokens",
    "admin_token": "coire-admin-token",
    "bootstrap_admin_email": "coire-bootstrap-admin-email",
    "ops_service_token": "coire-ops-service-token",
    "file_worker_service_token": "coire-file-worker-service-token",
    "failover_peer_key": "coire-failover-peer-key",
    "openai_api_key": "coire-openai-api-key",
    "anthropic_api_key": "coire-anthropic-api-key",
}


class ReleaseInputError(ValueError):
    """A release input is incomplete or unsafe to use."""


@contextmanager
def deployment_mutex(
    store: PrivateUpgradeStore,
    project: str,
    owner_identity: str,
    *,
    reconciled_prior: DeploymentOwnership | None = None,
    operation: tuple[str, int] | None = None,
    handover: tuple[str, int] | None = None,
) -> Iterator[int]:
    """Atomic shared ownership plus a namespace-local kernel lock.

    Abandoned foreign-kernel claims require recovery in their original namespace.
    """
    with ExitStack() as kernel:
        claimed: DeploymentLease | None = None
        previous: DeploymentOwnership | None = None
        try:
            with store.ownership("deployment-admission.lock"):
                descriptor = kernel.enter_context(store.ownership("deployment.lock"))
                previous = store.read("ownership.json", DeploymentOwnership)
                lease = store.read("deployment.lease", DeploymentLease)
                if lease is not None:
                    if (
                        lease.project != project
                        or lease.kernel_platform != sys.platform
                        or previous is None
                        or previous.project != project
                        or previous.owner_identity != lease.owner_identity
                        or (previous.state != "committed" and previous != reconciled_prior)
                    ):
                        raise UpgradeConflict("Shared deployment claim requires namespace recovery")
                    store.path("deployment.lease").unlink()
                    store.sync()
                claimed = DeploymentLease(
                    project=project,
                    owner_identity=owner_identity,
                    kernel_platform=cast(Literal["darwin", "linux"], sys.platform),
                    kernel_boot_identity=host_boot_identity(),
                    created_at=datetime.now(UTC),
                )
                store.write("deployment.lease", claimed, replace=False)
                from coire_core.models.upgrades import (
                    DeploymentHandoverHold,
                    DeploymentOperationHold,
                )

                handover_hold = store.read("handover-hold.json", DeploymentHandoverHold)
                if handover_hold is not None and (
                    handover_hold.project != project
                    or (
                        not handover_hold.released
                        and (handover_hold.preparation_sha256, handover_hold.generation) != handover
                    )
                ):
                    raise UpgradeConflict("Executor handover retains host deployment ownership")

                hold = store.read("operation-hold.json", DeploymentOperationHold)
                if hold is not None and (
                    hold.project != project
                    or (not hold.released and (hold.job_id, hold.generation) != operation)
                ):
                    raise UpgradeConflict("Managed maintenance retains host deployment ownership")
            yield descriptor
        finally:
            if claimed is not None:
                latest = store.read("ownership.json", DeploymentOwnership)
                current = store.read("deployment.lease", DeploymentLease)
                if current == claimed and (
                    latest == previous
                    or (
                        latest is not None
                        and latest.owner_identity == owner_identity
                        and latest.state == "committed"
                    )
                ):
                    store.path("deployment.lease").unlink()
                    store.sync()


def idle_executor_owner(
    store: PrivateUpgradeStore, project: str, executor_id: str
) -> DeploymentOwnership | None:
    """Witness an effect-free writer in this kernel without taking or releasing it.

    Callers publishing admission intent hold deployment-admission.lock; kernel
    acquisition uses that same barrier so host effects cannot cross publication.
    This witness grants no runtime effect, bootstrap readiness or foreign-kernel recovery.
    """
    owner = store.read("ownership.json", DeploymentOwnership)
    if (
        owner is None
        or owner.state != "active"
        or owner.owner_kind != "executor"
        or owner.job_id is not None
    ):
        return None
    lease = store.read("deployment.lease", DeploymentLease)
    boot = host_boot_identity()
    if (
        owner.project != project
        or owner.owner_identity != executor_id
        or owner.stage != "preparing"
        or owner.executor_runtime_id is None
        or owner.executor_container_id is None
        or owner.boot_identity != boot
        or lease is None
        or (lease.project, lease.owner_identity, lease.kernel_platform, lease.kernel_boot_identity)
        != (project, executor_id, sys.platform, boot)
    ):
        raise UpgradeConflict("Idle executor witness has another namespace or authority")
    path = store.path("deployment.lock")
    try:
        descriptor = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
        try:
            held, expected = os.fstat(descriptor), path.lstat()
            if (
                not stat.S_ISREG(held.st_mode)
                or held.st_uid != os.getuid()
                or held.st_mode & 0o077
                or held.st_nlink != 1
                or (held.st_dev, held.st_ino) != (expected.st_dev, expected.st_ino)
            ):
                raise UpgradeUnavailable("Idle executor kernel lock differs")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                pass
            else:
                raise UpgradeUnavailable("Idle executor retains no kernel writer")
            if (
                store.read("ownership.json", DeploymentOwnership) != owner
                or store.read("deployment.lease", DeploymentLease) != lease
            ):
                raise UpgradeConflict("Idle executor changed during admission observation")
        finally:
            os.close(descriptor)
    except OSError:
        raise UpgradeUnavailable("Idle executor kernel witness is unavailable") from None
    return owner


@contextmanager
def deployment_ownership(
    coordination_root: Path,
    claim: DeploymentOwnership,
    *,
    reconciled_prior: DeploymentOwnership | None = None,
) -> Iterator[PrivateUpgradeStore]:
    """Cross-namespace file ownership; abandoned intent needs explicit reconciliation.

    Callers prove the prior runtime/journal state before supplying reconciled_prior.
    PID liveness and expiry never authorize taking an abandoned active operation.
    """
    store = PrivateUpgradeStore(coordination_root)
    with deployment_mutex(
        store, claim.project, claim.owner_identity, reconciled_prior=reconciled_prior
    ):
        previous = store.read("ownership.json", DeploymentOwnership)
        if previous is not None:
            if previous.project != claim.project or claim.generation <= previous.generation:
                raise UpgradeConflict("Deployment owner project or generation differs")
            if previous.state == "active" and previous != reconciled_prior:
                raise UpgradeConflict("Prior deployment intent requires runtime reconciliation")
        elif reconciled_prior is not None:
            raise UpgradeConflict("Reconciled ownership witness is absent")
        if claim.state != "active":
            raise UpgradeConflict("New deployment ownership must begin active")
        store.write("ownership.json", claim)
        yield store
        latest = store.read("ownership.json", DeploymentOwnership)
        if (
            latest is None
            or latest.owner_identity != claim.owner_identity
            or latest.generation != claim.generation
            or latest.project != claim.project
            or latest.owner_kind != claim.owner_kind
            or latest.boot_identity != claim.boot_identity
            or latest.job_id != claim.job_id
            or latest.operation_generation != claim.operation_generation
        ):
            raise UpgradeConflict("Deployment ownership changed during execution")
        store.write("ownership.json", latest.model_copy(update={"state": "committed"}))


def host_boot_identity() -> str:
    if sys.platform == "darwin":
        identity = subprocess.run(
            ["sysctl", "-n", "kern.bootsessionuuid"],
            check=True,
            timeout=5,
            capture_output=True,
            text=True,
        ).stdout.strip()
    elif sys.platform == "linux":
        identity = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    else:
        raise ReleaseInputError("host boot identity is unavailable")
    if re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}", identity) is None:
        raise ReleaseInputError("host boot identity differs")
    return identity


def verify_host_deployment(
    state_root: Path, project: str
) -> tuple[PrivateUpgradeStore, DeploymentOwnership]:
    """Validate an inherited kernel ownership descriptor; a PID or marker is insufficient."""
    store = PrivateUpgradeStore(
        state_root / validate_project_name(project) / "upgrades/coordination"
    )
    try:
        descriptor = int(os.environ.get("COIRE_DEPLOY_OWNER_FD", ""))
        if descriptor < 3:
            raise ValueError("invalid descriptor")
        held = os.fstat(descriptor)
        expected = store.path("deployment.lock").lstat()
        if not stat.S_ISREG(expected.st_mode) or (held.st_dev, held.st_ino) != (
            expected.st_dev,
            expected.st_ino,
        ):
            raise ValueError("ownership descriptor differs")
        probe = os.open(store.path("deployment.lock"), os.O_RDWR | os.O_NOFOLLOW)
        try:
            try:
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                pass
            else:
                raise ValueError("ownership descriptor is not locked")
        finally:
            os.close(probe)
    except (OSError, ValueError):
        raise ReleaseInputError("host deployment requires inherited kernel ownership") from None
    claim = store.read("ownership.json", DeploymentOwnership)
    if (
        claim is None
        or claim.state != "active"
        or claim.owner_kind != "host"
        or claim.project != project
        or claim.boot_identity != host_boot_identity()
        or claim.owner_identity != os.environ.get("COIRE_DEPLOY_OWNER_ID")
    ):
        raise ReleaseInputError("host deployment ownership witness differs")
    lease = store.read("deployment.lease", DeploymentLease)
    if (
        lease is None
        or lease.owner_identity != claim.owner_identity
        or lease.project != project
        or lease.kernel_platform != sys.platform
        or lease.kernel_boot_identity != host_boot_identity()
    ):
        raise ReleaseInputError("shared deployment ownership witness differs")
    return store, claim


def mark_host_mutation(state_root: Path, project: str) -> None:
    store, claim = verify_host_deployment(state_root, project)
    store.write("ownership.json", claim.model_copy(update={"stage": "mutating"}))


def owned_release_sha256(state_root: Path, project: str, key: str) -> str:
    if re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}", key) is None:
        raise ReleaseInputError("retained release key differs")
    root = state_root / validate_project_name(project) / "releases"
    release = root / key
    if root.is_symlink() or release.is_symlink() or not release.is_dir():
        raise ReleaseInputError("retained release is unavailable")
    try:
        fd = os.open(release / "compose.json", os.O_RDONLY | os.O_NOFOLLOW)
        try:
            before = os.fstat(fd)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_uid != os.getuid()
                or before.st_nlink != 1
                or before.st_size > 1024 * 1024
            ):
                raise ReleaseInputError("retained release content is unsafe")
            digest = hashlib.sha256()
            read_bytes = 0
            with os.fdopen(fd, "rb", closefd=False) as handle:
                while block := handle.read(64 * 1024):
                    read_bytes += len(block)
                    if read_bytes > before.st_size:
                        raise ReleaseInputError("retained release grew during verification")
                    digest.update(block)
            after = os.fstat(fd)
            if (before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            ) or (release / "compose.json").lstat().st_ino != before.st_ino:
                raise ReleaseInputError("retained release changed during verification")
            return digest.hexdigest()
        finally:
            os.close(fd)
    except OSError:
        raise ReleaseInputError("retained release content is unavailable") from None


def _select_owned_pointer(state_root: Path, project: str, key: str) -> None:
    project_root = state_root / validate_project_name(project)
    temporary = project_root / (".current-" + uuid4().hex)
    try:
        temporary.symlink_to((project_root / "releases" / key).resolve(strict=True))
        os.replace(temporary, project_root / "current")
        fd = os.open(project_root, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        temporary.unlink(missing_ok=True)


def reconcile_release_selection(
    state_root: Path, project: str, store: PrivateUpgradeStore
) -> DeploymentSelectionReceipt | None:
    """Caller holds deployment ownership; verify retained receipts before pointer repair."""
    receipt = store.read("selection.json", DeploymentSelectionReceipt)
    if receipt is None:
        return None
    if receipt.project != project:
        raise ReleaseInputError("selected release belongs to another project")
    for key, digest in receipt.protected_compose_sha256s.items():
        if owned_release_sha256(state_root, project, key) != digest:
            raise ReleaseInputError("retained release content differs from selection receipt")
    current = state_root / project / "current"
    chosen = state_root / project / "releases" / receipt.release_key
    if not current.is_symlink() or current.resolve() != chosen.resolve():
        _select_owned_pointer(state_root, project, receipt.release_key)
    return receipt


def record_host_selection(state_root: Path, project: str, release: Path) -> None:
    store, owner = verify_host_deployment(state_root, project)
    if owner.stage != "mutating":
        raise ReleaseInputError("selection requires a verified owned runtime mutation")
    releases = state_root / project / "releases"
    chosen = release.resolve(strict=True)
    if release.is_symlink() or chosen.parent != releases.resolve(strict=True):
        raise ReleaseInputError("selected release is not project-owned")
    previous = store.read("selection.json", DeploymentSelectionReceipt)
    protected = {} if previous is None else dict(previous.protected_compose_sha256s)
    current = state_root / project / "current"
    if current.is_symlink():
        prior = current.resolve(strict=True)
        if prior.parent != releases.resolve(strict=True):
            raise ReleaseInputError("source selection belongs to another project")
        protected.setdefault(prior.name, owned_release_sha256(state_root, project, prior.name))
    chosen_digest = owned_release_sha256(state_root, project, chosen.name)
    if chosen.name in protected and protected[chosen.name] != chosen_digest:
        raise ReleaseInputError("retained selected release content changed")
    protected[chosen.name] = chosen_digest
    receipt = DeploymentSelectionReceipt(
        project=project,
        owner_identity=owner.owner_identity,
        boot_identity=owner.boot_identity,
        generation=owner.generation,
        release_key=chosen.name,
        compose_sha256=protected[chosen.name],
        protected_release_keys=tuple(sorted(protected)),
        protected_compose_sha256s=protected,
        selected_at=datetime.now(UTC),
    )
    # Runtime health has already been checked by the fixed up workflow. Persist
    # its selection before updating the convenience host link, so restart can repair it.
    store.write("selection.json", receipt)
    _select_owned_pointer(state_root, project, chosen.name)
    store.write("ownership.json", owner.model_copy(update={"stage": "selected"}))


def run_host_deployment(
    action: Literal["up", "down"],
    arguments: tuple[str, ...],
    *,
    source_root: Path,
    state_root: Path,
    project: str,
) -> int:
    """Run a fixed host entrypoint under the same FD lock used by the executor."""
    if action not in {"up", "down"}:
        raise ReleaseInputError("unsupported host deployment action")
    project = validate_project_name(project)
    allowed = (
        {"--secrets-from-env", "--build", "--no-build", "--recover-db-role", "-h", "--help"}
        if action == "up"
        else {"--purge"}
    )
    if any(argument not in allowed for argument in arguments):
        raise ReleaseInputError("unsupported host deployment argument")
    project_root = state_root / project
    if (project_root / ".coire-up.lock").exists():
        raise ReleaseInputError("legacy deployment ownership requires explicit reconciliation")
    store = PrivateUpgradeStore(project_root / "upgrades/coordination")
    owner_identity = f"host-{action}-{uuid4().hex}"
    with deployment_mutex(store, project, owner_identity) as descriptor:
        previous = store.read("ownership.json", DeploymentOwnership)
        if previous is not None and (previous.state == "active" or previous.project != project):
            raise UpgradeConflict("Prior deployment intent requires runtime reconciliation")
        reconcile_release_selection(state_root, project, store)
        claim = DeploymentOwnership(
            project=project,
            owner_kind="host",
            owner_identity=owner_identity,
            boot_identity=host_boot_identity(),
            generation=1 if previous is None else previous.generation + 1,
            stage="preparing",
        )
        store.write("ownership.json", claim)
        script = source_root.resolve() / "deploy/compose" / ("coire-" + action)
        environment = {
            **os.environ,
            "COIRE_DEPLOY_OWNER_FD": str(descriptor),
            "COIRE_DEPLOY_OWNER_ID": claim.owner_identity,
            "COIRE_STATE_ROOT": str(state_root),
            "COMPOSE_PROJECT_NAME": project,
            "COIRE_REPO_ROOT": str(source_root.resolve()),
        }
        result = subprocess.run(
            ["bash", str(script), *arguments], env=environment, pass_fds=(descriptor,), check=False
        )
        latest = store.read("ownership.json", DeploymentOwnership)
        if (
            latest is None
            or latest.owner_identity != claim.owner_identity
            or latest.generation != claim.generation
        ):
            raise UpgradeConflict("Host deployment ownership changed during execution")
        if result.returncode == 0 or latest.stage == "preparing":
            # Before the first marked runtime effect, a failed preparation has no
            # external state to reconcile. A failed mutation stays active.
            store.write("ownership.json", latest.model_copy(update={"state": "committed"}))
        return result.returncode


def validate_project_name(value: str) -> str:
    """Constrain project names before using one as a state-directory component."""
    if not re.fullmatch(r"[a-z][a-z0-9_-]{1,62}", value):
        raise ReleaseInputError("invalid Compose project name")
    return value


def _credential_generation(path: Path, owned_root: Path) -> Path:
    if not path.is_absolute() or path.is_symlink() or path.parent.is_symlink():
        raise ReleaseInputError("retained credential reference is unsafe")
    generation = path.parent.resolve()
    if (
        generation.parent != owned_root.resolve()
        or not generation.name.startswith("generation-")
        or path.name not in (*REQUIRED_SECRET_FILES, *OPTIONAL_SECRET_FILES)
    ):
        raise ReleaseInputError("retained credential reference differs from project")
    return generation


def protected_secret_generations(state_root: Path, base: Path, project: str) -> frozenset[Path]:
    """Every retained source/candidate manifest protects its immutable credentials."""
    project = validate_project_name(project)
    releases = state_root / project / "releases"
    current = state_root / project / "current"
    owned_root = base / project / "secrets"
    protected: set[Path] = set()
    if releases.is_symlink():
        raise ReleaseInputError("retained release root is linked")
    if current.exists() or current.is_symlink():
        if not current.is_symlink():
            raise ReleaseInputError("selected release reference is uncertain")
        try:
            if current.resolve(strict=True).parent != releases.resolve(strict=True):
                raise ReleaseInputError("selected release belongs to another project")
        except OSError:
            raise ReleaseInputError("selected release is unavailable") from None
    if not releases.exists():
        return frozenset()
    paths = list(releases.iterdir())
    if len(paths) > 512:
        raise ReleaseInputError("retained release collection exceeds its bound")
    for release in paths:
        if release.is_symlink() or not release.is_dir():
            raise ReleaseInputError("retained release collection is uncertain")
        try:
            fd = os.open(release / "compose.json", os.O_RDONLY | os.O_NOFOLLOW)
            try:
                info = os.fstat(fd)
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_uid != os.getuid()
                    or info.st_nlink != 1
                    or info.st_size > 1024 * 1024
                ):
                    raise ReleaseInputError("retained release manifest is unsafe")
                with os.fdopen(fd, "rb", closefd=False) as handle:
                    data = handle.read(1024 * 1024 + 1)
                if len(data) > 1024 * 1024:
                    raise ReleaseInputError("retained release manifest exceeds its bound")
                document = json.loads(data)
            finally:
                os.close(fd)
            if (
                not isinstance(document, dict)
                or document.get("name", project) != project
                or not isinstance(document.get("secrets", {}), dict)
            ):
                raise ReleaseInputError("retained release manifest differs")
            for name, binding in document.get("secrets", {}).items():
                if (
                    name not in (*REQUIRED_SECRET_FILES, *OPTIONAL_SECRET_FILES)
                    or not isinstance(binding, dict)
                    or not isinstance(binding.get("file"), str)
                ):
                    raise ReleaseInputError("retained release credential binding is uncertain")
                protected.add(_credential_generation(Path(binding["file"]), owned_root))
        except (OSError, ValueError):
            raise ReleaseInputError("retained release credentials cannot be verified") from None
    return frozenset(protected)


def mounted_secret_generations(base: Path, project: str) -> frozenset[Path]:
    """Also retain credential files pinned by actual stopped or running containers."""
    project = validate_project_name(project)
    listing = subprocess.run(
        ["docker", "ps", "-aq", "--filter", f"label=com.docker.compose.project={project}"],
        check=True,
        timeout=10,
        capture_output=True,
        text=True,
    )
    identities = listing.stdout.splitlines()
    if len(identities) > 512 or any(
        re.fullmatch(r"[a-f0-9]{12,64}", identity) is None for identity in identities
    ):
        raise ReleaseInputError("project container inventory is uncertain")
    root = base / project / "secrets"
    protected: set[Path] = set()
    for identity in identities:
        inspected = subprocess.run(
            ["docker", "inspect", "--format", "{{json .}}", identity],
            check=True,
            timeout=10,
            capture_output=True,
            text=True,
        )
        if len(inspected.stdout.encode()) > 1024 * 1024:
            raise ReleaseInputError("project container inventory exceeds its bound")
        try:
            container = json.loads(inspected.stdout)
            if (
                container["Config"]["Labels"]["com.docker.compose.project"] != project
                or not isinstance(container["Mounts"], list)
                or not isinstance(container.get("Id"), str)
                or re.fullmatch(r"[a-f0-9]{64}", container["Id"]) is None
                or not container["Id"].startswith(identity)
            ):
                raise ReleaseInputError("project container identity differs")
            for mount in container["Mounts"]:
                if not isinstance(mount, dict) or mount.get("Type") not in {
                    "bind",
                    "volume",
                    "tmpfs",
                }:
                    raise ReleaseInputError("project mount inventory is uncertain")
                if mount["Type"] == "bind":
                    if not isinstance(mount.get("Source"), str):
                        raise ReleaseInputError("project bind source is uncertain")
                    source = Path(mount["Source"])
                    if source.resolve().is_relative_to(root.resolve()):
                        protected.add(_credential_generation(source, root))
        except (KeyError, TypeError, ValueError):
            raise ReleaseInputError("project mounted credentials cannot be verified") from None
    return frozenset(protected)


def cleanup_unreferenced_secrets(
    state_root: Path, base: Path, project: str, *, mounted_generations: frozenset[Path]
) -> int:
    project = validate_project_name(project)
    protected = protected_secret_generations(state_root, base, project) | mounted_generations
    root = base / project / "secrets"
    if root.is_symlink():
        raise ReleaseInputError("credential cleanup root is linked")
    if not root.exists():
        return 0
    removed = 0
    for generation in root.glob("generation-*"):
        if generation.resolve() in protected:
            continue
        if (
            generation.is_symlink()
            or not generation.is_dir()
            or generation.stat().st_uid != os.getuid()
            or generation.stat().st_mode & 0o077
        ):
            raise ReleaseInputError("credential cleanup generation is unsafe")
        entries = list(generation.iterdir())
        if any(
            path.name not in (*REQUIRED_SECRET_FILES, *OPTIONAL_SECRET_FILES)
            or path.is_symlink()
            or not path.is_file()
            or path.stat().st_uid != os.getuid()
            or path.stat().st_nlink != 1
            or stat.S_IMODE(path.stat().st_mode) not in {0o600, 0o644}
            for path in entries
        ):
            raise ReleaseInputError("credential cleanup contents are uncertain")
        for path in entries:
            path.unlink()
        generation.rmdir()
        removed += 1
    root_fd = os.open(root, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        os.fsync(root_fd)
    finally:
        os.close(root_fd)
    if not any(root.iterdir()):
        root.rmdir()
    return removed


def validate_external_secret_base(base: Path, source_root: Path) -> Path:
    """Reject credentials placed in the source tree, including via a parent symlink."""
    resolved = base.expanduser().resolve()
    if resolved.is_relative_to(source_root.resolve()):
        raise ReleaseInputError("credential base must be outside the repository")
    return resolved


def stage_secret_generation(root: Path, values: Mapping[str, str]) -> Path:
    """Validate all inputs before writing an unpublished generation.

    Callers select a new generation for container recreation only after it is complete.
    Existing mounted files are never overwritten in place.
    """
    missing = [name for name in REQUIRED_SECRET_FILES if not values.get(name)]
    if missing:
        raise ReleaseInputError(f"missing required credential: {', '.join(missing)}")
    if root.is_symlink():
        raise ReleaseInputError("credential directory must not be a symlink")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    root.chmod(0o700)
    generation = Path(tempfile.mkdtemp(prefix="generation-", dir=root))
    generation.chmod(0o700)
    try:
        for name in (*REQUIRED_SECRET_FILES, *OPTIONAL_SECRET_FILES):
            value = values.get(name, "")
            target = generation / name
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            with os.fdopen(fd, "wb") as out:
                out.write(value.encode("utf-8"))
                out.flush()
                os.fsync(out.fileno())
            # Docker file-secret bind mounts retain the host uid; uid 65532 must read
            # the file after its 0700 parent has been traversed by the daemon.
            target.chmod(0o644)
        return generation
    except BaseException:
        shutil.rmtree(generation)
        raise


def release_manifest(
    resolved: dict[str, Any],
    *,
    secret_dir: Path,
    cluster_dir: Path,
    image_ids: Mapping[str, str],
) -> dict[str, Any]:
    """Freeze bind sources and image IDs after Compose resolves all interpolation."""
    manifest = cast(dict[str, Any], json.loads(json.dumps(resolved)))
    for name, source in manifest.get("secrets", {}).items():
        if name not in (*REQUIRED_SECRET_FILES, *OPTIONAL_SECRET_FILES):
            raise ReleaseInputError(f"unexpected Compose secret: {name}")
        source["file"] = str(secret_dir / name)
    for name, service in manifest["services"].items():
        service.pop("build", None)
        image = service.get("image", "")
        if name == "postgres" or image.startswith("coire-") or "/coire-" in image:
            if name not in image_ids:
                raise ReleaseInputError(f"missing image ID for {name}")
            service["image"] = image_ids[name]
        for volume in service.get("volumes", []):
            if volume.get("target") == "/run/coire/cluster":
                volume["source"] = str(cluster_dir)
                volume["bind"] = {"create_host_path": False}
    return manifest


def matching_active_generation(
    state_root: Path, base: Path, project: str, values: Mapping[str, str]
) -> Path | None:
    """Reuse the current immutable files when Keychain material has not changed."""
    manifest_path = state_root / project / "current" / "compose.json"
    try:
        manifest = json.loads(manifest_path.read_text())
        files = manifest["secrets"]
        paths = {name: Path(files[name]["file"]) for name in REQUIRED_SECRET_FILES}
        parents = {path.parent for path in paths.values()}
        if len(parents) != 1:
            return None
        generation = parents.pop()
        if (
            generation.is_symlink()
            or generation.resolve().parent != (base / project / "secrets").resolve()
        ):
            return None
        paths.update({name: generation / name for name in OPTIONAL_SECRET_FILES})
        for name, path in paths.items():
            if (
                path.is_symlink()
                or not path.is_file()
                or path.read_bytes() != values.get(name, "").encode()
            ):
                return None
        return generation
    except (OSError, KeyError, TypeError, ValueError):
        return None


def _read_keychain(name: str) -> str:
    result = subprocess.run(
        ["security", "find-generic-password", "-w", "-s", name],
        capture_output=True,
        check=False,
        text=True,
    )
    return result.stdout.rstrip("\n") if result.returncode == 0 else ""


def read_secret_inputs(*, from_env: bool) -> dict[str, str]:
    """Read every configured source into memory before creating any credential file."""
    values: dict[str, str] = {}
    for filename, item in KEYCHAIN_ITEMS.items():
        env_name = "COIRE_SECRET_" + filename.upper()
        values[filename] = os.environ.get(env_name, "") if from_env else _read_keychain(item)
        if from_env and filename in REQUIRED_SECRET_FILES and not values[filename]:
            raise ReleaseInputError(f"missing environment secret: {env_name}")
    if (
        "ops" in os.environ.get("COMPOSE_PROFILES", "").split(",")
        and not values["ops_service_token"]
    ):
        raise ReleaseInputError("ops profile requires coire-ops-service-token")
    if (
        "chat-files" in os.environ.get("COMPOSE_PROFILES", "").split(",")
        and not values["file_worker_service_token"]
    ):
        raise ReleaseInputError("chat-files profile requires coire-file-worker-service-token")
    if os.environ.get("COIRE_FAILOVER_MEMBER_NAME") == "coire-core":
        for key in (
            "COIRE_FAILOVER_CORE_PUBLIC_KEY",
            "COIRE_FAILOVER_EDGE_A_PUBLIC_KEY",
            "COIRE_FAILOVER_EDGE_B_PUBLIC_KEY",
        ):
            if not os.environ.get(key):
                raise ReleaseInputError(f"missing failover membership key: {key}")
        if not values["failover_peer_key"]:
            raise ReleaseInputError("missing core failover signing key")
    else:
        values["failover_peer_key"] = ""
    values["failover_relay_token"] = ""
    return values


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] in {
        "host-up",
        "host-down",
        "verify-host",
        "mark-host",
        "cleanup-secrets",
    }:
        try:
            project = validate_project_name(os.environ.get("COMPOSE_PROJECT_NAME", "coire"))
            state_root = Path(
                os.environ.get("COIRE_STATE_ROOT", str(Path.home() / ".coire/projects"))
            ).expanduser()
            if sys.argv[1] in {"verify-host", "mark-host", "cleanup-secrets"}:
                if len(sys.argv) != 2:
                    raise ReleaseInputError("unexpected ownership arguments")
                if sys.argv[1] == "verify-host":
                    verify_host_deployment(state_root, project)
                elif sys.argv[1] == "mark-host":
                    mark_host_mutation(state_root, project)
                else:
                    _, owner = verify_host_deployment(state_root, project)
                    if owner.stage != "mutating":
                        raise ReleaseInputError(
                            "credential cleanup requires owned runtime reconciliation"
                        )
                    base = Path(
                        os.environ.get(
                            "COIRE_SECRETS_BASE",
                            os.environ.get("COIRE_SECRETS_DIR", str(state_root)),
                        )
                    ).expanduser()
                    configured_repo = os.environ.get("COIRE_REPO_ROOT")
                    if configured_repo:
                        base = validate_external_secret_base(base, Path(configured_repo))
                    cleanup_unreferenced_secrets(
                        state_root,
                        base,
                        project,
                        mounted_generations=mounted_secret_generations(base, project),
                    )
                return 0
            source_root = Path(os.environ.get("COIRE_REPO_ROOT", str(Path.cwd())))
            arguments = tuple(sys.argv[2:])
            if arguments and arguments[0] == "--":
                arguments = arguments[1:]
            return run_host_deployment(
                "up" if sys.argv[1] == "host-up" else "down",
                arguments,
                source_root=source_root,
                state_root=state_root,
                project=project,
            )
        except (ReleaseInputError, CoireError, OSError, subprocess.CalledProcessError) as exc:
            print(str(exc), file=sys.stderr)
            return 3 if isinstance(exc, UpgradeConflict) else 2
    parser = argparse.ArgumentParser(description="Prepare isolated Coire deployment credentials")
    parser.add_argument("prepare", nargs="?", choices=["prepare"])
    parser.add_argument("--secrets-from-env", action="store_true")
    args = parser.parse_args()
    try:
        project = validate_project_name(os.environ.get("COMPOSE_PROJECT_NAME", "coire"))
        base = Path(os.environ.get("COIRE_SECRETS_BASE", str(Path.home() / ".coire" / "projects")))
        configured_source_root = os.environ.get("COIRE_REPO_ROOT")
        if configured_source_root:
            base = validate_external_secret_base(base, Path(configured_source_root))
        if base.is_symlink():
            raise ReleaseInputError("credential base must not be a symlink")
        values = read_secret_inputs(from_env=args.secrets_from_env)
        state_root = Path(
            os.environ.get("COIRE_STATE_ROOT", str(Path.home() / ".coire" / "projects"))
        )
        generation = matching_active_generation(state_root, base, project, values)
        if generation is None:
            generation = stage_secret_generation(base / project / "secrets", values)
    except ReleaseInputError as exc:
        parser.exit(2, f"{exc}\n")
    print(generation)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
