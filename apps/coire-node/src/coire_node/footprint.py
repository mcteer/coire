"""Measuring what an engine actually occupies.

`psutil`'s `memory_info().rss` is the obvious choice and the wrong one here. MLX allocates
through Metal, and those allocations land in the kernel's IOAccelerator accounting rather than
in the classic resident set; a released psutil (7.2.2) reports `pti_resident_size`, which can
understate an engine's real footprint badly. MLX's own issue #3896 shows `get_peak_memory()`
reporting 46 GB while Apple's `footprint` tool showed 110 GB for the same process.

The number the kernel actually accounts — and the one jetsam acts on — is
`ri_phys_footprint` from `proc_pid_rusage(RUSAGE_INFO_V4)`. There is no released psutil API
for it (psutil 8.0 adds `memory_extras()`), so this reads it through ctypes. On anything that
is not macOS the same function falls back to RSS, which is correct there.
"""

from __future__ import annotations

import ctypes
import logging
import sys

import psutil

logger = logging.getLogger(__name__)

RUSAGE_INFO_V4 = 4
_IS_DARWIN = sys.platform == "darwin"


class _RUsageInfoV4(ctypes.Structure):
    """`rusage_info_v4` from `<sys/resource.h>`.

    Only the layout up to `ri_phys_footprint` has to be right; the trailing fields are declared
    so the structure is large enough for the kernel to fill. Field order is fixed by the ABI —
    a wrong offset here would silently return some other counter, so the layout follows the
    header exactly rather than being trimmed.
    """

    _fields_ = [
        ("ri_uuid", ctypes.c_uint8 * 16),
        ("ri_user_time", ctypes.c_uint64),
        ("ri_system_time", ctypes.c_uint64),
        ("ri_pkg_idle_wkups", ctypes.c_uint64),
        ("ri_interrupt_wkups", ctypes.c_uint64),
        ("ri_pageins", ctypes.c_uint64),
        ("ri_wired_size", ctypes.c_uint64),
        ("ri_resident_size", ctypes.c_uint64),
        ("ri_phys_footprint", ctypes.c_uint64),
        ("ri_proc_start_abstime", ctypes.c_uint64),
        ("ri_proc_exit_abstime", ctypes.c_uint64),
        ("ri_child_user_time", ctypes.c_uint64),
        ("ri_child_system_time", ctypes.c_uint64),
        ("ri_child_pkg_idle_wkups", ctypes.c_uint64),
        ("ri_child_interrupt_wkups", ctypes.c_uint64),
        ("ri_child_pageins", ctypes.c_uint64),
        ("ri_child_elapsed_abstime", ctypes.c_uint64),
        ("ri_diskio_bytesread", ctypes.c_uint64),
        ("ri_diskio_byteswritten", ctypes.c_uint64),
        ("ri_cpu_time_qos_default", ctypes.c_uint64),
        ("ri_cpu_time_qos_maintenance", ctypes.c_uint64),
        ("ri_cpu_time_qos_background", ctypes.c_uint64),
        ("ri_cpu_time_qos_utility", ctypes.c_uint64),
        ("ri_cpu_time_qos_legacy", ctypes.c_uint64),
        ("ri_cpu_time_qos_user_initiated", ctypes.c_uint64),
        ("ri_cpu_time_qos_user_interactive", ctypes.c_uint64),
        ("ri_billed_system_time", ctypes.c_uint64),
        ("ri_serviced_system_time", ctypes.c_uint64),
        ("ri_logical_writes", ctypes.c_uint64),
        ("ri_lifetime_max_phys_footprint", ctypes.c_uint64),
        ("ri_instructions", ctypes.c_uint64),
        ("ri_cycles", ctypes.c_uint64),
        ("ri_billed_energy", ctypes.c_uint64),
        ("ri_serviced_energy", ctypes.c_uint64),
        ("ri_interval_max_phys_footprint", ctypes.c_uint64),
        ("ri_runnable_time", ctypes.c_uint64),
    ]


_libproc: ctypes.CDLL | None = None
_libproc_failed = False


def _load_libproc() -> ctypes.CDLL | None:
    global _libproc, _libproc_failed
    if _libproc is not None or _libproc_failed:
        return _libproc
    try:
        _libproc = ctypes.CDLL("/usr/lib/libSystem.dylib", use_errno=True)
        _libproc.proc_pid_rusage.argtypes = [
            ctypes.c_int,
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        _libproc.proc_pid_rusage.restype = ctypes.c_int
    except (OSError, AttributeError) as exc:  # pragma: no cover - macOS always has this
        logger.warning("libproc unavailable (%s); falling back to RSS", exc)
        _libproc_failed = True
        _libproc = None
    return _libproc


def phys_footprint(pid: int) -> int | None:
    """The kernel's physical footprint for a process, or None when it cannot be read."""
    if not _IS_DARWIN:
        return None
    lib = _load_libproc()
    if lib is None:
        return None
    buffer = _RUsageInfoV4()
    rc = lib.proc_pid_rusage(
        ctypes.c_int(pid),
        ctypes.c_int(RUSAGE_INFO_V4),
        ctypes.cast(ctypes.byref(buffer), ctypes.POINTER(ctypes.c_void_p)),
    )
    if rc != 0:
        return None
    return int(buffer.ri_phys_footprint)


def resident_bytes(pid: int) -> int | None:
    """What an engine occupies, by the best measure this platform offers.

    Physical footprint on macOS — the number that includes Metal allocations and the one the
    memory pressure system acts on. RSS elsewhere, which is the right answer on Linux, where
    there is no unified memory and CI runs a fake engine anyway.

    Returns None when the process is gone: an absent measurement is honest, a zero is not.
    """
    footprint = phys_footprint(pid)
    if footprint is not None and footprint > 0:
        return footprint
    try:
        return int(psutil.Process(pid).memory_info().rss)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return None


def cpu_percent(proc: psutil.Process) -> float | None:
    """Per-process CPU since the previous call on the same object (spec FR-013)."""
    try:
        return float(proc.cpu_percent(interval=None))
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return None
