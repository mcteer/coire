"""Positive retired process identity proof, never signals or owner adoption."""

import ctypes
import sys
import time
from datetime import UTC, datetime
from uuid import UUID

import psutil

from coire_core.errors import UpgradeUnavailable
from coire_core.models.upgrade_recovery import UpgradeRecoveryRetiredSupervisor
from coire_core.models.upgrades import NodeAgentProcessIdentity


def recovery_kernel_boot_identity(expires_at: datetime) -> str:
    """Read the fixed kernel value without spawning from a threaded native owner."""
    remaining = (expires_at - datetime.now(UTC)).total_seconds()
    if sys.platform != "darwin" or remaining <= 0:
        raise UpgradeUnavailable("Recovery kernel identity is outside supported epoch")
    deadline = time.monotonic() + min(1.0, remaining)
    try:
        library = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
        read = library.sysctlbyname
        read.argtypes = [
            ctypes.c_char_p,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_size_t),
            ctypes.c_void_p,
            ctypes.c_size_t,
        ]
        read.restype = ctypes.c_int
        # Canonical UUID plus NUL: no caller-controlled name, mutation or allocation.
        value = ctypes.create_string_buffer(37)
        length = ctypes.c_size_t(len(value))
        if read(b"kern.bootsessionuuid", value, ctypes.byref(length), None, 0) != 0:
            raise UpgradeUnavailable("Recovery kernel session cannot be observed")
        if length.value != 37 or value.raw[-1:] != b"\x00":
            raise UpgradeUnavailable("Recovery kernel session is malformed")
    except (OSError, AttributeError):
        raise UpgradeUnavailable("Recovery kernel session cannot be observed") from None
    try:
        identity = str(UUID(value.raw[:-1].decode("ascii")))
    except (ValueError, UnicodeError):
        raise UpgradeUnavailable("Recovery kernel session is malformed") from None
    if datetime.now(UTC) >= expires_at or time.monotonic() >= deadline:
        raise UpgradeUnavailable("Recovery kernel observation exceeded captured epoch")
    return identity


def verify_retired_identity(
    identity: NodeAgentProcessIdentity | UpgradeRecoveryRetiredSupervisor,
) -> None:
    """Only missing PID or positively different birth proves old identity gone.

    Owned-process lookup returning None can also mean denied/uncertain access and
    must not be used as absence proof. A zombie with the same birth still refuses.
    PID reuse proves only the specific old identity absent, not complete inventory
    emptiness or permission to signal the unrelated replacement process.
    """
    try:
        process = psutil.Process(identity.pid)
        birth = process.create_time()
    except psutil.NoSuchProcess:
        return
    except psutil.Error:
        raise UpgradeUnavailable("Retired process identity cannot be observed") from None
    if birth == identity.create_time_s:
        raise UpgradeUnavailable("Retired process identity remains present")
