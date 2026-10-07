"""Kernel evidence for OS processes whose argv is protected on macOS.

Bare Coire engines execute in Python/script hosts. A valid Apple platform native
process is not such a host; names or filesystem locations alone never prove that.
"""

import ctypes
import platform
from pathlib import Path

import psutil


def platform_code_flags(pid: int) -> int | None:
    if platform.system() != "Darwin":
        return None
    try:
        library = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
        library.csops.argtypes = [ctypes.c_int, ctypes.c_uint, ctypes.c_void_p, ctypes.c_size_t]
        library.csops.restype = ctypes.c_int
        flags = ctypes.c_uint()
        if library.csops(pid, 0, ctypes.byref(flags), ctypes.sizeof(flags)) != 0:
            return None
        return flags.value
    except (OSError, AttributeError, ValueError):
        return None


def verified_native_system_process(process: psutil.Process) -> bool:
    """Protected argv can be ignored only with stable identity and platform code.

    CS_VALID and CS_PLATFORM_BINARY come from the kernel, not a caller name/path.
    Generic code hosts remain candidates even when Apple signed. Any uncertainty
    remains a refusal; this does not authorize or skip another native engine.
    """
    try:
        born = process.create_time()
        path = Path(process.exe())
        if not path.is_absolute():
            return False
        name = path.name.lower()
        if (
            name in {"sh", "bash", "zsh", "dash", "osascript", "java", "node", "swift", "xpcproxy"}
            or any(host in name for host in ("python", "pypy", "perl", "ruby", "lua", "jsc"))
            or any(marker in str(path).lower() for marker in ("coire", "mlx", "mflux"))
        ):
            return False
        flags = platform_code_flags(process.pid)
        if flags is None or flags & 0x04000001 != 0x04000001:
            return False
        return bool(psutil.Process(process.pid).create_time() == born)
    except (psutil.Error, OSError, AttributeError, ValueError):
        return False


def attempt_process_absent(attempt: str) -> bool:
    """Positive current absence, including bare workers surviving lost local state."""
    for process in psutil.process_iter():
        try:
            if any(attempt in argument for argument in process.cmdline()):
                return False
        except psutil.NoSuchProcess:
            continue
        except psutil.AccessDenied:
            if not verified_native_system_process(process):
                return False
    return True
