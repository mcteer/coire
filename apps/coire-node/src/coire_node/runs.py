"""Node-owned hardened lifecycle for ephemeral agent containers."""

from __future__ import annotations

import asyncio
import io
import json
import re
import tarfile
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from coire_core.models.runs import (
    RunCollectedResult,
    RunContainerCreate,
    RunContainerObservation,
    RunContainerStatus,
    RunLogChunk,
    RunResourceUsage,
)
from coire_core.settings import Settings
from coire_node.docker_api import DockerAPI, DockerAPIError

RUN_LABEL = "com.coire.agent-run"
MANAGED_LABEL = "com.coire.managed"
RESULT_PATH = "/workspace/.coire/result.json"
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


class RunRuntimeError(RuntimeError):
    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        super().__init__(detail)


class RunManager:
    def __init__(self, settings: Settings, docker: DockerAPI) -> None:
        self.settings = settings
        self.docker = docker

    @staticmethod
    def container_name(run_id: uuid.UUID) -> str:
        return f"coire-run-{run_id}"

    @staticmethod
    def network_name(run_id: uuid.UUID) -> str:
        return f"coire-run-{run_id}"

    @staticmethod
    def relay_name(run_id: uuid.UUID) -> str:
        return f"coire-run-relay-{run_id}"

    def workspace(self, reference: str) -> Path:
        if not _SAFE_ID.fullmatch(reference):
            raise RunRuntimeError("run_workspace_invalid", "invalid workspace reference")
        root = Path(self.settings.run_workspace_root).resolve()
        candidate = root / reference
        try:
            resolved = candidate.resolve(strict=True)
        except OSError as exc:
            raise RunRuntimeError("run_workspace_missing", "workspace does not exist") from exc
        if resolved.parent != root or resolved.is_symlink() or not resolved.is_dir():
            raise RunRuntimeError("run_workspace_invalid", "workspace escapes configured root")
        return resolved

    def create_payload(self, command: RunContainerCreate, network_name: str) -> dict[str, Any]:
        if command.argv != ["-m", "coire_agent"]:
            raise RunRuntimeError("run_command_invalid", "run command is not allowlisted")
        if command.image != self.settings.run_agent_image:
            raise RunRuntimeError("run_image_invalid", "run image is not allowlisted")
        if command.limits.memory_bytes > self.settings.run_max_memory_bytes:
            raise RunRuntimeError("run_limit_invalid", "run memory exceeds node maximum")
        workspace = self.workspace(command.workspace_ref)
        gateway_host = urlparse(command.gateway_url).hostname
        if not gateway_host:
            raise RunRuntimeError("run_gateway_invalid", "gateway URL has no host")
        return {
            "Image": command.image,
            "Cmd": command.argv,
            "Env": [
                f"COIRE_RUN_ID={command.run_id}",
                f"COIRE_PROFILE={command.profile.value}",
                f"COIRE_MODEL_ID={command.model_id}",
                f"COIRE_VERIFIED_VARIANT_ID={command.variant_id}",
                "COIRE_API_URL=http://coire-gateway:8080/v1",
                f"COIRE_RUN_TOKEN={command.run_token}",
            ],
            "Labels": {
                RUN_LABEL: str(command.run_id),
                MANAGED_LABEL: "true",
                "com.coire.gateway-host": gateway_host,
                "com.coire.timeout-seconds": str(command.limits.timeout_seconds),
                "com.coire.log-bytes": str(command.limits.log_bytes),
                "com.coire.result-bytes": str(command.limits.result_bytes),
            },
            "User": "65532:65532",
            "WorkingDir": "/workspace",
            "NetworkDisabled": False,
            "StopTimeout": 3,
            "HostConfig": {
                "NetworkMode": network_name,
                "ReadonlyRootfs": True,
                "CapDrop": ["ALL"],
                "SecurityOpt": ["no-new-privileges:true"],
                "Privileged": False,
                "Memory": command.limits.memory_bytes,
                "MemorySwap": command.limits.memory_bytes,
                "NanoCpus": command.limits.nano_cpus,
                "PidsLimit": command.limits.pids_limit,
                "RestartPolicy": {"Name": "no", "MaximumRetryCount": 0},
                "Binds": [f"{workspace}:/workspace:rw"],
                "Tmpfs": {"/tmp": "rw,noexec,nosuid,nodev,size=67108864,mode=1777"},
                "PortBindings": {},
                "PublishAllPorts": False,
                "AutoRemove": False,
                "ExtraHosts": ["host.docker.internal:host-gateway"],
            },
            "NetworkingConfig": {"EndpointsConfig": {network_name: {}}},
        }

    def relay_payload(self, command: RunContainerCreate) -> dict[str, Any]:
        if not self.settings.run_relay_image:
            raise RunRuntimeError("run_relay_image_invalid", "relay image is not configured")
        return {
            "Image": self.settings.run_relay_image,
            "Cmd": ["-m", "coire_node.run_relay"],
            "Env": [
                f"COIRE_RELAY_TARGET={command.gateway_url.rstrip('/')}",
                f"COIRE_RELAY_MAX_REQUEST_BYTES={self.settings.run_relay_request_bytes}",
            ],
            "Labels": {
                RUN_LABEL: str(command.run_id),
                MANAGED_LABEL: "true",
                "com.coire.component": "run-relay",
            },
            "User": "65532:65532",
            "NetworkDisabled": False,
            "StopTimeout": 3,
            "HostConfig": {
                "NetworkMode": "bridge",
                "ReadonlyRootfs": True,
                "CapDrop": ["ALL"],
                "SecurityOpt": ["no-new-privileges:true"],
                "Privileged": False,
                "Memory": 128 * 1024**2,
                "MemorySwap": 128 * 1024**2,
                "NanoCpus": 250_000_000,
                "PidsLimit": 64,
                "RestartPolicy": {"Name": "no", "MaximumRetryCount": 0},
                "Tmpfs": {"/tmp": "rw,noexec,nosuid,nodev,size=16777216,mode=1777"},
                "PortBindings": {},
                "PublishAllPorts": False,
                "AutoRemove": False,
            },
        }

    async def create(self, command: RunContainerCreate) -> RunContainerStatus:
        name = self.container_name(command.run_id)
        observed = await self.docker.inspect_container(name)
        if observed is None:
            network = self.network_name(command.run_id)
            try:
                await self.docker.create_network(network, internal=True)
            except DockerAPIError as exc:
                # CheckDuplicate makes a replay safe; a conflicting network is verified by
                # the subsequent container create/inspect rather than blindly replaced.
                if exc.status_code != 409:
                    raise
            relay_name = self.relay_name(command.run_id)
            relay = await self.docker.inspect_container(relay_name)
            if relay is None:
                relay_id = await self.docker.create_container(
                    relay_name, self.relay_payload(command)
                )
                await self.docker.connect_network(network, relay_id, aliases=["coire-gateway"])
                await self.docker.start_container(relay_id)
                await self._wait_relay_ready(relay_id)
            else:
                # A control-plane retry can observe the relay after its create effect but
                # before the runner create effect. Revalidate the existing relay instead of
                # assuming that a named container is usable.
                await self._wait_relay_ready(str(relay.get("Id", relay_name)))
            container_id = await self.docker.create_container(
                name, self.create_payload(command, network)
            )
            observed = await self.docker.inspect_container(container_id)
        if observed is None:
            raise RunRuntimeError("run_runtime_unavailable", "created container is not inspectable")
        return self._status(command.run_id, observed)

    async def _wait_relay_ready(self, container_id: str) -> None:
        deadline = time.monotonic() + self.settings.run_relay_start_timeout_s
        while time.monotonic() < deadline:
            observed = await self.docker.inspect_container(container_id)
            if observed is None:
                raise RunRuntimeError("run_relay_failed", "run relay disappeared")
            state = observed.get("State") or {}
            health = state.get("Health") or {}
            if health.get("Status") == "healthy":
                return
            if state.get("Status") in {"dead", "exited"} or health.get("Status") == "unhealthy":
                raise RunRuntimeError("run_relay_failed", "run relay did not become healthy")
            await asyncio.sleep(0.1)
        raise RunRuntimeError("run_relay_failed", "run relay readiness timed out")

    async def start(self, run_id: uuid.UUID) -> RunContainerStatus:
        name = self.container_name(run_id)
        await self.docker.start_container(name)
        observed = await self.docker.inspect_container(name)
        if observed is None:
            raise RunRuntimeError("run_container_missing", "run container disappeared")
        return self._status(run_id, observed)

    async def wait(self, run_id: uuid.UUID) -> RunContainerStatus:
        name = self.container_name(run_id)
        before = await self.docker.inspect_container(name)
        if before is None:
            raise RunRuntimeError("run_container_missing", "run container disappeared")
        labels = (before.get("Config") or {}).get("Labels") or {}
        timeout_seconds = int(labels.get("com.coire.timeout-seconds", 900))
        started_at = self._datetime((before.get("State") or {}).get("StartedAt"))
        elapsed = (datetime.now(UTC) - started_at).total_seconds() if started_at else 0.0
        remaining = max(0.001, timeout_seconds - elapsed)
        timed_out = False
        try:
            await asyncio.wait_for(self.docker.wait_container(name), timeout=remaining)
        except TimeoutError:
            timed_out = True
            await self.docker.kill_container(name)
        observed = await self.docker.inspect_container(name)
        if observed is None:
            raise RunRuntimeError("run_container_missing", "run container disappeared")
        result = self._status(run_id, observed)
        stats = await self.docker.stats(name)
        if stats is not None:
            memory = stats.get("memory_stats") or {}
            cpu = stats.get("cpu_stats") or {}
            cpu_usage = cpu.get("cpu_usage") or {}
            result.resource_usage = RunResourceUsage(
                peak_memory_bytes=max(int(memory.get("max_usage", 0)), int(memory.get("usage", 0))),
                cpu_nanoseconds=int(cpu_usage.get("total_usage", 0)),
            )
        return result.model_copy(update={"state": "timed_out"}) if timed_out else result

    async def logs(self, run_id: uuid.UUID, *, offset: int = 0) -> list[RunLogChunk]:
        name = self.container_name(run_id)
        observed = await self.docker.inspect_container(name)
        if observed is None:
            raise RunRuntimeError("run_container_missing", "run container disappeared")
        labels = (observed.get("Config") or {}).get("Labels") or {}
        raw = await self.docker.logs(name)
        limit = min(
            self.settings.run_max_log_bytes,
            int(labels.get("com.coire.log-bytes", self.settings.run_max_log_bytes)),
        )
        truncated = len(raw) > limit
        bounded = raw[:limit]
        text = self._decode_docker_logs(bounded)
        return [
            RunLogChunk(
                run_id=run_id,
                offset=offset,
                stream="stdout",
                content=text,
                truncated=truncated,
            )
        ]

    async def collect(self, run_id: uuid.UUID) -> RunCollectedResult:
        name = self.container_name(run_id)
        observed = await self.docker.inspect_container(name)
        if observed is None:
            raise RunRuntimeError("run_container_missing", "run container disappeared")
        labels = (observed.get("Config") or {}).get("Labels") or {}
        limit = min(
            self.settings.run_max_result_bytes,
            int(labels.get("com.coire.result-bytes", self.settings.run_max_result_bytes)),
        )
        archive = await self.docker.archive(name, RESULT_PATH)
        if archive is None:
            raise RunRuntimeError("run_result_missing", "run result is absent")
        if len(archive) > limit + 1024 * 1024:
            raise RunRuntimeError("run_result_unreadable", "run result archive exceeds limit")
        try:
            with tarfile.open(fileobj=io.BytesIO(archive), mode="r:*") as bundle:
                regular = [member for member in bundle.getmembers() if member.isfile()]
                if len(regular) != 1 or Path(regular[0].name).name != "result.json":
                    raise ValueError("archive must contain exactly result.json")
                extracted = bundle.extractfile(regular[0])
                if extracted is None:
                    raise ValueError("result is unreadable")
                payload = extracted.read(limit + 1)
                if len(payload) > limit:
                    raise ValueError("result exceeds limit")
                result = json.loads(payload)
                if not isinstance(result, dict):
                    raise ValueError("result must be an object")
        except (tarfile.TarError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise RunRuntimeError("run_result_unreadable", str(exc)) from exc
        return RunCollectedResult(run_id=run_id, result=result)

    async def remove(self, run_id: uuid.UUID, *, kill: bool = False) -> None:
        name = self.container_name(run_id)
        if kill:
            await self.docker.kill_container(name)
        await self.docker.remove_container(name, force=True)
        relay = self.relay_name(run_id)
        await self.docker.kill_container(relay)
        await self.docker.remove_container(relay, force=True)
        await self.docker.remove_network(self.network_name(run_id))

    async def observations(self) -> list[RunContainerObservation]:
        rows = await self.docker.list_containers(labels={MANAGED_LABEL: "true"})
        now = datetime.now(UTC)
        observations: list[RunContainerObservation] = []
        for row in rows:
            labels = row.get("Labels") or {}
            if labels.get("com.coire.component") == "run-relay":
                continue
            try:
                run_id = uuid.UUID(str(labels[RUN_LABEL]))
            except (KeyError, TypeError, ValueError):
                continue
            observations.append(
                RunContainerObservation(
                    run_id=run_id,
                    container_id=str(row.get("Id", "")),
                    state=str(row.get("State", "unknown")),
                    observed_at=now,
                )
            )
        return observations

    @staticmethod
    def _decode_docker_logs(raw: bytes) -> str:
        # Non-TTY Docker logs may be multiplexed: 1-byte stream, 3 zero bytes, 4-byte length.
        cursor = 0
        frames: list[bytes] = []
        while cursor + 8 <= len(raw) and raw[cursor] in (0, 1, 2):
            size = int.from_bytes(raw[cursor + 4 : cursor + 8], "big")
            end = cursor + 8 + size
            if end > len(raw):
                break
            frames.append(raw[cursor + 8 : end])
            cursor = end
        payload = b"".join(frames) if frames and cursor == len(raw) else raw
        return payload.decode("utf-8", errors="replace")

    @staticmethod
    def _status(run_id: uuid.UUID, observed: dict[str, Any]) -> RunContainerStatus:
        state = observed.get("State") or {}
        return RunContainerStatus(
            run_id=run_id,
            container_id=str(observed.get("Id", "")),
            state=str(state.get("Status", "unknown")),
            exit_code=state.get("ExitCode"),
            started_at=RunManager._datetime(state.get("StartedAt")),
            finished_at=RunManager._datetime(state.get("FinishedAt")),
            resource_usage=RunResourceUsage(),
            hardened=True,
        )

    @staticmethod
    def _datetime(value: Any) -> datetime | None:
        if not value or str(value).startswith("0001-"):
            return None
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
