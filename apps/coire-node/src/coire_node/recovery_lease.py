"""Non-wire lifetime capability; retained claim data is never a held kernel lease."""

import fcntl
import os
import stat
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path

from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.upgrade_journal import PrivateUpgradeStore


@dataclass(frozen=True)
class _Capability:
    path: Path
    owner_sha256: str
    descriptor: int
    pid: int
    released: threading.Event
    lifetime_descriptor: int | None
    lifetime_released: threading.Event
    supervision_descriptor: int | None


_scope: ContextVar[_Capability | None] = ContextVar("coire_recovery_lease", default=None)


def require_recovery_lease(store: PrivateUpgradeStore, owner_sha256: str) -> None:
    """Validate capability and actual held private lock, no implicit acquisition.

    Caller also reauthorizes Core and physical identity at every effect boundary.
    Capability is never serialized, inherited across process identity or a grant
    of readiness/qualification. Child tasks cannot use it after descriptor close.
    """
    cap = _scope.get()
    path = store.path("recovery-transfer.lock")
    if (
        cap is None
        or cap.released.is_set()
        or cap.path != path
        or cap.owner_sha256 != owner_sha256
        or cap.pid != os.getpid()
    ):
        raise UpgradeUnavailable("Recovery publication has no matching lifetime lease")
    probe = None
    try:
        held = os.fstat(cap.descriptor)
        expected = path.lstat()
        if (
            (held.st_dev, held.st_ino) != (expected.st_dev, expected.st_ino)
            or not stat.S_ISREG(expected.st_mode)
            or expected.st_uid != os.getuid()
            or expected.st_mode & 0o077
            or expected.st_nlink != 1
        ):
            raise UpgradeUnavailable("Recovery lifetime descriptor differs")
        probe = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
        if (os.fstat(probe).st_dev, os.fstat(probe).st_ino) != (held.st_dev, held.st_ino):
            raise UpgradeUnavailable("Recovery lifetime lock changed")
        try:
            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            if cap.released.is_set():
                raise UpgradeUnavailable(
                    "Recovery lifetime capability was released during probe"
                ) from None
            return
        raise UpgradeUnavailable("Recovery lifetime descriptor is not locked")
    except OSError:
        raise UpgradeUnavailable("Recovery lifetime descriptor cannot be verified") from None
    finally:
        if probe is not None:
            os.close(probe)


@contextmanager
def bind_recovery_lease(
    store: PrivateUpgradeStore,
    owner_sha256: str,
    descriptor: int,
    *,
    lifetime_descriptor: int | None = None,
    supervision_descriptor: int | None = None,
) -> Iterator[None]:
    """Bind only a descriptor already held by the trusted owner context."""
    cap = _Capability(
        store.path("recovery-transfer.lock"),
        owner_sha256,
        descriptor,
        os.getpid(),
        threading.Event(),
        lifetime_descriptor,
        threading.Event(),
        supervision_descriptor,
    )
    token = _scope.set(cap)
    try:
        require_recovery_lease(store, owner_sha256)
        yield
    finally:
        # Context copies retain the same object. Revoke before resetting the
        # local context, even if another binding still holds this descriptor.
        cap.released.set()
        _scope.reset(token)


def recovery_supervision_descriptor(
    store: PrivateUpgradeStore, owner_sha256: str, path: Path
) -> int:
    """Return only the actual held fixture descriptor of this cold lifetime.

    Passing this descriptor to a fixed child preserves flock across parent
    close. Wire claims and a newly opened descriptor cannot substitute for it.
    """
    require_recovery_lease(store, owner_sha256)
    cap = _scope.get()
    if cap is None or cap.supervision_descriptor is None:
        raise UpgradeUnavailable("Recovery lifetime has no fixture supervision descriptor")
    descriptor = cap.supervision_descriptor
    try:
        held = os.fstat(descriptor)
        actual = path.stat(follow_symlinks=False)
        if (
            (held.st_dev, held.st_ino) != (actual.st_dev, actual.st_ino)
            or held.st_uid != os.getuid()
            or held.st_mode & 0o077
            or not stat.S_ISREG(held.st_mode)
            or held.st_nlink != 1
        ):
            raise UpgradeUnavailable("Recovery supervision descriptor differs from private path")
        with PrivateUpgradeStore(path.parent, create=False).ownership(path.name):
            raise UpgradeUnavailable("Recovery supervision descriptor is not locked")
    except OSError:
        raise UpgradeUnavailable("Recovery supervision descriptor cannot be verified") from None
    except UpgradeConflict:
        pass  # A separate open description positively confirms held exclusion.
    require_recovery_lease(store, owner_sha256)
    return descriptor


def handoff_recovery_lifetime(store: PrivateUpgradeStore, owner_sha256: str) -> None:
    """Only the held cold session can relinquish its writer lock for Source.

    Caller must first retain/authorize exact handback intent and keep original
    maintenance plus the separate recovery descriptor continuously held. This
    function grants no Source readiness or authority to open admission.
    """
    require_recovery_lease(store, owner_sha256)
    cap = _scope.get()
    if cap is None or cap.lifetime_descriptor is None:
        raise UpgradeUnavailable("Source handback has no actual cold lifetime descriptor")
    if cap.lifetime_released.is_set():
        return
    descriptor = cap.lifetime_descriptor
    path = store.path("lifetime.lock")
    probe = None
    try:
        held = os.fstat(descriptor)
        current = path.lstat()
        if (
            (held.st_dev, held.st_ino) != (current.st_dev, current.st_ino)
            or not stat.S_ISREG(current.st_mode)
            or current.st_uid != os.getuid()
            or current.st_mode & 0o077
            or current.st_nlink != 1
        ):
            raise UpgradeUnavailable("Source handback lifetime descriptor differs")
        probe = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
        if (os.fstat(probe).st_dev, os.fstat(probe).st_ino) != (held.st_dev, held.st_ino):
            raise UpgradeUnavailable("Source handback lifetime path changed")
        try:
            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            pass
        else:
            raise UpgradeUnavailable("Source handback lifetime was not continuously held")
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        cap.lifetime_released.set()
    except OSError:
        raise UpgradeUnavailable("Source handback lifetime cannot be relinquished safely") from None
    finally:
        if probe is not None:
            os.close(probe)
