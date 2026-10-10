"""Node-owned CPU analysis processes, immutable command journals and stop-before-release."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import platform
import shutil
import signal
import subprocess
import sys
import uuid
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import anyio
import httpx
import psutil
from opentelemetry import metrics, trace

from coire_core.models.acquisition import ReservationRequest
from coire_core.models.datasets import DatasetAnalysis
from coire_core.models.training_node import (
    DatasetAnalysisWorkerInput,
    NodeDatasetAnalysisRequest,
    NodeDatasetAnalysisStatus,
)
from coire_core.settings import Settings
from coire_node.reservations import ReservationLedger
from coire_node.store import write_atomic

logger = logging.getLogger(__name__)
tracer = trace.get_tracer("coire.node.training.analysis")
outcomes = metrics.get_meter("coire.node.training.analysis").create_counter(
    "coire_dataset_analysis_outcomes_total"
)


class AnalysisSupervisor:
    def __init__(self, settings: Settings, reservations: ReservationLedger) -> None:
        self.settings = settings
        self.reservations = reservations
        self.root = Path(settings.node_state_dir) / "training-analyses"
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.root.is_symlink() or self.root.stat().st_mode & 0o077:
            raise ValueError("analysis root must be private and unlinked")
        self.lock = asyncio.Lock()
        self.processes: dict[uuid.UUID, subprocess.Popen[bytes]] = {}
        self.preparations: dict[uuid.UUID, asyncio.Task[Any]] = {}
        for existing in self.root.glob("*/journal.json"):
            identity = uuid.UUID(existing.parent.name)
            journal = self.journal(identity)
            if journal.get("owns_reservation"):
                self._bind_owner(identity, uuid.UUID(journal["reservation_id"]))

    def _bind_owner(self, identity: uuid.UUID, reservation_id: uuid.UUID) -> None:
        self.reservations.bind_owner(
            reservation_id,
            release_check=lambda: self._release_allowed(identity, reservation_id),
            footprint_bytes=lambda: self._footprint(identity),
        )

    def _stopped(self, identity: uuid.UUID, journal: dict[str, Any]) -> bool:
        if (
            journal.get("state") not in {"failed", "cancelled", "succeeded"}
            or journal.get("stop_proven") is not True
        ):
            return False
        if (journal.get("pid") is None) != (journal.get("process_create_time") is None):
            return False
        if journal.get("pid") is not None:
            if self._owned_process(identity, journal) is not None:
                return False
            self._prove_group_dead(int(journal["pid"]))
        return True

    def _release_allowed(self, identity: uuid.UUID, reservation_id: uuid.UUID) -> bool:
        journal = self.journal(identity)
        if (
            journal.get("reservation_id") != str(reservation_id)
            or journal.get("owns_reservation") is not True
            or not self._stopped(identity, journal)
        ):
            return False
        root = self.path(identity)
        return not any(
            path.exists() or path.is_symlink()
            for path in (root / "source.jsonl", root / "worker-home")
        )

    def _footprint(self, identity: uuid.UUID) -> int | None:
        journal = self.journal(identity)
        process = self._owned_process(identity, journal)
        return int(process.memory_info().rss) if process is not None else None

    def _release(self, identity: uuid.UUID, journal: dict[str, Any]) -> None:
        if journal.get("owns_reservation", True) and not self.reservations.release(
            uuid.UUID(journal["reservation_id"])
        ):
            raise ValueError("analysis memory/disk release requires checked stop and cleanup")

    def argv(self, identity: uuid.UUID, nonce: str) -> list[str]:
        return [
            sys.executable,
            "-m",
            "coire_node.training.analysis_worker",
            "--analysis",
            str(identity),
            "--owner",
            nonce,
            "--state-root",
            str(self.root),
            "--store-root",
            self.settings.node_store_dir,
        ]

    def cleanup(self, identity: uuid.UUID) -> None:
        """Remove private source/cache only after preparation or process death is proved."""
        path = self.path(identity)
        (path / "source.jsonl").unlink(missing_ok=True)
        home = path / "worker-home"
        if home.is_symlink():
            raise ValueError("analysis cache is linked")
        if home.exists():
            shutil.rmtree(home)
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def path(self, identity: uuid.UUID) -> Path:
        path = self.root / str(identity)
        if path.is_symlink():
            raise ValueError("analysis journal path is linked")
        return path

    def journal(self, identity: uuid.UUID) -> dict[str, Any]:
        path = self.path(identity) / "journal.json"
        if not self.path(identity).exists():
            raise FileNotFoundError("analysis identity unavailable")
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 64 * 1024:
            raise ValueError("analysis journal is unavailable")
        value = json.loads(path.read_bytes())
        if not isinstance(value, dict) or value.get("analysis_id") != str(identity):
            raise ValueError("analysis journal identity differs")
        return value

    def save(self, identity: uuid.UUID, value: dict[str, Any]) -> None:
        write_atomic(
            self.path(identity) / "journal.json", json.dumps(value, sort_keys=True).encode()
        )
        for directory in (self.path(identity), self.root):
            fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)

    def result(self, identity: uuid.UUID) -> DatasetAnalysis | None:
        journal = self.journal(identity)
        digest = journal.get("request_sha256", "")
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(letter not in "0123456789abcdef" for letter in digest)
        ):
            raise ValueError("analysis result command identity unavailable")
        # The shared result model is published under its complete immutable input
        # digest; no additional untyped worker-to-parent receipt is needed.
        path = self.path(identity) / f"result-{digest}.json"
        if not path.exists():
            return None
        if path.is_symlink() or path.stat().st_size > 512 * 1024:
            raise ValueError("analysis result is unsafe or oversized")
        result = DatasetAnalysis.model_validate_json(path.read_bytes())
        worker_path = self.path(identity) / "worker.json"
        if (
            worker_path.is_symlink()
            or not worker_path.is_file()
            or worker_path.stat().st_size > 512 * 1024
        ):
            raise ValueError("analysis worker envelope unsafe")
        envelope = DatasetAnalysisWorkerInput.model_validate_json(worker_path.read_bytes())
        if (
            hashlib.sha256(envelope.model_dump_json().encode()).hexdigest()
            != journal["request_sha256"]
        ):
            raise ValueError("analysis worker envelope changed")
        result_sha = hashlib.sha256(result.model_dump_json().encode()).hexdigest()
        if journal.get("result_sha256") is not None and journal["result_sha256"] != result_sha:
            raise ValueError("analysis result immutable identity differs")
        result.validate_binding(envelope.binding)
        expected_samples = result.row_count * (
            2 if envelope.binding.format.value == "preference" else 1
        )
        if (
            result.id != identity
            or result.dataset_id != envelope.binding.dataset_id
            or result.model_id != envelope.binding.model_id
            or result.variant_id != envelope.binding.variant_id
            or result.state not in {"succeeded", "failed"}
            or not result.row_count
            or result.invalid_count > result.row_count
            or result.duplicate_rows >= result.row_count
            or any(count < 0 for count in result.role_counts.values())
            or any(item.row > result.row_count for item in result.diagnostics)
            or (result.tokens is not None and sum(result.tokens.histogram) > expected_samples)
            or (
                result.state == "succeeded"
                and result.tokens is not None
                and sum(result.tokens.histogram) != expected_samples
            )
            or (
                journal.get("template_sha256") is not None
                and result.template_sha256 != journal["template_sha256"]
            )
            or (
                envelope.binding.template_override is not None
                and result.template_sha256
                != hashlib.sha256(envelope.binding.template_override.encode()).hexdigest()
            )
        ):
            raise ValueError("analysis result differs from its immutable worker binding")
        return result

    async def start(self, command: NodeDatasetAnalysisRequest) -> NodeDatasetAnalysisStatus:
        with tracer.start_as_current_span("coire.node.training.analysis.start") as span:
            span.set_attribute("coire.analysis_id", str(command.analysis_id))
            return await self._start(command)

    async def _start(self, command: NodeDatasetAnalysisRequest) -> NodeDatasetAnalysisStatus:
        if platform.node().lower().split(".", 1)[0] == "coire-core":
            raise ValueError("tokenizer analysis execution is forbidden on core")
        if (
            not self.settings.training_enabled
            or command.input_grant.node != self.settings.node_name
        ):
            raise ValueError("analysis capability/node scope unavailable")
        if command.deadline <= datetime.now(UTC) or command.input_grant.expires_at <= datetime.now(
            UTC
        ):
            raise ValueError("analysis execution scope expired")
        if command.deadline > datetime.now(UTC) + timedelta(
            seconds=self.settings.training_analysis_timeout_s
        ):
            raise ValueError("analysis deadline exceeds 30 minutes")
        if (
            command.memory_bytes > self.settings.training_analysis_memory_bytes
            or command.input_grant.max_bytes > self.settings.training_dataset_upload_max_bytes
        ):
            raise ValueError("analysis exceeds configured input or memory bounds")
        envelope = DatasetAnalysisWorkerInput(
            command_id=command.command_id,
            analysis_id=command.analysis_id,
            binding=command.binding,
            source_bytes=command.input_grant.max_bytes,
            memory_bytes=command.memory_bytes,
            max_sequence_length=command.max_sequence_length,
            deadline=command.deadline,
        )
        digest = hashlib.sha256(envelope.model_dump_json().encode()).hexdigest()
        command_digest = hashlib.sha256(
            json.dumps(
                # Delivery credentials may be refreshed; execution identity and
                # the grant's node/source/byte scope remain immutable.
                command.model_dump(
                    mode="json", exclude={"input_grant": {"secret", "grant_id", "expires_at"}}
                ),
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        if command.request_sha256 != digest:
            raise ValueError("analysis request digest differs from its immutable worker envelope")
        async with self.lock:
            path = self.path(command.analysis_id)
            if path.exists():
                prior = await anyio.to_thread.run_sync(self.journal, command.analysis_id)
                if prior["reservation_id"] != str(command.reservation_id):
                    raise ValueError("analysis reservation identity conflicts")
                if (
                    prior["request_sha256"] != digest
                    or prior["command_id"] != str(command.command_id)
                    or prior.get("command_sha256") != command_digest
                ):
                    raise ValueError("analysis command identity conflicts")
                return await self._status(command.analysis_id)
            if len(list(self.root.glob("*/journal.json"))) >= 64:
                raise ValueError("analysis journal retention requires cleanup")
            for previous in self.root.glob("*/journal.json"):
                value = self.journal(uuid.UUID(previous.parent.name))
                if value.get("state") not in {"succeeded", "failed", "cancelled"} or value.get(
                    "release_pending"
                ):
                    raise ValueError("node analysis slot remains occupied or uncertain")
            if (
                shutil.disk_usage(self.root).free
                < command.input_grant.max_bytes
                + 1024**2
                + self.settings.training_artifact_disk_floor_bytes
            ):
                raise ValueError("analysis cache filesystem has insufficient free space")
            await anyio.to_thread.run_sync(lambda: path.mkdir(mode=0o700))
            journal: dict[str, Any] = {
                "analysis_id": str(command.analysis_id),
                "command_id": str(command.command_id),
                "request_sha256": digest,
                "command_sha256": command_digest,
                "reservation_id": str(command.reservation_id),
                "state": "preparing",
                "pid": None,
                "process_create_time": None,
                "spawn_nonce": str(uuid.uuid4()),
                "deadline": command.deadline.isoformat(),
                "memory_bytes": command.memory_bytes,
                "template_sha256": command.template_sha256,
                "owns_reservation": False,
            }
            await anyio.to_thread.run_sync(self.save, command.analysis_id, journal)
            task = asyncio.current_task()
            assert task is not None
            self.preparations[command.analysis_id] = task
            held = False
            try:
                _reservation, held = self.reservations.hold(
                    ReservationRequest(
                        workflow_id=command.analysis_id,
                        variant_id=command.variant_id,
                        memory_bytes=command.memory_bytes,
                        disk_bytes=command.input_grant.max_bytes + 1024**2,
                        idempotency_key=command.reservation_id,
                    ),
                    disk_path=path,
                    disk_floor_bytes=self.settings.training_artifact_disk_floor_bytes,
                    require_stop=True,
                )
                if not held:
                    raise ValueError("analysis reservation belongs to an existing command")
                journal["owns_reservation"] = True
                self._bind_owner(command.analysis_id, command.reservation_id)
                self.save(command.analysis_id, journal)
                url = self.settings.training_input_api_url
                parsed = urlsplit(url)
                if (
                    parsed.hostname != self.settings.core_control_host
                    or parsed.scheme not in {"http", "https"}
                    or parsed.username
                    or parsed.password
                    or parsed.path not in {"", "/"}
                    or parsed.query
                    or parsed.fragment
                ):
                    raise ValueError("analysis source origin differs from declared core")
                source = path / "source.jsonl"
                actual, source_hash = 0, hashlib.sha256()
                async with (
                    asyncio.timeout(max(0, (command.deadline - datetime.now(UTC)).total_seconds())),
                    httpx.AsyncClient(
                        timeout=30, follow_redirects=False, trust_env=False
                    ) as client,
                    client.stream(
                        "GET",
                        f"{url.rstrip('/')}/api/v1/internal/training/datasets/{command.binding.dataset_id}/content",
                        headers={
                            "Authorization": "Bearer "
                            + self.settings.node_token.get_secret_value(),
                            "X-Coire-Node": self.settings.node_name,
                            "X-Coire-Dataset-Grant": command.input_grant.secret,
                        },
                    ) as response,
                ):
                    response.raise_for_status()
                    if response.headers.get("content-encoding", "identity") != "identity":
                        raise ValueError("analysis source encoding is unsupported")
                    async with await anyio.open_file(source, "xb") as output:
                        await anyio.to_thread.run_sync(source.chmod, 0o600)
                        async for chunk in response.aiter_bytes(64 * 1024):
                            actual += len(chunk)
                            if (
                                actual > command.input_grant.max_bytes
                                or datetime.now(UTC) >= command.deadline
                            ):
                                raise ValueError("analysis source exceeds its execution bounds")
                            source_hash.update(chunk)
                            await output.write(chunk)
                if (
                    actual != command.input_grant.max_bytes
                    or source_hash.hexdigest() != command.binding.source_sha256
                ):
                    raise ValueError("analysis source digest differs")
                write_atomic(path / "worker.json", envelope.model_dump_json().encode())
                # Intent exists before spawn. This fixed marker permits re-adoption
                # after a node-agent crash without blindly spawning another worker.
                journal["state"] = "launching"
                self.save(command.analysis_id, journal)
                env = {
                    key: os.environ[key] for key in ("PATH", "LANG", "LC_ALL") if key in os.environ
                }
                env.update(
                    {
                        "HF_HUB_OFFLINE": "1",
                        "TRANSFORMERS_OFFLINE": "1",
                        "HF_HUB_DISABLE_TELEMETRY": "1",
                        "PYTHONDONTWRITEBYTECODE": "1",
                    }
                )
                env["OTLP_ENDPOINT"] = self.settings.otlp_endpoint
                home = path / "worker-home"
                await anyio.to_thread.run_sync(lambda: home.mkdir(mode=0o700))
                env.update(
                    {
                        "HOME": str(home),
                        "HF_HOME": str(home),
                        "TOKENIZERS_PARALLELISM": "false",
                    }
                )
                process = await anyio.to_thread.run_sync(
                    lambda: subprocess.Popen(
                        self.argv(command.analysis_id, journal["spawn_nonce"]),
                        start_new_session=True,
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        env=env,
                    )
                )
                self.processes[command.analysis_id] = process
                journal["pid"], journal["process_create_time"] = (
                    process.pid,
                    psutil.Process(process.pid).create_time(),
                )
                journal["state"] = "running"
                self.save(command.analysis_id, journal)
            except BaseException:
                if (
                    journal.get("state") == "preparing"
                    and command.analysis_id not in self.processes
                ):
                    journal["state"] = "failed"
                    journal["stop_proven"] = True
                    journal["release_pending"] = held
                    self.save(command.analysis_id, journal)
                    self.cleanup(command.analysis_id)
                    if held:
                        self._release(command.analysis_id, journal)
                    journal["release_pending"] = False
                    self.save(command.analysis_id, journal)
                raise
            finally:
                self.preparations.pop(command.analysis_id, None)
            return await self._status(command.analysis_id)

    def _owned_process(self, identity: uuid.UUID, journal: dict[str, Any]) -> psutil.Process | None:
        candidates = []
        if journal.get("pid"):
            try:
                candidates = [psutil.Process(int(journal["pid"]))]
            except psutil.NoSuchProcess:
                return None
        else:
            if journal.get("state") == "preparing":
                return None
            candidates = list(psutil.process_iter())
        owned = []
        for process in candidates:
            try:
                if not journal.get("pid") and process.uids().real != os.getuid():
                    continue
                argv = process.cmdline()
                if (
                    argv == self.argv(identity, journal.get("spawn_nonce", ""))
                    and os.getpgid(process.pid) == process.pid
                    and process.uids().real == os.getuid()
                ):
                    if journal.get(
                        "process_create_time"
                    ) is not None and process.create_time() != float(
                        journal["process_create_time"]
                    ):
                        continue
                    owned.append(process)
                    continue
                if (
                    journal.get("pid")
                    and journal.get("process_create_time") is not None
                    and process.create_time() == float(journal["process_create_time"])
                ):
                    raise ValueError("analysis process marker no longer matches its live identity")
            except psutil.AccessDenied:
                raise ValueError("analysis process identity cannot be proved") from None
            except (psutil.NoSuchProcess, ProcessLookupError):
                continue
        if len(owned) > 1:
            raise ValueError("analysis process ownership is ambiguous")
        return owned[0] if owned else None

    async def status(self, identity: uuid.UUID) -> NodeDatasetAnalysisStatus:
        async with self.lock:
            return await self._status(identity)

    async def _status(self, identity: uuid.UUID) -> NodeDatasetAnalysisStatus:
        journal = await anyio.to_thread.run_sync(self.journal, identity)
        if journal.get("release_pending"):
            if not self._stopped(identity, journal):
                raise ValueError("analysis stop proof changed before cleanup retry")
            await anyio.to_thread.run_sync(self.cleanup, identity)
            self._release(identity, journal)
            journal["release_pending"] = False
            await anyio.to_thread.run_sync(self.save, identity, journal)
        if journal["state"] in {"running", "queued", "preparing", "launching"}:
            process = await anyio.to_thread.run_sync(self._owned_process, identity, journal)
            if process is not None and process.status() != psutil.STATUS_ZOMBIE:
                if journal.get("pid") is None:
                    journal["pid"], journal["process_create_time"] = (
                        process.pid,
                        process.create_time(),
                    )
                    await anyio.to_thread.run_sync(self.save, identity, journal)
                if (
                    journal.get("deadline")
                    and datetime.now(UTC) >= datetime.fromisoformat(journal["deadline"])
                ) or (
                    journal.get("memory_bytes")
                    and process.memory_info().rss > journal["memory_bytes"]
                ):
                    await self._stop(identity, journal, process, "failed")
                    return NodeDatasetAnalysisStatus(
                        analysis_id=identity, state="failed", completed_rows=0
                    )
                return NodeDatasetAnalysisStatus(
                    analysis_id=identity, state="running", completed_rows=0
                )
            if journal.get("pid") is None:
                return NodeDatasetAnalysisStatus(
                    analysis_id=identity, state="queued", completed_rows=0
                )
            # A dead leader alone is insufficient if its process group still has members.
            await anyio.to_thread.run_sync(self._prove_group_dead, int(journal["pid"]))
            try:
                result = await anyio.to_thread.run_sync(self.result, identity)
            except ValueError:
                result = None
            journal["state"] = result.state if result else "failed"
            journal["stop_proven"] = True
            journal["release_pending"] = True
            if result is not None:
                journal["result_sha256"] = hashlib.sha256(
                    result.model_dump_json().encode()
                ).hexdigest()
            await anyio.to_thread.run_sync(self.save, identity, journal)
            await anyio.to_thread.run_sync(self.cleanup, identity)
            self._release(identity, journal)
            journal["release_pending"] = False
            await anyio.to_thread.run_sync(self.save, identity, journal)
            if identity in self.processes:
                child = self.processes.pop(identity)
                await anyio.to_thread.run_sync(lambda: child.wait(timeout=1))
            outcomes.add(1, {"state": journal["state"], "node": self.settings.node_name})
            logger.info(
                "dataset analysis completed",
                extra={"analysis_id": str(identity), "state": journal["state"]},
            )
        result = None
        if journal["state"] in {"succeeded", "failed"}:
            try:
                result = await anyio.to_thread.run_sync(self.result, identity)
            except ValueError:
                if journal["state"] == "succeeded":
                    raise
            if result is not None and result.state != journal["state"]:
                result = None
        return NodeDatasetAnalysisStatus.model_validate(
            {
                "analysis_id": str(identity),
                "state": journal["state"],
                "completed_rows": result.row_count if result else 0,
                "result": result.model_dump(mode="json") if result else None,
            }
        )

    async def cancel(self, identity: uuid.UUID) -> NodeDatasetAnalysisStatus:
        task = self.preparations.get(identity)
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError, ValueError, httpx.HTTPError, TimeoutError):
                await task
        async with self.lock:
            return await self._cancel(identity)

    def _prove_group_dead(self, pid: int) -> None:
        for member in psutil.process_iter():
            try:
                if os.getpgid(member.pid) == pid and member.status() != psutil.STATUS_ZOMBIE:
                    # PID reuse with a different create time proves the old leader dead;
                    # only its newly owned group is unrelated to this reservation.
                    if member.pid == pid:
                        continue
                    raise ValueError("analysis process group death remains uncertain")
            except (psutil.NoSuchProcess, ProcessLookupError):
                continue
            except (psutil.AccessDenied, PermissionError):
                raise ValueError("analysis process group death cannot be proved") from None

    async def _stop(
        self,
        identity: uuid.UUID,
        journal: dict[str, Any],
        process: psutil.Process | None,
        state: str,
    ) -> None:
        if process is not None:
            # Re-prove immediately before signaling; a recycled PID is never a kill target.
            current = await anyio.to_thread.run_sync(self._owned_process, identity, journal)
            if current is None:
                raise ValueError("analysis stop identity changed")
            if journal.get("pid") is None:
                journal["pid"] = current.pid
                journal["process_create_time"] = current.create_time()
                await anyio.to_thread.run_sync(self.save, identity, journal)
            try:
                os.killpg(current.pid, signal.SIGTERM)
                await anyio.to_thread.run_sync(current.wait, 2)
            except psutil.TimeoutExpired:
                current = await anyio.to_thread.run_sync(self._owned_process, identity, journal)
                if current is not None:
                    os.killpg(current.pid, signal.SIGKILL)
                    await anyio.to_thread.run_sync(current.wait, 2)
            except ProcessLookupError:
                pass
            if await anyio.to_thread.run_sync(self._owned_process, identity, journal) is not None:
                raise ValueError("analysis process stop remains uncertain")
        if journal.get("pid"):
            await anyio.to_thread.run_sync(self._prove_group_dead, int(journal["pid"]))
        journal["state"] = state
        journal["stop_proven"] = True
        journal["release_pending"] = True
        await anyio.to_thread.run_sync(self.save, identity, journal)
        await anyio.to_thread.run_sync(self.cleanup, identity)
        self._release(identity, journal)
        journal["release_pending"] = False
        await anyio.to_thread.run_sync(self.save, identity, journal)
        child = self.processes.pop(identity, None)
        if child is not None:
            await anyio.to_thread.run_sync(lambda: child.wait(timeout=1))
        outcomes.add(1, {"state": state, "node": self.settings.node_name})
        logger.info(
            "dataset analysis stopped", extra={"analysis_id": str(identity), "state": state}
        )

    async def _cancel(self, identity: uuid.UUID) -> NodeDatasetAnalysisStatus:
        journal = await anyio.to_thread.run_sync(self.journal, identity)
        if journal["state"] in {"succeeded", "cancelled"}:
            return await self._status(identity)
        if journal["state"] == "failed" and journal.get("pid") is None:
            await self._stop(identity, journal, None, "cancelled")
            return NodeDatasetAnalysisStatus(
                analysis_id=identity, state="cancelled", completed_rows=0
            )
        process = await anyio.to_thread.run_sync(self._owned_process, identity, journal)
        if process is None and journal.get("pid") is None and journal["state"] != "failed":
            raise ValueError("analysis preparation/spawn liveness remains uncertain")
        await self._stop(identity, journal, process, "cancelled")
        return NodeDatasetAnalysisStatus(analysis_id=identity, state="cancelled", completed_rows=0)
