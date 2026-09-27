"""Explicit, bounded JACCL/ring probe execution owned by rank-zero coire-node."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from coire_core.models import LinkObservation, LinkProbeCommand, ProbeOutcome, ProbeTransport
from coire_core.settings import Settings

MARKER = "COIRE_PROBE_RESULT "


def build_probe_argv(command: LinkProbeCommand, hostfile: Path) -> list[str]:
    override = os.environ.get("COIRE_PROBE_COMMAND")
    launcher = override.split(os.pathsep) if override else [sys.executable, "-m", "mlx.launch"]
    return [
        *launcher,
        "--backend",
        command.transport.value,
        "--hostfile",
        str(hostfile),
        "--env",
        "MLX_METAL_FAST_SYNCH=1",
        "--",
        sys.executable,
        "-m",
        "coire_node.link_probe_worker",
        "--backend",
        command.transport.value,
    ]


class LinkProbeRunner:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._results: dict[uuid.UUID, LinkObservation] = {}

    def run(self, command: LinkProbeCommand) -> LinkObservation:
        if command.command_id in self._results:
            return self._results[command.command_id]
        if self._settings.node_name != "coire-edge-a":
            raise ValueError("only declared rank zero may coordinate a link probe")
        path = Path(
            self._settings.sharding_jaccl_hostfile
            if command.transport is ProbeTransport.JACCL
            else self._settings.sharding_ring_hostfile
        )
        payload = path.read_bytes()
        if hashlib.sha256(payload).hexdigest() != command.hostfile_sha256:
            raise ValueError("hostfile digest does not match probe command")
        completed = subprocess.run(
            build_probe_argv(command, path),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=self._settings.sharding_start_timeout_s,
            check=False,
            env={**os.environ, "HF_HUB_OFFLINE": "1"},
        )
        result = self._observation(command.transport, completed.returncode, completed.stdout)
        self._results[command.command_id] = result
        return result

    def _observation(
        self, transport: ProbeTransport, returncode: int, output: bytes
    ) -> LinkObservation:
        records: dict[int, dict[str, Any]] = {}
        decoded = output.decode(errors="replace")
        decoder = json.JSONDecoder()
        offset = 0
        while (marker_at := decoded.find(MARKER, offset)) >= 0:
            payload_at = marker_at + len(MARKER)
            try:
                record, consumed = decoder.raw_decode(decoded[payload_at:])
            except json.JSONDecodeError:
                offset = payload_at
                continue
            if isinstance(record, dict) and "rank" in record:
                records[int(record["rank"])] = record
            offset = payload_at + consumed
        succeeded = returncode == 0 and set(records) == {0, 1}
        first, second = records.get(0, {}), records.get(1, {})
        return LinkObservation(
            id=uuid.uuid4(),
            node_a="coire-edge-a",
            node_b="coire-edge-b",
            transport=transport,
            outcome=ProbeOutcome.SUCCEEDED if succeeded else ProbeOutcome.FAILED,
            bandwidth_bytes_per_second=(
                min(
                    int(first["bandwidth_bytes_per_second"]),
                    int(second["bandwidth_bytes_per_second"]),
                )
                if succeeded
                else None
            ),
            latency_ms=(
                max(float(first["latency_ms"]), float(second["latency_ms"])) if succeeded else None
            ),
            os_version_a=str(first.get("os_version", "unknown")),
            os_version_b=str(second.get("os_version", "unknown")),
            engine_version=str(first.get("engine_version", "unknown")),
            reason=None
            if succeeded
            else f"probe exited {returncode}: {output[-512:].decode(errors='replace')}",
            observed_at=datetime.now(UTC),
        )
