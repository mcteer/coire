"""Fixed Darwin kernel read has no subprocess, mutation or renewed epoch."""

import ctypes
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from coire_core.errors import UpgradeUnavailable


@pytest.mark.parametrize(
    "fault", [None, "error", "malformed", "expired", "oversize", "unterminated", "slow", "library"]
)
def test_kernel_identity_is_bounded_fixed_read(monkeypatch, fault):
    from coire_node import recovery_identity as worker

    identity = str(uuid4())
    calls = []

    class Read:
        def __call__(self, name, value, size, new_value, new_size):
            calls.append(name)
            assert name == b"kern.bootsessionuuid"
            assert new_value is None and new_size == 0
            assert ctypes.cast(size, ctypes.POINTER(ctypes.c_size_t)).contents.value == 37
            payload = b"invalid" if fault == "malformed" else identity.encode()
            value.raw = (payload + b"\x00").ljust(37, b"\x00")
            if fault == "unterminated":
                value.raw = identity.encode() + b"x"
            if fault == "oversize":
                ctypes.cast(size, ctypes.POINTER(ctypes.c_size_t)).contents.value = 38
            return 1 if fault == "error" else 0

    read = Read()

    def library(path, *, use_errno):
        assert path == "/usr/lib/libSystem.B.dylib" and use_errno is True
        if fault == "library":
            raise OSError("private loader details")
        return SimpleNamespace(sysctlbyname=read)

    monkeypatch.setattr(worker.ctypes, "CDLL", library)
    monkeypatch.setattr(worker.sys, "platform", "darwin")
    ticks = iter([10.0, 11.0 if fault == "slow" else 10.1])
    monkeypatch.setattr(worker.time, "monotonic", lambda: next(ticks))
    expires = datetime.now(UTC) + timedelta(seconds=-1 if fault == "expired" else 10)
    if fault:
        with pytest.raises(UpgradeUnavailable):
            worker.recovery_kernel_boot_identity(expires)
    else:
        assert worker.recovery_kernel_boot_identity(expires) == identity
    assert calls == ([] if fault in {"expired", "library"} else [b"kern.bootsessionuuid"])
    if calls:
        assert read.restype is ctypes.c_int
        assert len(read.argtypes) == 5


def test_kernel_read_rejects_unsupported_platform_before_loading(monkeypatch):
    from coire_node import recovery_identity as worker

    monkeypatch.setattr(worker.sys, "platform", "linux")
    monkeypatch.setattr(worker.ctypes, "CDLL", lambda *a, **k: pytest.fail("loaded Darwin library"))
    with pytest.raises(UpgradeUnavailable):
        worker.recovery_kernel_boot_identity(datetime.now(UTC) + timedelta(seconds=10))
