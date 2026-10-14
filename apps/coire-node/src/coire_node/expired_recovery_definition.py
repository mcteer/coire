"""Safe fixed-location legacy plist preimage read, never installation/mutation."""

import os
import pwd
import stat
from pathlib import Path

from coire_core.errors import UpgradeUnavailable
from coire_core.models.upgrade_recovery import UpgradeExpiredRecoveryLegacyService
from coire_core.models.upgrades import NativeGuardStatus
from coire_core.settings import Settings
from coire_node.expired_recovery_launchd import verify_legacy_definition


def read_definition_file(path: Path, expected_uid: int) -> bytes:
    """Internal trusted OS path only; not a wire path or remote file reader."""
    fd = None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        before = os.fstat(fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != expected_uid
            or before.st_mode & 0o022
            or before.st_nlink != 1
            or before.st_size > 64 * 1024
        ):
            raise UpgradeUnavailable("Legacy service definition ownership or size differs")
        with os.fdopen(fd, "rb", closefd=False) as handle:
            data = handle.read(64 * 1024 + 1)
        after = os.fstat(fd)
        if len(data) > 64 * 1024 or (
            before.st_ino,
            before.st_dev,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) != (after.st_ino, after.st_dev, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise UpgradeUnavailable("Legacy service definition changed during read")
        installed = path.lstat()
        if (
            not stat.S_ISREG(installed.st_mode)
            or (installed.st_dev, installed.st_ino) != (after.st_dev, after.st_ino)
            or installed.st_uid != expected_uid
            or installed.st_mode & 0o022
            or installed.st_nlink != 1
            or (installed.st_size, installed.st_mtime_ns, installed.st_ctime_ns)
            != (after.st_size, after.st_mtime_ns, after.st_ctime_ns)
        ):
            raise UpgradeUnavailable("Legacy service definition path changed during read")
        return data
    except OSError:
        raise UpgradeUnavailable("Legacy service definition cannot be safely read") from None
    finally:
        if fd is not None:
            os.close(fd)


def read_legacy_definition(
    settings: Settings,
    guard: NativeGuardStatus,
    service: UpgradeExpiredRecoveryLegacyService,
) -> str:
    """Resolve fixed service file from positively observed domain/current UID.

    Return digest only; raw plist may contain secrets. Loaded preservation policy
    and current physical/runtime/Core authority must be independently verified.
    """
    service = UpgradeExpiredRecoveryLegacyService.model_validate_json(service.model_dump_json())
    if service.owner_uid != os.getuid() or service.process != guard.process:
        raise UpgradeUnavailable("Legacy service definition actor differs")
    try:
        owner = pwd.getpwuid(service.owner_uid)
    except KeyError:
        raise UpgradeUnavailable("Legacy service definition user is unknown") from None
    if service.target == "system/com.coire.upgrade-guard":
        path = Path("/Library/LaunchDaemons/com.coire.upgrade-guard.plist")
        expected_uid = 0
    elif service.target == f"gui/{service.owner_uid}/com.coire.upgrade-guard":
        path = Path(owner.pw_dir) / "Library/LaunchAgents/com.coire.upgrade-guard.plist"
        expected_uid = service.owner_uid
    else:
        raise UpgradeUnavailable("Legacy service definition has no fixed domain")
    return verify_legacy_definition(
        settings, guard, read_definition_file(path, expected_uid), owner.pw_name
    )
