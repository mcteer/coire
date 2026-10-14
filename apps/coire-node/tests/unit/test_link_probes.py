from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from coire_core.models import LinkProbeCommand, ProbeOutcome, ProbeTransport
from coire_core.settings import Settings
from coire_node.link_probes import LinkProbeRunner, build_probe_argv


def command() -> LinkProbeCommand:
    return LinkProbeCommand(
        command_id=uuid.uuid4(), transport=ProbeTransport.JACCL, hostfile_sha256="0" * 64
    )


def test_probe_argv_is_fixed_two_host_launcher(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("COIRE_PROBE_COMMAND", raising=False)
    argv = build_probe_argv(command(), Path("/state/generated.json"))
    assert argv[1:3] == ["-c", "from mlx._distributed_utils.launch import main; main()"]
    assert "-n" not in argv
    assert argv.count("jaccl") == 2
    assert "coire_node.link_probe_worker" in argv


def test_probe_parser_requires_both_ranks() -> None:
    runner = LinkProbeRunner(Settings(_secrets_dir="/nonexistent"))  # type: ignore[call-arg]
    line = (
        b'COIRE_PROBE_RESULT {"rank":0,"bandwidth_bytes_per_second":100,'
        b'"latency_ms":5000,"os_version":"26","engine_version":"1"}\n'
    )
    assert runner._observation(ProbeTransport.JACCL, 0, line).outcome is ProbeOutcome.FAILED
    both = line + line.replace(b'"rank":0', b'"rank":1')
    result = runner._observation(ProbeTransport.JACCL, 0, both)
    assert result.outcome is ProbeOutcome.SUCCEEDED
    assert result.latency_ms == 5000  # visible but never an admission threshold


def test_managed_probe_resolves_each_studios_selected_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("COIRE_PROBE_COMMAND", raising=False)
    argv = build_probe_argv(
        command(),
        Path("/state/generated.json"),
        Settings(upgrades_enabled=True, upgrade_environment_root="/opt/coire/envs"),
    )
    assert argv[argv.index("--") + 1] == "/opt/coire/envs/current/bin/python3"
    assert argv[-3:] == ["--managed-runtime", "--environment-root", "/opt/coire/envs"]
    assert "NODE_NAME=coire-edge-a" not in argv


def test_probe_parser_accepts_rank_records_concatenated_by_launcher() -> None:
    runner = LinkProbeRunner(Settings(_secrets_dir="/nonexistent"))  # type: ignore[call-arg]
    output = (
        b'launcher: COIRE_PROBE_RESULT {"rank":0,"bandwidth_bytes_per_second":100,'
        b'"latency_ms":1,"os_version":"26","engine_version":"1"}'
        b'COIRE_PROBE_RESULT {"rank":1,"bandwidth_bytes_per_second":90,'
        b'"latency_ms":2,"os_version":"26","engine_version":"1"}'
    )
    result = runner._observation(ProbeTransport.JACCL, 0, output)
    assert result.outcome is ProbeOutcome.SUCCEEDED
    assert result.bandwidth_bytes_per_second == 90


def test_remote_probe_verifies_its_own_identity_before_native_import(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import platform
    import sys

    from coire_core.errors import UpgradeUnavailable
    from coire_node import link_probe_worker, upgrade_runtime

    monkeypatch.setattr(platform, "node", lambda: "coire-edge-b.lab")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "probe",
            "--backend",
            "jaccl",
            "--managed-runtime",
            "--environment-root",
            "/opt/coire/envs",
        ],
    )
    seen: list[Settings] = []

    def verify(settings: Settings) -> str:
        seen.append(settings)
        raise UpgradeUnavailable("sealed graph intentionally absent in CPU test")

    monkeypatch.setattr(upgrade_runtime, "running_runtime_sha256", verify)
    with pytest.raises(UpgradeUnavailable):
        link_probe_worker.main()
    assert seen[0].node_name == "coire-edge-b"
    assert seen[0].upgrades_enabled
    assert seen[0].upgrade_environment_root == "/opt/coire/envs"


def test_probe_failure_is_bounded_and_does_not_expose_process_output() -> None:
    runner = LinkProbeRunner(Settings(_secrets_dir="/nonexistent"))  # type: ignore[call-arg]
    result = runner._observation(ProbeTransport.JACCL, 1, b"private diagnostic" * 1000)
    assert result.outcome is ProbeOutcome.FAILED
    assert result.reason == "probe incomplete: exit=1, observed_ranks=[]"


def test_confirmed_launcher_timeout_is_failed_and_replay_does_not_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import hashlib
    import subprocess
    from unittest.mock import Mock

    hostfile = tmp_path / "hostfile.json"
    hostfile.write_bytes(b"private hostfile")
    value = command().model_copy(
        update={
            "hostfile_sha256": hashlib.sha256(hostfile.read_bytes()).hexdigest(),
        }
    )
    legacy = Mock(side_effect=subprocess.TimeoutExpired("launcher", 1))
    cleaned = Mock(return_value=subprocess.CompletedProcess(["launcher"], -15, b""))
    monkeypatch.setattr(subprocess, "run", legacy)
    monkeypatch.setattr("coire_node.link_probes.run_collective_launcher", cleaned, raising=False)
    runner = LinkProbeRunner(
        Settings(node_name="coire-edge-a", sharding_jaccl_hostfile=str(hostfile))
    )
    result = runner.run(value)
    assert result.outcome is ProbeOutcome.FAILED
    assert runner.run(value) == result
    assert cleaned.call_count == 1
    legacy.assert_not_called()


def test_interrupted_collective_cannot_respawn_or_inherit_control_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import hashlib
    import subprocess
    from datetime import UTC, datetime, timedelta
    from unittest.mock import Mock

    hostfile = tmp_path / "hostfile.json"
    hostfile.write_bytes(b"private test hostfile")
    settings = Settings(node_name="coire-edge-a", sharding_jaccl_hostfile=str(hostfile))
    value = command().model_copy(
        update={"hostfile_sha256": hashlib.sha256(hostfile.read_bytes()).hexdigest()}
    )
    dispatched = Mock(side_effect=subprocess.TimeoutExpired("bare launcher", 1))
    monkeypatch.setattr("coire_node.link_probes.run_collective_launcher", dispatched)
    monkeypatch.setenv("NODE_TOKEN", "private-control-credential")
    monkeypatch.setenv("DATABASE_URL", "private-database-url")
    runner = LinkProbeRunner(settings)
    deadline = datetime.now(UTC) + timedelta(seconds=1)
    with pytest.raises(subprocess.TimeoutExpired):
        runner.run(value, deadline_at=deadline)
    with pytest.raises(ValueError, match="unresolved"):
        runner.run(value, deadline_at=deadline)
    assert dispatched.call_count == 1
    environment = dispatched.call_args.kwargs["env"]
    assert "NODE_TOKEN" not in environment
    assert "DATABASE_URL" not in environment
    assert environment["HF_HUB_OFFLINE"] == "1"
    assert 0 < dispatched.call_args.kwargs["timeout"] <= 1


def test_private_launcher_diagnostic_is_bounded_redacted_from_response_and_not_replayed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import hashlib
    import logging
    import subprocess
    from unittest.mock import Mock

    from coire_core.models.probe_diagnostics import ProbeLauncherDiagnostic
    from coire_core.upgrade_journal import PrivateUpgradeStore

    hostfile = tmp_path / "hostfile.json"
    hostfile.write_bytes(b"owned hostfile")
    settings = Settings(node_name="coire-edge-a", sharding_jaccl_hostfile=str(hostfile))
    value = command().model_copy(
        update={"hostfile_sha256": hashlib.sha256(hostfile.read_bytes()).hexdigest()}
    )
    output = b"private traceback " * 1000
    launched = Mock(return_value=subprocess.CompletedProcess(["launcher"], 0, output))
    monkeypatch.setattr("coire_node.link_probes.run_collective_launcher", launched)
    store = PrivateUpgradeStore(tmp_path / "private-diagnostics")
    runner = LinkProbeRunner(settings, diagnostic_store=store)
    with caplog.at_level(logging.INFO, logger="coire_node.link_probes"):
        result = runner.run(value)
    assert result.outcome is ProbeOutcome.FAILED
    assert "private traceback" not in result.model_dump_json()
    assert "private traceback" not in caplog.text
    diagnostic = store.read(str(value.command_id) + ".json", ProbeLauncherDiagnostic)
    assert diagnostic is not None
    assert diagnostic.output_bytes == len(output)
    assert diagnostic.output_sha256 == hashlib.sha256(output).hexdigest()
    assert len(diagnostic.output_tail) <= 8192 and diagnostic.truncated
    assert store.path(str(value.command_id) + ".json").stat().st_mode & 0o077 == 0
    assert runner.run(value) == result
    with pytest.raises(ValueError, match="cannot repeat"):
        LinkProbeRunner(settings, diagnostic_store=store).run(value)
    assert launched.call_count == 1
