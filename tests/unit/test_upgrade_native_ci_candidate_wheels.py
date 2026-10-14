"""Native failure tests install real changed code with valid wheel records."""

import base64
import csv
import hashlib
import io
from pathlib import Path
from typing import Literal
from zipfile import ZipFile

import pytest
from integration.upgrade_native_ci_wheels import (
    candidate_wheel,
    initialize_source_bootstrap,
    install_initial_source,
)

from coire_core.models.upgrades import UpgradeReleaseRegister
from coire_core.upgrade_journal import PrivateUpgradeStore


@pytest.mark.parametrize("kind", ["healthy", "broken"])
def test_candidate_changes_executed_startup_and_rehashes_every_record(
    tmp_path: Path, kind: Literal["healthy", "broken"]
) -> None:
    source = tmp_path / "source.whl"
    startup = (
        "class Settings: pass\n"
        "class Logger:\n"
        "    def info(self, message): print(message)\n"
        "logger = Logger()\n"
        "def run():\n"
        "    settings = Settings()\n"
        "    return settings\n"
    )
    record = "coire_node-0.2.0.dist-info/RECORD"
    with ZipFile(source, "w") as wheel:
        wheel.writestr("coire_node/__main__.py", startup)
        wheel.writestr("coire_node-0.2.0.dist-info/METADATA", "Version: 0.2.0\n")
        wheel.writestr(record, "")
    output = tmp_path / "candidate.whl"
    candidate_wheel(source, output, kind)
    with ZipFile(output) as wheel:
        modified = wheel.read("coire_node/__main__.py")
        assert modified != startup.encode()
        for name, digest, size in csv.reader(io.StringIO(wheel.read(record).decode())):
            if name == record:
                assert digest == size == ""
                continue
            content = wheel.read(name)
            expected = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b"=")
            assert digest == "sha256=" + expected.decode()
            assert size == str(len(content))
        namespace: dict[str, object] = {}
        exec(modified, namespace)
        run = namespace["run"]
        assert callable(run)
        if kind == "broken":
            with pytest.raises(RuntimeError, match="broken candidate"):
                run()
        else:
            assert run() is not None
    assert source.read_bytes() != output.read_bytes()
    with pytest.raises(RuntimeError, match="fresh"):
        candidate_wheel(source, output, kind)


@pytest.mark.parametrize("name", ["envs/current", "upgrade-bootstrap"])
@pytest.mark.parametrize("dangling", [False, True])
def test_initial_install_refuses_existing_authority_before_running_installer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, dangling: bool
) -> None:
    monkeypatch.setattr("integration.upgrade_native_ci_wheels.require_hosted_mac", lambda _: None)
    marker = tmp_path / name
    marker.parent.mkdir(parents=True, exist_ok=True)
    if dangling:
        marker.symlink_to(tmp_path / "missing-authority")
    else:
        marker.mkdir()
    calls: list[object] = []
    monkeypatch.setattr(
        "integration.upgrade_native_ci_wheels.subprocess.run",
        lambda *args, **kwargs: calls.append(args),
    )
    with pytest.raises(RuntimeError, match="cannot replace"):
        install_initial_source(tmp_path / "repo", tmp_path, tmp_path / "wheels")
    assert calls == []


@pytest.mark.parametrize("dangling", [False, True])
def test_ci_bootstrap_cannot_replace_existing_recovery_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dangling: bool
) -> None:
    monkeypatch.setattr("integration.upgrade_native_ci_wheels.require_hosted_mac", lambda _: None)
    marker = tmp_path / "upgrade-bootstrap"
    if dangling:
        marker.symlink_to(tmp_path / "absent")
    else:
        marker.mkdir(mode=0o700)
    calls = []
    monkeypatch.setattr(
        "integration.upgrade_native_ci_wheels.subprocess.run",
        lambda *a, **kw: calls.append((a, kw)),
    )
    request = UpgradeReleaseRegister(prepared_artifact_id="source-ci", expected_sha256="a" * 64)
    with pytest.raises(RuntimeError, match="cannot replace"):
        initialize_source_bootstrap(tmp_path / "repo", tmp_path, "coire-edge-a", request)
    assert calls == []
    assert not (tmp_path / "fixture-records").exists()


def test_ci_bootstrap_invokes_ordinary_producer_and_never_installs_global_services(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("integration.upgrade_native_ci_wheels.require_hosted_mac", lambda _: None)
    calls = []
    monkeypatch.setattr(
        "integration.upgrade_native_ci_wheels.subprocess.run",
        lambda *a, **kw: calls.append((a, kw)),
    )
    request = UpgradeReleaseRegister(prepared_artifact_id="source-ci", expected_sha256="a" * 64)
    repo = tmp_path / "repo"
    initialize_source_bootstrap(repo, tmp_path, "coire-edge-b", request)
    store = PrivateUpgradeStore(tmp_path / "fixture-records", create=False)
    assert store.read("initial-bootstrap-request.json", UpgradeReleaseRegister) == request
    ((arguments, options),) = calls
    assert arguments[0] == [
        str(tmp_path / "envs/current/bin/python3"),
        "-I",
        "-B",
        "-m",
        "coire_node.upgrade_bootstrap",
    ]
    assert options["env"]["NODE_NAME"] == "coire-edge-b"
    assert options["env"]["UPGRADE_BOOTSTRAP_ROOT"] == str(tmp_path / "upgrade-bootstrap")
    assert options["env"]["UPGRADE_PREPARED_DIR"] == str(tmp_path / "upgrades/prepared")
    assert options["stdin"].name == str(store.path("initial-bootstrap-request.json"))
    assert options["check"] is True
    assert not (tmp_path / "upgrade-bootstrap").exists()
    with pytest.raises(RuntimeError, match="already retained"):
        initialize_source_bootstrap(repo, tmp_path, "coire-edge-b", request)
    assert len(calls) == 1
