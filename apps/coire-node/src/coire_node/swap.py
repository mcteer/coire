"""Authoritative swap counters; Darwin filesystem page-outs are a separate metric."""

from __future__ import annotations

import ctypes
import logging
import sys
from dataclasses import dataclass
from functools import lru_cache

import psutil
from opentelemetry import metrics, trace

from coire_core.errors import TrainingUnavailable

_IS_DARWIN = sys.platform == "darwin"
_HOST_VM_INFO64 = 4
_HOST_INFO_MAX = 1024
logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)
failures = metrics.get_meter("coire.node.training").create_counter(
    "coire_training_swap_sample_failures", description="Unverifiable native swap telemetry"
)


@dataclass(frozen=True)
class SwapSample:
    used: int
    out: int


class _VMStatistics64Prefix(ctypes.Structure):
    """Stable HOST_VM_INFO64 ABI prefix through the compressor swap-out counter.

    The kernel writes into a full HOST_INFO_MAX buffer below, so newer trailing
    fields cannot overrun this prefix. Returned word count must cover swapouts.
    Layout follows the installed mach/vm_statistics.h, including natural_t fields.
    """

    _fields_ = [
        ("free_count", ctypes.c_uint32),
        ("active_count", ctypes.c_uint32),
        ("inactive_count", ctypes.c_uint32),
        ("wire_count", ctypes.c_uint32),
        ("zero_fill_count", ctypes.c_uint64),
        ("reactivations", ctypes.c_uint64),
        ("pageins", ctypes.c_uint64),
        ("pageouts", ctypes.c_uint64),
        ("faults", ctypes.c_uint64),
        ("cow_faults", ctypes.c_uint64),
        ("lookups", ctypes.c_uint64),
        ("hits", ctypes.c_uint64),
        ("purges", ctypes.c_uint64),
        ("purgeable_count", ctypes.c_uint32),
        ("speculative_count", ctypes.c_uint32),
        ("decompressions", ctypes.c_uint64),
        ("compressions", ctypes.c_uint64),
        ("swapins", ctypes.c_uint64),
        ("swapouts", ctypes.c_uint64),
    ]


@lru_cache(maxsize=1)
def _darwin_api() -> tuple[ctypes.CDLL, int]:
    library = ctypes.CDLL("/usr/lib/libSystem.dylib")
    library.mach_host_self.argtypes = []
    library.mach_host_self.restype = ctypes.c_uint32
    library.host_statistics64.argtypes = [
        ctypes.c_uint32,
        ctypes.c_int32,
        ctypes.POINTER(ctypes.c_int32),
        ctypes.POINTER(ctypes.c_uint32),
    ]
    library.host_statistics64.restype = ctypes.c_int32
    library.host_page_size.argtypes = [ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32)]
    library.host_page_size.restype = ctypes.c_int32
    library.mach_port_deallocate.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
    library.mach_port_deallocate.restype = ctypes.c_int32
    task = ctypes.c_uint32.in_dll(library, "mach_task_self_").value
    if not task:
        raise TrainingUnavailable("Native swap telemetry task identity is unavailable")
    return library, task


def _darwin_swap_out_bytes() -> int:
    library, task = _darwin_api()
    host = library.mach_host_self()
    if not host:
        raise TrainingUnavailable("Native swap telemetry host identity is unavailable")
    try:
        buffer = (ctypes.c_int32 * _HOST_INFO_MAX)()
        count = ctypes.c_uint32(_HOST_INFO_MAX)
        page_size = ctypes.c_uint32()
        required = ctypes.sizeof(_VMStatistics64Prefix) // ctypes.sizeof(ctypes.c_int32)
        if (
            library.host_statistics64(host, _HOST_VM_INFO64, buffer, ctypes.byref(count)) != 0
            or not required <= count.value <= _HOST_INFO_MAX
            or library.host_page_size(host, ctypes.byref(page_size)) != 0
            or not 0 < page_size.value <= 1024**2
            or page_size.value & (page_size.value - 1)
        ):
            raise TrainingUnavailable("Native swap telemetry counter or page size is unavailable")
        return int(_VMStatistics64Prefix.from_buffer(buffer).swapouts) * page_size.value
    finally:
        if library.mach_port_deallocate(task, host) != 0:
            raise TrainingUnavailable("Native swap telemetry host port cleanup is unverified")


def read_swap_sample() -> SwapSample:
    """Refuse unknown readings; never substitute filesystem I/O or fabricated zero."""
    with tracer.start_as_current_span(
        "coire.node.training.swap_sample", record_exception=False, set_status_on_exception=False
    ):
        try:
            swap = psutil.swap_memory()
            used = int(swap.used)
            out = _darwin_swap_out_bytes() if _IS_DARWIN else int(swap.sout)
            if used < 0 or out < 0:
                raise TrainingUnavailable("Native swap telemetry counter is invalid")
            return SwapSample(used=used, out=out)
        except (OSError, AttributeError, ValueError, psutil.Error, TrainingUnavailable):
            failures.add(1, {"platform": "darwin" if _IS_DARWIN else "other"})
            logger.warning("native swap telemetry unavailable", extra={"reason": "swap_unverified"})
            raise TrainingUnavailable("Native swap telemetry cannot be verified") from None
