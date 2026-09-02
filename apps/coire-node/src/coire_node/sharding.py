"""Persisted ownership of bare two-rank MLX/JACCL process groups."""

from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from coire_core.models import (
    ShardCapabilityResult,
    ShardGroupCommand,
    ShardGroupState,
    ShardGroupStatus,
    ShardingMode,
)
from coire_core.settings import Settings
from coire_node.store import Store, write_atomic_json

GROUPS_FILE = "shard-groups.json"


def build_shard_argv(
    command: ShardGroupCommand, *, model_path: str, hostfile_path: str
) -> list[str]:
    """Build fixed bare-engine argv; request bodies cannot supply paths or flags."""
    prefix = os.environ.get("COIRE_SHARD_COMMAND")
    launcher = prefix.split(os.pathsep) if prefix else [sys.executable, "-m", "mlx.launch"]
    argv = [
        *launcher,
        "--hostfile",
        hostfile_path,
        "--backend",
        "jaccl" if command.mode is ShardingMode.TENSOR_PARALLEL else "ring",
        "--env",
        "MLX_METAL_FAST_SYNCH=1",
        "--",
        sys.executable,
        "-m",
        "mlx_lm.server",
        "--model",
        model_path,
        "--host",
        command.ranks[0].host,
        "--port",
        str(command.ranks[0].port),
        "--log-level",
        "INFO",
    ]
    return argv


class ShardGroupManager:
    def __init__(self, settings: Settings, store: Store) -> None:
        self._settings = settings
        self._store = store
        self._groups: dict[uuid.UUID, ShardGroupStatus] = {}
        self._processes: dict[uuid.UUID, subprocess.Popen[bytes]] = {}
        self._commands: dict[uuid.UUID, uuid.UUID] = {}
        self._model_paths: dict[uuid.UUID, str] = {}
        self._state_file = Path(settings.node_state_dir) / GROUPS_FILE
        self._lock = threading.RLock()
        self._adopt()

    def capability(self, slug: str, mode: ShardingMode) -> ShardCapabilityResult:
        """Ask the installed MLX model object, not its repository name, about sharding."""
        model_path = self._store.path_for(slug)
        config_path = model_path / "config.json"
        if not model_path.is_dir() or not config_path.is_file():
            raise FileNotFoundError(slug)
        config = json.loads(config_path.read_text())
        architectures = config.get("architectures")
        architecture = (
            str(architectures[0])
            if isinstance(architectures, list) and architectures
            else str(config.get("model_type") or "unknown")
        )
        if os.environ.get("COIRE_TEST_FAKE_VALIDATION") == "1":
            supported = True
        else:
            from mlx_lm import load

            loaded = load(str(model_path), lazy=True)
            model = loaded[0]
            supported = (
                hasattr(model, "shard")
                if mode is ShardingMode.TENSOR_PARALLEL
                else hasattr(getattr(model, "model", None), "pipeline")
            )
        return ShardCapabilityResult(
            architecture=architecture,
            mode=mode,
            supported=supported,
        )

    def prepare(self, command: ShardGroupCommand) -> ShardGroupStatus:
        with self._lock:
            existing_id = self._commands.get(command.command_id)
            if existing_id is not None:
                return self._groups[existing_id]
            if command.group_id in self._groups:
                raise ValueError("group exists with a different command identity")
            # Store resolves a registry slug beneath the managed model root.
            model_path = self._store.path_for(command.slug)
            if not model_path.is_dir():
                raise FileNotFoundError(command.slug)
            hostfile = self._verified_hostfile(command)
            status = ShardGroupStatus(
                group_id=command.group_id,
                instance_id=command.instance_id,
                mode=command.mode,
                state=ShardGroupState.PREPARING,
                ranks=command.ranks,
                started_at=datetime.now(UTC),
            )
            self._groups[command.group_id] = status
            self._model_paths[command.group_id] = str(model_path)
            self._commands[command.command_id] = command.group_id
            self._persist()
            # Only rank zero owns mlx.launch; the launcher starts the remote rank over the
            # declared data fabric. Rank one records the command for conjunction health.
            local_rank = next(
                rank.rank for rank in command.ranks if rank.node_name == self._settings.node_name
            )
            if local_rank == 0:
                proc = subprocess.Popen(
                    build_shard_argv(
                        command, model_path=str(model_path), hostfile_path=str(hostfile)
                    ),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                    env={**os.environ, "HF_HUB_OFFLINE": "1"},
                )
                self._processes[command.group_id] = proc
                command.ranks[0].pid = proc.pid
                command.ranks[0].process_create_time = datetime.now(UTC).timestamp()
            status.state = ShardGroupState.STARTING
            self._persist()
            return status

    def get(self, group_id: uuid.UUID) -> ShardGroupStatus | None:
        with self._lock:
            status = self._groups.get(group_id)
            proc = self._processes.get(group_id)
            if (
                status
                and proc
                and proc.poll() is not None
                and status.state
                not in {
                    ShardGroupState.STOPPED,
                    ShardGroupState.FAILED,
                }
            ):
                status.state = ShardGroupState.FAILED
                status.state_reason = f"rank process exited with {proc.returncode}"
                status.stopped_at = datetime.now(UTC)
                self._persist()
            return status

    def model_path(self, group_id: uuid.UUID) -> str | None:
        with self._lock:
            return self._model_paths.get(group_id)

    def stop(self, group_id: uuid.UUID) -> ShardGroupStatus | None:
        with self._lock:
            status = self._groups.get(group_id)
            if status is None:
                return None
            proc = self._processes.get(group_id)
            if proc and proc.poll() is None:
                status.state = ShardGroupState.STOPPING
                self._persist()
                with suppress(ProcessLookupError):
                    os.killpg(proc.pid, signal.SIGTERM)
                try:
                    proc.wait(timeout=min(30, self._settings.sharding_start_timeout_s))
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired as exc:
                        status.state = ShardGroupState.FAILED
                        status.state_reason = "rank process group did not stop"
                        self._persist()
                        raise RuntimeError(status.state_reason) from exc
            status.state = ShardGroupState.STOPPED
            status.stopped_at = datetime.now(UTC)
            self._persist()
            return status

    def mark_ready(self, group_id: uuid.UUID) -> ShardGroupStatus | None:
        with self._lock:
            status = self.get(group_id)
            if status is None:
                return None
            if status.state not in {ShardGroupState.STARTING, ShardGroupState.READY}:
                raise ValueError("group cannot become ready from its current state")
            if (
                self._settings.node_name == "coire-edge-a"
                and status.state is not ShardGroupState.READY
            ):
                self._wait_for_generation(status)
            status.state = ShardGroupState.READY
            self._persist()
            return status

    def _wait_for_generation(self, status: ShardGroupStatus) -> None:
        deadline = time.monotonic() + self._settings.sharding_start_timeout_s
        rank_zero = next(rank for rank in status.ranks if rank.rank == 0)
        payload = {
            "model": self._model_paths[status.group_id],
            "messages": [{"role": "user", "content": "0"}],
            "max_tokens": 1,
            "temperature": 0,
        }
        last_error = "not ready"
        while time.monotonic() < deadline:
            try:
                response = httpx.post(
                    f"http://{rank_zero.host}:{rank_zero.port}/v1/chat/completions",
                    json=payload,
                    timeout=5,
                )
                if response.is_success:
                    return
                last_error = f"HTTP {response.status_code}"
            except httpx.HTTPError as exc:
                last_error = type(exc).__name__
            time.sleep(self._settings.node_engine_health_interval_s)
        raise ValueError(f"distributed first generation did not become ready: {last_error}")

    def _verified_hostfile(self, command: ShardGroupCommand) -> Path:
        configured = (
            self._settings.sharding_jaccl_hostfile
            if command.mode is ShardingMode.TENSOR_PARALLEL
            else self._settings.sharding_ring_hostfile
        )
        path = Path(configured)
        encoded = path.read_bytes()
        if hashlib.sha256(encoded).hexdigest() != command.hostfile_sha256:
            raise ValueError("hostfile digest does not match scheduler command")
        return path

    def _persist(self) -> None:
        records = [item.model_dump(mode="json") for item in self._groups.values()]
        write_atomic_json(self._state_file, records)

    def _adopt(self) -> None:
        try:
            records: list[dict[str, Any]] = json.loads(self._state_file.read_text())
        except (OSError, json.JSONDecodeError):
            return
        for record in records:
            status = ShardGroupStatus.model_validate(record)
            if status.state in {
                ShardGroupState.PREPARING,
                ShardGroupState.STARTING,
                ShardGroupState.READY,
            }:
                status.state = ShardGroupState.FAILED
                status.state_reason = "agent restarted without a verifiable rank process identity"
                status.stopped_at = datetime.now(UTC)
            self._groups[status.group_id] = status
