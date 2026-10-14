"""A hosted native consumer cannot invoke the operator Studio SSH transport."""

from pathlib import Path

import pytest
from upgrade_native_faults import _fault_command


def _hosted(monkeypatch: pytest.MonkeyPatch, temporary: Path) -> None:
    for name, value in {
        "GITHUB_ACTIONS": "true",
        "RUNNER_ENVIRONMENT": "github-hosted",
        "RUNNER_OS": "macOS",
        "RUNNER_ARCH": "ARM64",
        "RUNNER_TEMP": str(temporary),
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr("upgrade_native_faults.platform.system", lambda: "Darwin")
    monkeypatch.setattr("upgrade_native_faults.platform.machine", lambda: "arm64")


def _local(temporary: Path) -> tuple[Path, dict[str, object], str]:
    native = temporary / "coire-019-acceptance/native-upgrade-lab-ci"
    native.mkdir(parents=True, mode=0o700)
    python = native / "envs/source/bin/python3"
    python.parent.mkdir(parents=True)
    python.write_bytes(b"not executed by this transport-boundary test")
    root = temporary / "core/api"
    root.mkdir(parents=True, mode=0o700)
    return root, {"native_root": str(native), "native_transport": "local"}, str(python)


def test_hosted_fault_uses_owned_local_interpreter_without_ssh(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _hosted(monkeypatch, tmp_path)
    root, fixture, python = _local(tmp_path)
    command = _fault_command(root, fixture, python)
    assert command[:4] == [python, "-I", "-B", "-c"]
    assert "ssh" not in command and "coire-edge-a" not in command


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("RUNNER_ENVIRONMENT", "self-hosted"),
        ("RUNNER_OS", "Linux"),
        ("RUNNER_ARCH", "X64"),
    ],
)
def test_ci_cannot_select_operator_transport_or_untrusted_runner(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, name: str, value: str
) -> None:
    _hosted(monkeypatch, tmp_path)
    root, fixture, python = _local(tmp_path)
    monkeypatch.setenv(name, value)
    with pytest.raises(RuntimeError, match="isolated hosted"):
        _fault_command(root, fixture, python)


def test_ci_rejects_operator_fixture_even_on_hosted_mac(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _hosted(monkeypatch, tmp_path)
    root, fixture, python = _local(tmp_path)
    fixture.pop("native_transport")
    with pytest.raises(RuntimeError, match="isolated hosted"):
        _fault_command(root, fixture, python)


def test_ci_rejects_fixture_outside_ephemeral_runner_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    temporary = tmp_path / "runner"
    temporary.mkdir(mode=0o700)
    _hosted(monkeypatch, temporary)
    root, fixture, python = _local(tmp_path)
    with pytest.raises(RuntimeError, match="operator roots"):
        _fault_command(root, fixture, python)


def test_ci_rejects_interpreter_symlink_outside_private_native_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _hosted(monkeypatch, tmp_path)
    root, fixture, python = _local(tmp_path)
    outsider = tmp_path / "foreign-python"
    outsider.write_bytes(b"foreign")
    Path(python).unlink()
    Path(python).symlink_to(outsider)
    with pytest.raises(RuntimeError, match="foreign interpreters"):
        _fault_command(root, fixture, python)


def test_ci_rejects_nonprivate_native_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _hosted(monkeypatch, tmp_path)
    root, fixture, python = _local(tmp_path)
    Path(str(fixture["native_root"])).chmod(0o755)
    with pytest.raises(RuntimeError, match="operator roots"):
        _fault_command(root, fixture, python)
