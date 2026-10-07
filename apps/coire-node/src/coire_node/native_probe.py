"""Bounded macOS probes avoid fork handlers and inherit only explicit stdio."""

from __future__ import annotations

import ctypes
import os
import platform
import signal
import subprocess
import tempfile
import time
from pathlib import Path

_MAX_OUTPUT = 4 * 1024**2
# Apple's public sys/spawn.h flag closes every descriptor except explicit file actions.
_CLOEXEC_DEFAULT = 0x4000


def run_probe(argv: list[str], *, timeout: float) -> subprocess.CompletedProcess[bytes]:
    if not argv or not Path(argv[0]).is_absolute() or timeout <= 0:
        raise ValueError("Probe requires an explicit executable and positive deadline")
    if platform.system() != "Darwin":
        result = subprocess.run(
            argv,
            capture_output=True,
            timeout=timeout,
            env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "C"},
        )
        if len(result.stdout) > _MAX_OUTPUT or len(result.stderr) > _MAX_OUTPUT:
            raise subprocess.SubprocessError("Probe output exceeds its bounded capture")
        return result
    libc = ctypes.CDLL(None)
    opaque = ctypes.POINTER(ctypes.c_void_p)
    signatures = {
        "posix_spawnattr_init": [opaque],
        "posix_spawnattr_destroy": [opaque],
        "posix_spawnattr_setflags": [opaque, ctypes.c_short],
        "posix_spawn_file_actions_init": [opaque],
        "posix_spawn_file_actions_destroy": [opaque],
        "posix_spawn_file_actions_adddup2": [opaque, ctypes.c_int, ctypes.c_int],
        "posix_spawn": [
            ctypes.POINTER(ctypes.c_int),
            ctypes.c_char_p,
            opaque,
            opaque,
            ctypes.POINTER(ctypes.c_char_p),
            ctypes.POINTER(ctypes.c_char_p),
        ],
    }
    for name, arguments in signatures.items():
        function = getattr(libc, name)
        function.argtypes = arguments
        function.restype = ctypes.c_int

    def checked(code: int) -> None:
        if code:
            raise OSError(code, os.strerror(code))

    attributes, actions = ctypes.c_void_p(), ctypes.c_void_p()
    checked(libc.posix_spawnattr_init(ctypes.byref(attributes)))
    try:
        checked(libc.posix_spawnattr_setflags(ctypes.byref(attributes), _CLOEXEC_DEFAULT))
        checked(libc.posix_spawn_file_actions_init(ctypes.byref(actions)))
        try:
            with (
                open(os.devnull, "rb") as stdin,
                tempfile.TemporaryFile() as stdout,
                tempfile.TemporaryFile() as stderr,
            ):
                for source, destination in [
                    (stdin.fileno(), 0),
                    (stdout.fileno(), 1),
                    (stderr.fileno(), 2),
                ]:
                    checked(
                        libc.posix_spawn_file_actions_adddup2(
                            ctypes.byref(actions), source, destination
                        )
                    )
                arguments = (ctypes.c_char_p * (len(argv) + 1))(
                    *[os.fsencode(a) for a in argv], None
                )
                environment = (ctypes.c_char_p * 3)(
                    b"PATH=/usr/bin:/bin:/usr/sbin:/sbin", b"LANG=C", None
                )
                pid = ctypes.c_int()
                checked(
                    libc.posix_spawn(
                        ctypes.byref(pid),
                        os.fsencode(argv[0]),
                        ctypes.byref(actions),
                        ctypes.byref(attributes),
                        arguments,
                        environment,
                    )
                )
                deadline = time.monotonic() + timeout
                while True:
                    waited, status = os.waitpid(pid.value, os.WNOHANG)
                    if waited:
                        break
                    if time.monotonic() >= deadline:
                        os.kill(pid.value, signal.SIGKILL)
                        os.waitpid(pid.value, 0)
                        raise subprocess.TimeoutExpired(argv, timeout)
                    time.sleep(min(0.005, max(0, deadline - time.monotonic())))
                stdout.seek(0)
                stderr.seek(0)
                output, errors = stdout.read(_MAX_OUTPUT + 1), stderr.read(_MAX_OUTPUT + 1)
                if len(output) > _MAX_OUTPUT or len(errors) > _MAX_OUTPUT:
                    raise subprocess.SubprocessError("Probe output exceeds its bounded capture")
                return subprocess.CompletedProcess(
                    argv, os.waitstatus_to_exitcode(status), output, errors
                )
        finally:
            libc.posix_spawn_file_actions_destroy(ctypes.byref(actions))
    finally:
        libc.posix_spawnattr_destroy(ctypes.byref(attributes))
