"""Protected argv is classified from kernel code identity, never guessed vacancy."""

import psutil
import pytest

from coire_node.training import process_inventory as inventory


@pytest.mark.parametrize(
    ("path", "flags", "new_birth", "expected"),
    [
        ("/usr/libexec/system-daemon", 0x04000001, 1.0, True),
        ("/usr/libexec/system-daemon", 0x04000000, 1.0, False),
        ("/usr/libexec/system-daemon", 1, 1.0, False),
        ("/usr/libexec/system-daemon", None, 1.0, False),
        ("/usr/libexec/system-daemon", 0x04000001, 2.0, False),
        ("/usr/bin/python3", 0x04000001, 1.0, False),
        ("/bin/zsh", 0x04000001, 1.0, False),
        ("/usr/libexec/xpcproxy", 0x04000001, 1.0, False),
        ("/opt/coire/python/bin/coire-node-python", 0x04000001, 1.0, False),
        ("relative-daemon", 0x04000001, 1.0, False),
    ],
)
def test_only_stable_valid_native_platform_code_is_outside_bare_engine_inventory(
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    flags: int | None,
    new_birth: float,
    expected: bool,
) -> None:
    class Process:
        pid = 42

        def create_time(self) -> float:
            return 1.0

        def exe(self) -> str:
            return path

    class Current:
        def create_time(self) -> float:
            return new_birth

    monkeypatch.setattr(inventory, "platform_code_flags", lambda _: flags)
    monkeypatch.setattr("coire_node.training.process_inventory.psutil.Process", lambda _: Current())
    assert inventory.verified_native_system_process(Process()) is expected


def test_unreadable_executable_cannot_be_exempted(monkeypatch: pytest.MonkeyPatch) -> None:
    class Process:
        pid = 42

        def create_time(self) -> float:
            return 1.0

        def exe(self) -> str:
            raise psutil.AccessDenied(self.pid)

    monkeypatch.setattr(inventory, "platform_code_flags", lambda _: 0x04000001)
    assert not inventory.verified_native_system_process(Process())


def test_attempt_absence_requires_complete_candidate_inventory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempt = "01ARZ3NDEKTSV4RRFFQ69G5FAW"

    class Process:
        def cmdline(self) -> list[str]:
            return ["python", "-m", "coire_node.training.measurement", "--attempt", attempt]

    monkeypatch.setattr(
        "coire_node.training.process_inventory.psutil.process_iter", lambda: [Process()]
    )
    assert not inventory.attempt_process_absent(attempt)

    class Protected:
        def cmdline(self) -> list[str]:
            raise psutil.AccessDenied(42)

    monkeypatch.setattr(
        "coire_node.training.process_inventory.psutil.process_iter", lambda: [Protected()]
    )
    monkeypatch.setattr(inventory, "verified_native_system_process", lambda _: False)
    assert not inventory.attempt_process_absent(attempt)
    monkeypatch.setattr(inventory, "verified_native_system_process", lambda _: True)
    assert inventory.attempt_process_absent(attempt)
