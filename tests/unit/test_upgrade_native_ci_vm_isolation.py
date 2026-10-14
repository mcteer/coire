"""Native CI VM provisioning cannot discover or mount an operator environment."""

import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml
from integration.provision_upgrade_ci_vm import (
    configuration,
    require_control_plane_prototype,
    require_hosted_mac,
    studio_configuration,
)


def _hosted(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    for key, value in {
        "GITHUB_ACTIONS": "true",
        "RUNNER_ENVIRONMENT": "github-hosted",
        "RUNNER_OS": "macOS",
        "RUNNER_ARCH": "ARM64",
        "RUNNER_TEMP": str(root),
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr("integration.provision_upgrade_ci_vm.platform.system", lambda: "Darwin")
    monkeypatch.setattr("integration.provision_upgrade_ci_vm.platform.machine", lambda: "arm64")


def test_hosted_vm_requires_ephemeral_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _hosted(monkeypatch, tmp_path)
    require_hosted_mac(tmp_path / "owned-vm")
    with pytest.raises(RuntimeError, match="ephemeral"):
        require_hosted_mac(tmp_path.parent / "operator-vm")


@pytest.mark.parametrize("linked", [False, True])
def test_hosted_flags_cannot_authorize_an_installed_operator_node(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, linked: bool
) -> None:
    _hosted(monkeypatch, tmp_path)
    installed = tmp_path / "operator-current"
    if linked:
        installed.symlink_to(tmp_path / "absent-runtime")
    else:
        installed.write_text("operator installation")
    monkeypatch.setattr(
        "integration.provision_upgrade_ci_vm.installed_native_paths", lambda: (installed,)
    )
    with pytest.raises(RuntimeError, match="installed operator node"):
        require_hosted_mac(tmp_path / "owned-vm")


@pytest.mark.parametrize(
    "key,value",
    [("RUNNER_ENVIRONMENT", "self-hosted"), ("RUNNER_ARCH", "X64"), ("RUNNER_OS", "Linux")],
)
def test_vm_rejects_other_runners(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, key: str, value: str
) -> None:
    _hosted(monkeypatch, tmp_path)
    monkeypatch.setenv(key, value)
    with pytest.raises(RuntimeError, match="hosted Apple Silicon"):
        require_hosted_mac(tmp_path / "owned-vm")


def test_ci_cannot_bypass_isolation_with_prototype_flag(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _hosted(monkeypatch, tmp_path)
    with pytest.raises(RuntimeError, match="cannot bypass"):
        require_control_plane_prototype(Path("/private/tmp/coire-019-native-ci-prototype-bypass"))


def test_vm_configuration_has_no_host_mounts_or_automatic_network_forwarding() -> None:
    config = yaml.safe_load(configuration())
    assert config["mounts"] == []
    assert config["ssh"]["loadDotSSHPubKeys"] is False
    assert config["containerd"] == {"system": False, "user": False}
    forwards = config["portForwards"]
    assert len(forwards) == 2
    assert forwards[0] == {
        "guestSocket": "/run/docker.sock",
        "hostSocket": "{{.Dir}}/sock/docker.sock",
    }
    assert forwards[1] == {
        "guestPortRange": [1, 65535],
        "guestIP": "0.0.0.0",
        "guestIPMustBeZero": False,
        "proto": "any",
        "ignore": True,
    }
    assert config["memory"] == "3GiB"
    assert config["images"][0]["digest"].startswith("sha256:")


def test_studio_vm_mounts_only_owned_harness_workspaces(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("integration.provision_upgrade_ci_vm.require_hosted_mac", lambda _: None)
    workspaces = tuple(
        tmp_path / "coire-019-acceptance" / ("native-upgrade-lab-" + node) / "workspaces"
        for node in ("a", "b")
    )
    for workspace in workspaces:
        workspace.mkdir(parents=True, mode=0o700)
    config = yaml.safe_load(studio_configuration((workspaces[0], workspaces[1])))
    assert config["mounts"] == [
        {"location": str(path), "mountPoint": str(path), "writable": True} for path in workspaces
    ]
    assert config["mountType"] == "9p"
    assert config["ssh"]["loadDotSSHPubKeys"] is False
    assert config["portForwards"] == yaml.safe_load(configuration())["portForwards"]


@pytest.mark.parametrize("kind", ["models", "linked", "public"])
def test_studio_vm_refuses_model_mounts_aliases_and_public_workspaces(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, kind: str
) -> None:
    monkeypatch.setattr("integration.provision_upgrade_ci_vm.require_hosted_mac", lambda _: None)
    base = tmp_path / "coire-019-acceptance"
    first = base / "native-upgrade-lab-a" / "workspaces"
    first.mkdir(parents=True, mode=0o700)
    second = base / "native-upgrade-lab-b" / ("models" if kind == "models" else "workspaces")
    second.parent.mkdir(parents=True)
    if kind == "linked":
        second.symlink_to(first)
    else:
        mode = 0o755 if kind == "public" else 0o700
        second.mkdir(mode=mode)
        second.chmod(mode)  # mkdir's mode is filtered by the invoking shell's umask.
    with pytest.raises(RuntimeError, match="owned private harness workspace"):
        studio_configuration((first, second))


@pytest.mark.parametrize("version,expected", [("29.1.3", 0), ("29.1.2", 1)])
def test_version_probe_checks_streamed_script_without_consuming_its_stdin(
    tmp_path: Path, version: str, expected: int
) -> None:
    """Lima streams the probe to sh; command-string execution missed this defect."""
    commands = {
        "sudo": '#!/bin/sh\nexec "$@"\n',
        "docker": (
            '#!/bin/sh\nif [ "$1" = version ]; then\n'
            + " printf '%s\\n' "
            + "'"
            + json.dumps({"Server": {"Version": version}})
            + "'"
            + "\nelse\n printf '%s\\n' '2.40.3+ds1-0ubuntu1~24.04.1'\nfi\n"
        ),
        "id": "#!/bin/sh\nprintf '%s\\n' 65532\n",
    }
    for name, content in commands.items():
        path = tmp_path / name
        path.write_text(content)
        path.chmod(0o700)
    probe = yaml.safe_load(configuration())["probes"][0]["script"]
    result = subprocess.run(
        ["/bin/sh"],
        input=probe,
        text=True,
        capture_output=True,
        env={**os.environ, "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"]},
        timeout=5,
    )
    assert result.returncode == expected, result.stderr
    assert "{{" not in probe, "Lima must not expand Docker's Go-template placeholders"
