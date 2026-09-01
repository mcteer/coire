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
    assert argv[1:3] == ["-m", "mlx.launch"]
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
