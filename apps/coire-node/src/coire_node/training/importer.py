"""Restart-visible, credential-free import journals and bounded data-fabric artifact transfer."""

from __future__ import annotations

import asyncio
import os
import shutil
import stat
import tempfile
import threading
import uuid
from pathlib import Path

import anyio
from opentelemetry import trace

from coire_core.models.training_node import (
    TrainingArtifactGrantRefresh,
    TrainingArtifactImportIntent,
    TrainingArtifactImportJournal,
    TrainingArtifactImportRequest,
    TrainingArtifactImportStatus,
    TrainingArtifactManifest,
)
from coire_core.net import DataFabricClient
from coire_node.store import sha256_file
from coire_node.training.artifacts import TrainingArtifacts

tracer = trace.get_tracer("coire.node.training")
MAX_MANIFEST_BYTES = 64 * 1024**2


class GrantUnavailable(RuntimeError):
    pass


class ArtifactImporter:
    def __init__(
        self,
        artifacts: TrainingArtifacts,
        journal_root: Path,
        *,
        port: int,
        client: DataFabricClient | None = None,
        max_bytes: int = 200 * 1024**3,
        disk_floor_bytes: int = 20 * 1024**3,
    ) -> None:
        if journal_root.is_symlink():
            raise ValueError("artifact journal root is linked")
        journal_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.artifacts = artifacts
        self.root = journal_root.resolve()
        self.port = port
        self.client = client or DataFabricClient(timeout=60)
        self.max_bytes = max_bytes
        self.disk_floor_bytes = disk_floor_bytes
        self._records: dict[uuid.UUID, TrainingArtifactImportJournal] = {}
        self._tasks: dict[uuid.UUID, asyncio.Task[None]] = {}
        self._slots = asyncio.Semaphore(1)
        self._holds: dict[uuid.UUID, int] = {}
        self._admission = asyncio.Lock()
        self._commands = asyncio.Lock()
        self._cache_lock = threading.RLock()

    def _record(self, identity: uuid.UUID) -> TrainingArtifactImportJournal | None:
        cached = self._records.get(identity)
        if cached is not None:
            return cached
        path = self.root / f"{identity}.json"
        if not path.exists():
            return None
        info = path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_mode & 0o077
            or info.st_size > MAX_MANIFEST_BYTES
        ):
            raise ValueError("artifact import journal is unsafe")
        record = TrainingArtifactImportJournal.model_validate_json(path.read_bytes())
        if record.intent.command_id != identity:
            raise ValueError("artifact import journal filename differs from intent")
        self._cache(record)
        return record

    def _cache(self, record: TrainingArtifactImportJournal) -> None:
        with self._cache_lock:
            self._records[record.intent.command_id] = record
            finished = [
                identity
                for identity, item in self._records.items()
                if item.status.state in {"verified", "failed", "cancelled"}
                and (identity not in self._tasks or self._tasks[identity].done())
            ]
            for identity in finished[:-8]:
                self._records.pop(identity, None)
                self._tasks.pop(identity, None)

    def _persist(self, record: TrainingArtifactImportJournal) -> None:
        destination = self.root / f"{record.intent.command_id}.json"
        fd, temporary = tempfile.mkstemp(prefix=".import-", dir=self.root)
        try:
            with os.fdopen(fd, "wb") as output:
                output.write(record.model_dump_json().encode())
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, destination)
            directory = os.open(self.root, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def status(self, identity: uuid.UUID) -> TrainingArtifactImportStatus:
        record = self._record(identity)
        if record is None:
            raise ValueError("artifact import is unknown")
        return record.status

    async def _status(self, identity: uuid.UUID, **changes: object) -> None:
        record = self._records[identity]
        status = TrainingArtifactImportStatus.model_validate(
            {**record.status.model_dump(mode="json"), **changes}
        )
        updated = TrainingArtifactImportJournal(intent=record.intent, status=status)
        await anyio.to_thread.run_sync(self._persist, updated)
        self._cache(updated)

    async def start(self, request: TrainingArtifactImportRequest) -> TrainingArtifactImportStatus:
        async with self._commands:
            return await self._start(request)

    async def _start(self, request: TrainingArtifactImportRequest) -> TrainingArtifactImportStatus:
        if (self.artifacts.root / f".deletion-{request.artifact_id}.json").exists():
            raise ValueError("retired artifact cannot be imported")
        if (
            request.destination_node != self.artifacts.node_name
            or request.source_node == request.destination_node
        ):
            raise ValueError(
                "artifact import must use distinct declared source/destination Studios"
            )
        intent = TrainingArtifactImportIntent.model_validate(
            request.model_dump(mode="json", exclude={"grant_secret"})
        )
        prior = await anyio.to_thread.run_sync(self._record, request.command_id)
        if prior is not None:
            if prior.intent.model_dump(exclude={"grant_id"}) != intent.model_dump(
                exclude={"grant_id"}
            ):
                raise ValueError("artifact import command identifies different immutable intent")
            return prior.status
        if sum(not task.done() for task in self._tasks.values()) >= 32:
            raise ValueError("artifact import queue capacity exhausted")
        status = TrainingArtifactImportStatus(
            import_id=request.command_id,
            artifact_id=request.artifact_id,
            manifest_sha256=request.manifest_sha256,
            state="staging",
            transferred_bytes=0,
        )
        record = TrainingArtifactImportJournal(intent=intent, status=status)
        await anyio.to_thread.run_sync(self._persist, record)
        self._cache(record)
        self._tasks[request.command_id] = asyncio.create_task(self._run(request))
        return status

    async def refresh(
        self, identity: uuid.UUID, request: TrainingArtifactGrantRefresh
    ) -> TrainingArtifactImportStatus:
        async with self._commands:
            return await self._refresh(identity, request)

    async def _refresh(
        self, identity: uuid.UUID, request: TrainingArtifactGrantRefresh
    ) -> TrainingArtifactImportStatus:
        prior = await anyio.to_thread.run_sync(self._record, identity)
        if prior is None:
            raise ValueError("artifact import is unknown")
        intent = prior.intent
        if (
            request.artifact_id != intent.artifact_id
            or request.manifest_sha256 != intent.manifest_sha256
            or request.attempt_id != intent.attempt_id
            or request.fence != intent.fence
        ):
            raise ValueError("artifact grant refresh cannot expand import scope")
        if prior.status.state == "verified":
            return prior.status
        task = self._tasks.get(identity)
        if task is not None and not task.done():
            raise ValueError("artifact import is still running")
        updated = intent.model_copy(update={"grant_id": request.grant_id})
        resumed = prior.status.model_copy(update={"state": "staging", "reason": None})
        record = TrainingArtifactImportJournal(intent=updated, status=resumed)
        await anyio.to_thread.run_sync(self._persist, record)
        self._records[identity] = record
        command = TrainingArtifactImportRequest.model_validate(
            {**updated.model_dump(mode="json"), "grant_secret": request.grant_secret}
        )
        self._tasks[identity] = asyncio.create_task(self._run(command))
        return resumed

    async def _fetch_manifest(
        self, request: TrainingArtifactImportRequest
    ) -> TrainingArtifactManifest:
        body = bytearray()
        async with self.client.stream(
            "GET",
            request.source_node,
            f"/training-artifacts/{request.artifact_id}/manifest",
            port=self.port,
            headers={"X-Coire-Artifact-Grant": request.grant_secret},
        ) as response:
            if response.status_code in {401, 403, 404}:
                raise GrantUnavailable()
            response.raise_for_status()
            async for chunk in response.aiter_bytes(64 * 1024):
                if len(body) + len(chunk) > MAX_MANIFEST_BYTES:
                    raise ValueError("artifact manifest exceeds its bound")
                body.extend(chunk)
        manifest = TrainingArtifactManifest.model_validate_json(body)
        if (
            manifest.artifact_id != request.artifact_id
            or manifest.canonical_sha256() != request.manifest_sha256
        ):
            raise ValueError("artifact manifest differs from immutable import intent")
        return manifest

    def _prepare_partial(self, path: Path, maximum: int) -> int:
        if not path.exists():
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            os.close(descriptor)
            return 0
        info = path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_mode & 0o077
            or info.st_size > maximum
        ):
            raise ValueError("artifact partial file is unsafe")
        return info.st_size

    def _prepare_staging(self, path: Path) -> None:
        if path.is_symlink():
            raise ValueError("artifact staging directory is linked")
        path.mkdir(mode=0o700, exist_ok=True)
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077:
            raise ValueError("artifact staging directory is not private")

    async def _run(self, request: TrainingArtifactImportRequest) -> None:
        identity = request.command_id
        try:
            async with self._slots:
                with tracer.start_as_current_span("coire.node.training.artifact.import"):
                    manifest = await self._fetch_manifest(request)
                    destination = self.artifacts.root / str(request.artifact_id)
                    if await anyio.to_thread.run_sync(destination.exists):
                        existing = await anyio.to_thread.run_sync(
                            self.artifacts.manifest, request.artifact_id
                        )
                        if existing.canonical_sha256() != request.manifest_sha256:
                            raise ValueError("artifact identity already holds different bytes")
                        for entry in existing.files:
                            path = await anyio.to_thread.run_sync(
                                self.artifacts.file, existing, entry.id
                            )
                            if await anyio.to_thread.run_sync(sha256_file, path) != entry.sha256:
                                raise ValueError("existing artifact checksum differs")
                        await self._status(
                            identity,
                            state="verified",
                            verified_manifest=existing.model_dump(mode="json"),
                            transferred_bytes=existing.total_bytes,
                        )
                        return
                    async with self._admission:
                        used = await anyio.to_thread.run_sync(
                            lambda: sum(
                                path.stat().st_size
                                for path in self.artifacts.root.rglob("*")
                                if path.is_file() and not path.is_symlink()
                            )
                        )
                        free = await anyio.to_thread.run_sync(
                            lambda: shutil.disk_usage(self.artifacts.root).free
                        )
                        if (
                            used + manifest.total_bytes > self.max_bytes
                            or free - manifest.total_bytes < self.disk_floor_bytes
                        ):
                            raise ValueError("artifact transfer disk quota is unavailable")
                        self._holds[identity] = manifest.total_bytes
                    await self._status(identity, state="transferring", reason=None)
                    staging = self.artifacts.root / f".import-{request.artifact_id}"
                    await anyio.to_thread.run_sync(self._prepare_staging, staging)
                    transferred = 0
                    for entry in manifest.files:
                        path = staging / entry.name
                        offset = await anyio.to_thread.run_sync(
                            self._prepare_partial, path, entry.bytes
                        )
                        headers = {"X-Coire-Artifact-Grant": request.grant_secret}
                        if offset:
                            headers["Range"] = f"bytes={offset}-"
                        if offset < entry.bytes:
                            async with self.client.stream(
                                "GET",
                                request.source_node,
                                f"/training-artifacts/{manifest.artifact_id}/files/{entry.id}",
                                port=self.port,
                                headers=headers,
                            ) as response:
                                if response.status_code in {401, 403, 404}:
                                    raise GrantUnavailable()
                                if offset and (
                                    response.status_code != 206
                                    or response.headers.get("content-range", "")
                                    != f"bytes {offset}-{entry.bytes - 1}/{entry.bytes}"
                                ):
                                    raise ValueError(
                                        "artifact Range response does not match partial bytes"
                                    )
                                response.raise_for_status()
                                async with await anyio.open_file(path, "ab") as output:
                                    async for chunk in response.aiter_bytes(1024 * 1024):
                                        offset += len(chunk)
                                        if offset > entry.bytes:
                                            raise ValueError(
                                                "artifact response exceeds declared bytes"
                                            )
                                        await output.write(chunk)
                                    await output.flush()
                            await anyio.to_thread.run_sync(path.chmod, 0o600)
                        if (
                            offset != entry.bytes
                            or await anyio.to_thread.run_sync(sha256_file, path) != entry.sha256
                        ):
                            await anyio.to_thread.run_sync(path.unlink)
                            raise ValueError("artifact received bytes failed digest verification")
                        from coire_node.training.checkpoints import _finish_file, _fsync_directory

                        await anyio.to_thread.run_sync(_finish_file, path)
                        transferred += entry.bytes
                        await self._status(identity, transferred_bytes=transferred)
                    await self._status(identity, state="verifying")
                    manifest_path = staging / "manifest.json"
                    async with await anyio.open_file(manifest_path, "wb") as output:
                        await output.write(manifest.model_dump_json().encode())
                        await output.flush()
                    await anyio.to_thread.run_sync(_finish_file, manifest_path)
                    await anyio.to_thread.run_sync(_fsync_directory, staging)

                    def publish() -> None:
                        with self.artifacts._lock:
                            if (
                                self.artifacts.root / f".deletion-{request.artifact_id}.json"
                            ).exists():
                                raise ValueError("retired artifact cannot be republished")
                            os.rename(staging, destination)

                    await anyio.to_thread.run_sync(publish)
                    await anyio.to_thread.run_sync(_fsync_directory, self.artifacts.root)
                    await self._status(
                        identity,
                        state="verified",
                        verified_manifest=manifest.model_dump(mode="json"),
                        transferred_bytes=manifest.total_bytes,
                    )
        except GrantUnavailable:
            await self._status(identity, state="failed", reason="lease_expired")
        except asyncio.CancelledError:
            await self._status(identity, state="cancelled", reason="cancelled")
            raise
        except Exception:
            await self._status(identity, state="failed", reason="replication_failed")
        finally:
            self._holds.pop(identity, None)

    async def cancel(self, identity: uuid.UUID) -> TrainingArtifactImportStatus:
        async with self._commands:
            record = await anyio.to_thread.run_sync(self._record, identity)
            if record is None:
                raise ValueError("artifact import is unknown")
            task = self._tasks.get(identity)
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            current = self.status(identity)
            if current.state not in {"verified", "cancelled"}:
                await self._status(identity, state="cancelled", reason="cancelled")
            return self.status(identity)

    async def aclose(self) -> None:
        for task in self._tasks.values():
            if not task.done():
                task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        await self.client.aclose()
