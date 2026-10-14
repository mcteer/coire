"""Explicit, bounded JACCL/ring probe execution owned by rank-zero coire-node."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from coire_core.models import LinkObservation, LinkProbeCommand, ProbeOutcome, ProbeTransport
from coire_core.models.probe_diagnostics import ProbeLauncherDiagnostic
from coire_core.settings import Settings
from coire_core.upgrade_journal import PrivateUpgradeStore
from coire_node.collective_process import run_collective_launcher
from coire_node.mlx_launch import launcher_argv

MARKER = "COIRE_PROBE_RESULT "
logger = logging.getLogger(__name__)


def build_probe_argv(
    command: LinkProbeCommand, hostfile: Path, settings: Settings | None = None
) -> list[str]:
    override = os.environ.get("COIRE_PROBE_COMMAND")
    launcher = override.split(os.pathsep) if override else launcher_argv(sys.executable)
    managed = settings is not None and settings.upgrades_enabled
    environment_root = Path(settings.upgrade_environment_root) if settings else None
    if managed and (environment_root is None or not environment_root.is_absolute()):
        raise ValueError("managed collective environment root must be absolute")
    worker_python = (
        str(environment_root / "current/bin/python3")
        if managed and environment_root is not None
        else sys.executable
    )
    argv = [
        *launcher,
        "--backend",
        command.transport.value,
        "--hostfile",
        str(hostfile),
        "--env",
        "MLX_METAL_FAST_SYNCH=1",
        "--",
        worker_python,
        "-m",
        "coire_node.link_probe_worker",
        "--backend",
        command.transport.value,
    ]
    if managed:
        argv.extend(["--managed-runtime", "--environment-root", str(environment_root)])
    return argv


class LinkProbeRunner:
    def __init__(
        self, settings: Settings, *, diagnostic_store: PrivateUpgradeStore | None = None
    ) -> None:
        self._settings = settings
        self._diagnostic_store = diagnostic_store
        self._results: dict[uuid.UUID, LinkObservation] = {}
        self._commands: dict[uuid.UUID, LinkProbeCommand] = {}

    def run(
        self,
        command: LinkProbeCommand,
        *,
        deadline_at: datetime | None = None,
        authority: Callable[[], None] | None = None,
    ) -> LinkObservation:
        if deadline_at is not None and datetime.now(UTC) >= deadline_at:
            raise ValueError("collective original deadline elapsed")
        previous = self._commands.get(command.command_id)
        if previous is not None and previous != command:
            raise ValueError("collective command replay differs")
        if command.command_id in self._results:
            return self._results[command.command_id]
        if (
            self._diagnostic_store is not None
            and self._diagnostic_store.read(
                str(command.command_id) + ".json", ProbeLauncherDiagnostic
            )
            is not None
        ):
            raise ValueError("collective has retained diagnostics; dispatch cannot repeat")
        if previous is not None:
            raise ValueError("previous collective dispatch remains unresolved")
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
        remaining = (
            (deadline_at - datetime.now(UTC)).total_seconds()
            if deadline_at is not None
            else self._settings.sharding_start_timeout_s
        )
        if remaining <= 0:
            raise ValueError("collective original deadline elapsed")
        self._commands[command.command_id] = command
        environment = {
            key: value
            for key, value in os.environ.items()
            if not key.upper().endswith(("_TOKEN", "_TOKENS", "_KEY", "_SECRET", "_PASSWORD"))
            and key
            not in {
                "DATABASE_URL",
                "REDIS_URL",
                "PYTHONPATH",
                "PYTHONHOME",
                "DYLD_INSERT_LIBRARIES",
                "LD_PRELOAD",
                "GOOGLE_APPLICATION_CREDENTIALS",
            }
        }
        environment.update({"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"})
        argv = build_probe_argv(command, path, self._settings)
        completed = (
            run_collective_launcher(
                argv,
                timeout=min(remaining, self._settings.sharding_start_timeout_s),
                env=environment,
            )
            if authority is None
            else run_collective_launcher(
                argv,
                timeout=min(remaining, self._settings.sharding_start_timeout_s),
                env=environment,
                authority=authority,
            )
        )
        if self._diagnostic_store is not None:
            diagnostic = ProbeLauncherDiagnostic(
                command=command,
                argv_sha256=hashlib.sha256(
                    json.dumps(argv, separators=(",", ":")).encode()
                ).hexdigest(),
                returncode=completed.returncode,
                output_sha256=hashlib.sha256(completed.stdout).hexdigest(),
                output_bytes=len(completed.stdout),
                output_tail=completed.stdout[-8192:].decode(errors="replace"),
                truncated=len(completed.stdout) > 8192,
                observed_at=datetime.now(UTC),
            )
            self._diagnostic_store.write(
                str(command.command_id) + ".json", diagnostic, replace=False
            )
            logger.info(
                "collective launcher diagnostic retained",
                extra={
                    "command_id": str(command.command_id),
                    "returncode": diagnostic.returncode,
                    "output_bytes": diagnostic.output_bytes,
                    "output_sha256": diagnostic.output_sha256,
                },
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
            runtime_sha256_a=first.get("runtime_sha256"),
            runtime_sha256_b=second.get("runtime_sha256"),
            reason=None
            if succeeded
            else f"probe incomplete: exit={returncode}, observed_ranks={sorted(records)}",
            observed_at=datetime.now(UTC),
        )
