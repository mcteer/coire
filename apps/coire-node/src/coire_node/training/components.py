"""Fenced rank-component store and bounded, credential-free data-fabric imports."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import secrets
import shutil
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import anyio
import httpx
from fastapi import Depends, FastAPI
from opentelemetry import metrics, trace
from safetensors import safe_open

from coire_core.errors import TrainingConflict, TrainingValidationError
from coire_core.models.acquisition import ReservationRequest
from coire_core.models.training_node import (
    CheckpointWorkerState,
    MixtureSamplerState,
    TrainingArtifactGrantIssued,
    TrainingArtifactGrantRefresh,
    TrainingArtifactImportIntent,
    TrainingPrepareRequest,
    TrainingRankComponentManifest,
    TrainingRankGrantRequest,
    TrainingRankImportRequest,
    TrainingRankImportStatus,
    TrainingRankVerificationReceipt,
)
from coire_core.net import DataFabricClient
from coire_node.reservations import ReservationLedger
from coire_node.store import sha256_file, write_atomic
from coire_node.training.artifacts import data_peer_addresses
from coire_node.training.checkpoints import (
    MAX_STATE_BYTES,
    RankCheckpointComponent,
    _check_tensor_header,
    _decode_tree,
    _finish_file,
    _fsync_directory,
    _private_file,
)
from coire_node.training.journal import TrainingJournal

tracer = trace.get_tracer("coire.node.training")
outcomes = metrics.get_meter("coire.node.training").create_counter(
    "coire_training_component_imports_total"
)


def descriptor(
    identity: uuid.UUID, component: RankCheckpointComponent
) -> TrainingRankComponentManifest:
    state = component.state
    if state.world_size != 2:
        raise TrainingConflict("Rank component requires world size two")
    return TrainingRankComponentManifest(
        artifact_id=identity,
        job_id=state.job_id,
        attempt_id=state.attempt_id,
        fence=state.fence,
        update=state.completed_update,
        rank=state.rank,
        world_size=2,
        runtime_sha256=state.runtime_sha256,
        resolved_spec_sha256=state.resolved_spec_sha256,
        state=component.rank_manifest,
        files=list(component.files),
        total_bytes=sum(f.bytes for f in component.files),
    )


def verify_directory(
    directory: Path, manifest: TrainingRankComponentManifest
) -> RankCheckpointComponent:
    """Allocation-free tensor header/byte/state checks; no MLX import or executable state."""
    if any(p.is_symlink() for p in (directory, *directory.parents)):
        raise TrainingValidationError("Rank component path is linked")
    paths = {f.id: directory / f.name for f in manifest.files}
    for file in manifest.files:
        path = paths[file.id]
        _private_file(path)
        if path.stat().st_size != file.bytes or sha256_file(path) != file.sha256:
            raise TrainingConflict("Rank component file differs from its digest/size")
    rank = manifest.state
    _check_tensor_header(paths[rank.adapter_file_id], rank.adapter_tensors)
    _check_tensor_header(paths[rank.optimizer_file_id], rank.optimizer_tensors)
    path = paths[rank.state_file_id]
    if path.stat().st_size > MAX_STATE_BYTES:
        raise TrainingValidationError("Rank state exceeds its bound")
    state = CheckpointWorkerState.model_validate_json(path.read_bytes())
    tree = _decode_tree(state.optimizer_tree, {t.key: object() for t in rank.optimizer_tensors})
    if not isinstance(tree, dict) or "step" not in tree:
        raise TrainingValidationError("Rank checkpoint requires full optimizer counter")
    step_node = state.optimizer_tree
    if not isinstance(step_node, dict):
        raise TrainingValidationError("Rank optimizer tree must be a mapping")
    items = step_node.get("items")
    if not isinstance(items, dict):
        raise TrainingValidationError("Rank optimizer tree must contain its entries")
    step = items.get("step")
    if not isinstance(step, dict):
        raise TrainingValidationError("Rank optimizer step is missing")
    if step.get("kind") == "tensor":
        key = step.get("tensor")
        description = next((t for t in rank.optimizer_tensors if t.key == key), None)
        if (
            description is None
            or description.shape != []
            or description.dtype not in {"uint32", "uint64", "int32", "int64"}
        ):
            raise TrainingValidationError("Rank optimizer step must be an integer scalar")
        with safe_open(str(paths[rank.optimizer_file_id]), framework="np") as tensors:
            value = tensors.get_tensor(key).item()
    else:
        value = step.get("value")
    if type(value) is not int or value != state.completed_update:
        raise TrainingConflict("Rank optimizer counter differs from completed update")
    if isinstance(state.sampler, MixtureSamplerState) and (
        state.sampler.rank != state.rank or state.sampler.world_size != state.world_size
    ):
        raise TrainingConflict("Rank sampler differs from component scope")
    if (
        any(
            getattr(state, k) != getattr(manifest, k)
            for k in (
                "job_id",
                "attempt_id",
                "fence",
                "rank",
                "world_size",
                "runtime_sha256",
                "resolved_spec_sha256",
            )
        )
        or state.completed_update != manifest.update
    ):
        raise TrainingConflict("Rank component state differs from its immutable identity")
    return RankCheckpointComponent(directory, state, rank, tuple(manifest.files))


class TrainingComponents:
    def __init__(
        self,
        root: Path,
        journal: TrainingJournal,
        *,
        peer_addresses: Callable[[str], set[str]] = data_peer_addresses,
    ) -> None:
        self.root, self.journal, self.node_name = root, journal, journal.node
        if root.is_symlink() or self.node_name not in {"coire-edge-a", "coire-edge-b"}:
            raise TrainingValidationError("Components require an owned Studio store")
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.peer_addresses = peer_addresses
        self.lock = journal.lock
        self.grants: dict[
            uuid.UUID, tuple[TrainingRankGrantRequest, bytes, TrainingArtifactGrantIssued]
        ] = {}
        self.commands: dict[uuid.UUID, uuid.UUID] = {}

    def scope(
        self, manifest: TrainingRankComponentManifest, *, own: bool = False
    ) -> TrainingPrepareRequest:
        with self.journal.lock:
            value = self.journal.get(manifest.attempt_id)
            prepared = TrainingPrepareRequest.model_validate(value["prepare"])
            latest = max(
                v["prepare"]["fence"]
                for v in self.journal.records()
                if v["prepare"]["job_id"] == manifest.job_id
            )
            from coire_node.training.worker import resolved_digest

            if (
                manifest.job_id != prepared.job_id
                or manifest.fence != prepared.fence
                or manifest.fence != latest
                or prepared.world_size != 2
                or manifest.runtime_sha256 != prepared.resolved.runtime_sha256
                or manifest.resolved_spec_sha256 != resolved_digest(prepared)
                or value["released"]
                or value["liveness"] != "running"
                or value["reason"] in {"cancelled", "admin_cancel", "authorization_revoked"}
                or datetime.fromisoformat(value["lease_expires_at"]) <= datetime.now(UTC)
                or (own and manifest.rank != prepared.rank)
            ):
                raise TrainingConflict("Rank component has no current owned execution authority")
            return prepared

    def directory(self, identity: uuid.UUID, rank: int) -> Path:
        if not isinstance(identity, uuid.UUID) or type(rank) is not int or rank not in (0, 1):
            raise TrainingValidationError("Rank component identity is invalid")
        path = self.root / f".rank-components-{identity}" / f"rank-{rank}"
        if any(p.is_symlink() for p in (path, *path.parents)):
            raise TrainingValidationError("Rank component directory is linked")
        return path

    def register(
        self, identity: uuid.UUID, component: RankCheckpointComponent
    ) -> TrainingRankComponentManifest:
        manifest = descriptor(identity, component)
        self.scope(manifest, own=True)
        directory = self.directory(identity, manifest.rank)
        if directory != component.directory:
            raise TrainingConflict("Rank component is outside its owned store")
        verify_directory(directory, manifest)
        path = directory / "component.json"
        if path.exists() and self.manifest(identity, manifest.rank) != manifest:
            raise TrainingConflict("Rank component descriptor is immutable")
        write_atomic(path, manifest.model_dump_json().encode())
        _fsync_directory(directory)
        return manifest

    def manifest(self, identity: uuid.UUID, rank: int) -> TrainingRankComponentManifest:
        path = self.directory(identity, rank) / "component.json"
        _private_file(path)
        if path.stat().st_size > MAX_STATE_BYTES:
            raise TrainingValidationError("Rank descriptor exceeds its bound")
        value = TrainingRankComponentManifest.model_validate_json(path.read_bytes())
        if value.artifact_id != identity or value.rank != rank:
            raise TrainingConflict("Rank descriptor filename differs from its identity")
        return value

    def verify(self, manifest: TrainingRankComponentManifest) -> RankCheckpointComponent:
        self.scope(manifest)
        if self.manifest(manifest.artifact_id, manifest.rank) != manifest:
            raise TrainingConflict("Local rank descriptor differs")
        return verify_directory(self.directory(manifest.artifact_id, manifest.rank), manifest)

    def issue(self, request: TrainingRankGrantRequest) -> TrainingArtifactGrantIssued:
        request = TrainingRankGrantRequest.model_validate(request.model_dump(mode="json"))
        self.scope(request.component, own=True)
        with self.lock:
            grant_id = self.commands.get(request.command_id)
            prior = self.grants.get(grant_id) if grant_id is not None else None
            if prior is not None:
                if prior[0] != request:
                    raise TrainingConflict("Rank grant command changed")
                return prior[2]
            now = datetime.now(UTC)
            self.grants = {k: v for k, v in self.grants.items() if v[0].expires_at > now}
            self.commands = {k: v for k, v in self.commands.items() if v in self.grants}
            if (
                request.source_node != self.node_name
                or not 0 < (request.expires_at - now).total_seconds() <= 60
                or len(self.grants) >= 32
            ):
                raise TrainingConflict("Rank grant source/expiry/capacity is invalid")
            self.verify(request.component)
            issued = TrainingArtifactGrantIssued(
                grant_id=uuid.uuid4(),
                secret=secrets.token_urlsafe(32),
                expires_at=request.expires_at,
            )
            self.grants[issued.grant_id] = (
                request,
                hashlib.sha256(issued.secret.encode()).digest(),
                issued,
            )
            self.commands[request.command_id] = issued.grant_id
            return issued

    def revoke(self, identity: uuid.UUID) -> None:
        with self.lock:
            self.grants.pop(identity, None)

    def authorize(
        self, secret: str, identity: uuid.UUID, rank: int, peer: str, file_id: str | None = None
    ) -> TrainingRankComponentManifest | None:
        if not 32 <= len(secret) <= 256:
            return None
        digest = hashlib.sha256(secret.encode()).digest()
        with self.lock:
            for scope, expected, _ in self.grants.values():
                if not hmac.compare_digest(digest, expected):
                    continue
                if (
                    scope.expires_at <= datetime.now(UTC)
                    or scope.artifact_id != identity
                    or scope.component.rank != rank
                    or peer not in self.peer_addresses(scope.destination_node)
                    or (file_id is not None and file_id not in scope.file_ids)
                ):
                    return None
                self.scope(scope.component, own=True)
                return scope.component if self.manifest(identity, rank) == scope.component else None
        return None

    def attach(self, control: FastAPI, data: FastAPI, *, importer: RankImporter) -> None:
        from coire_node.routes.training_components import control_router, data_router

        control.state.training_components = data.state.training_components = self
        control.state.training_rank_importer = importer
        control.include_router(
            control_router, dependencies=[Depends(control.state.require_node_token)]
        )
        data.include_router(data_router)


class RankImporter:
    """Controller-started peer imports; retries keep their original absolute deadline."""

    def __init__(
        self,
        components: TrainingComponents,
        journal_root: Path,
        *,
        port: int,
        client: DataFabricClient | None = None,
        max_bytes: int = 200 * 1024**3,
        disk_floor_bytes: int = 20 * 1024**3,
        reservations: ReservationLedger | None = None,
    ) -> None:
        self.components, self.root, self.port = components, journal_root, port
        if journal_root.is_symlink():
            raise TrainingValidationError("Rank import journal is linked")
        journal_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.owned_client = (
            httpx.AsyncClient(timeout=5, follow_redirects=False, trust_env=False)
            if client is None
            else None
        )
        self.client = client or DataFabricClient(client=self.owned_client)
        self.max_bytes, self.floor = max_bytes, disk_floor_bytes
        self.records: dict[
            uuid.UUID, tuple[TrainingArtifactImportIntent, TrainingRankImportStatus]
        ] = {}
        self.tasks: dict[uuid.UUID, asyncio.Task[None]] = {}
        self.deadlines: dict[uuid.UUID, float] = {}
        self.holds: dict[uuid.UUID, int] = {}
        self.lock = asyncio.Lock()
        self.slots = asyncio.Semaphore(1)
        self.reservations = reservations
        self.reservation_ids: dict[uuid.UUID, list[uuid.UUID]] = {}
        if reservations is not None:
            for path in self.root.glob("*.json"):
                identity = uuid.UUID(path.stem)
                self.status(identity)
                self.release_reservations(identity)

    def release_reservations(self, identity: uuid.UUID) -> None:
        if self.reservations is not None:
            for scope in self.reservation_ids.get(identity, []):
                if self.reservations.get(scope) is not None:
                    self.reservations.bind_owner(
                        scope, release_check=lambda: True, footprint_bytes=lambda: None
                    )
                    self.reservations.release(scope)

    def status(self, identity: uuid.UUID) -> TrainingRankImportStatus:
        if identity not in self.records:
            path = self.root / f"{identity}.json"
            _private_file(path)
            if path.stat().st_size > MAX_STATE_BYTES:
                raise TrainingValidationError("Rank import journal is oversized")
            import json

            value = json.loads(path.read_bytes())
            intent = TrainingArtifactImportIntent.model_validate(value["intent"])
            status = TrainingRankImportStatus.model_validate(value["status"])
            if (
                intent.command_id != identity
                or status.import_id != identity
                or intent.manifest_sha256 != status.component.canonical_sha256()
                or intent.artifact_id != status.component.artifact_id
                or intent.attempt_id != status.component.attempt_id
                or intent.fence != status.component.fence
                or intent.destination_node != self.components.node_name
            ):
                raise TrainingConflict("Rank import journal identity differs")
            self.records[identity] = intent, status
            scopes = value.get("reservation_ids", [])
            if not isinstance(scopes, list) or len(scopes) > 4:
                raise TrainingValidationError("Rank import reservation journal exceeds bounds")
            self.reservation_ids[identity] = [uuid.UUID(scope) for scope in scopes]
            if status.state in {"staging", "transferring"}:
                status = status.model_copy(
                    update={"state": "failed", "reason": "replication_failed"}
                )
                self.persist(identity, status)
            if len(self.records) > 64:
                old = [
                    key
                    for key in self.records
                    if key != identity and (key not in self.tasks or self.tasks[key].done())
                ]
                for key in old[: len(self.records) - 64]:
                    self.records.pop(key, None)
                    self.reservation_ids.pop(key, None)
                    self.deadlines.pop(key, None)
        return self.records[identity][1]

    def persist(self, identity: uuid.UUID, status: TrainingRankImportStatus) -> None:
        import json

        intent = self.records[identity][0]
        write_atomic(
            self.root / f"{identity}.json",
            json.dumps(
                {
                    "intent": intent.model_dump(mode="json"),
                    "status": status.model_dump(mode="json"),
                    "reservation_ids": [
                        str(scope) for scope in self.reservation_ids.get(identity, [])
                    ],
                }
            ).encode(),
        )
        _fsync_directory(self.root)
        self.records[identity] = intent, status

    async def start(self, request: TrainingRankImportRequest) -> TrainingRankImportStatus:
        request = TrainingRankImportRequest.model_validate(request.model_dump(mode="json"))
        async with self.lock:
            self.components.scope(request.component)
            if request.destination_node != self.components.node_name:
                raise TrainingConflict("Rank import targets another Studio")
            intent = TrainingArtifactImportIntent.model_validate(
                request.model_dump(mode="json", exclude={"grant_secret", "component"})
            )
            path = self.root / f"{request.command_id}.json"
            if request.command_id in self.records or path.exists():
                prior = self.status(request.command_id)
                if (
                    self.records[request.command_id][0] != intent
                    or prior.component != request.component
                ):
                    raise TrainingConflict("Rank import intent is immutable")
                return prior
            if len([t for t in self.tasks.values() if not t.done()]) >= 32:
                raise TrainingConflict("Rank import queue is full")
            status = TrainingRankImportStatus(
                import_id=request.command_id,
                component=request.component,
                state="staging",
                transferred_bytes=0,
            )
            self.records[request.command_id] = intent, status
            self.deadlines[request.command_id] = time.monotonic() + 60
            self.persist(request.command_id, status)
            self.tasks[request.command_id] = asyncio.create_task(self.run(request))
            return status

    async def refresh(
        self, identity: uuid.UUID, request: TrainingArtifactGrantRefresh
    ) -> TrainingRankImportStatus:
        async with self.lock:
            status = self.status(identity)
            intent = self.records[identity][0]
            self.components.scope(status.component)
            if any(
                getattr(request, k) != getattr(intent, k)
                for k in ("artifact_id", "manifest_sha256", "attempt_id", "fence")
            ):
                raise TrainingConflict("Rank grant refresh expands scope")
            if status.state == "verified":
                return status
            if (
                status.state == "cancelled"
                or identity not in self.deadlines
                or time.monotonic() >= self.deadlines[identity]
            ):
                raise TrainingConflict("Rank import deadline/cancellation cannot be resurrected")
            if identity in self.tasks and not self.tasks[identity].done():
                raise TrainingConflict("Rank import is still running")
            intent = intent.model_copy(update={"grant_id": request.grant_id})
            self.records[identity] = intent, status
            self.persist(identity, status)
            command = TrainingRankImportRequest.model_validate(
                {
                    **intent.model_dump(mode="json"),
                    "grant_secret": request.grant_secret,
                    "component": status.component.model_dump(mode="json"),
                }
            )
            self.tasks[identity] = asyncio.create_task(self.run(command))
            return status

    async def cancel(self, identity: uuid.UUID) -> TrainingRankImportStatus:
        async with self.lock:
            status = self.status(identity)
            task = self.tasks.get(identity)
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            if status.state != "verified":
                self.persist(
                    identity,
                    status.model_copy(update={"state": "cancelled", "reason": "cancelled"}),
                )
            return self.status(identity)

    async def run(self, request: TrainingRankImportRequest) -> None:
        identity, manifest = request.command_id, request.component
        threads: list[asyncio.Task[Any]] = []

        async def owned_thread(function: Callable[..., Any], *args: Any) -> Any:
            task = asyncio.create_task(asyncio.to_thread(function, *args))
            threads.append(task)
            return await asyncio.shield(task)

        def authorized() -> None:
            self.components.scope(manifest)
            if time.monotonic() >= self.deadlines[identity]:
                raise TrainingConflict("Rank transfer deadline expired")

        def update(**changes: Any) -> None:
            self.persist(
                identity,
                TrainingRankImportStatus.model_validate(
                    {**self.status(identity).model_dump(mode="json"), **changes}
                ),
            )

        try:
            async with (
                asyncio.timeout(max(0, self.deadlines[identity] - time.monotonic())),
                self.slots,
            ):
                with tracer.start_as_current_span("coire.node.training.component.import"):
                    authorized()
                    destination = self.components.directory(manifest.artifact_id, manifest.rank)
                    if self.reservations is not None:
                        scopes = self.reservation_ids.setdefault(identity, [])
                        if len(scopes) >= 4:
                            raise TrainingConflict("Rank import retry reservation bound exhausted")
                        scope_id = uuid.uuid4()
                        scopes.append(scope_id)
                        self.persist(identity, self.status(identity))
                        self.reservations.hold(
                            ReservationRequest(
                                idempotency_key=scope_id,
                                workflow_id=identity,
                                variant_id=manifest.artifact_id,
                                memory_bytes=192 * 1024**2,
                                disk_bytes=manifest.total_bytes,
                            ),
                            disk_path=self.components.root,
                            disk_floor_bytes=self.floor,
                            require_stop=True,
                            materialized_paths=(
                                destination,
                                destination.parent / f".import-rank-{manifest.rank}",
                            ),
                        )
                    if destination.exists():
                        await owned_thread(self.components.verify, manifest)
                    else:
                        used = await owned_thread(
                            lambda: sum(
                                p.stat().st_size
                                for p in self.components.root.rglob("*")
                                if p.is_file() and not p.is_symlink()
                            )
                        )
                        if (
                            used + sum(self.holds.values()) + manifest.total_bytes > self.max_bytes
                            or shutil.disk_usage(self.components.root).free
                            - sum(self.holds.values())
                            - manifest.total_bytes
                            < self.floor
                        ):
                            raise TrainingValidationError("Rank import disk reservation cannot fit")
                        self.holds[identity] = manifest.total_bytes
                        parent = destination.parent
                        parent.mkdir(mode=0o700, exist_ok=True)
                        staging = parent / f".import-rank-{manifest.rank}"
                        if staging.is_symlink():
                            raise TrainingValidationError("Rank staging is linked")
                        staging.mkdir(mode=0o700, exist_ok=True)
                        transferred = 0
                        update(state="transferring", reason=None)
                        for file in manifest.files:
                            authorized()
                            path = staging / file.name
                            if not path.exists():
                                fd = os.open(
                                    path,
                                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                    0o600,
                                )
                                os.close(fd)
                            _private_file(path)
                            offset = path.stat().st_size
                            if offset > file.bytes:
                                raise TrainingConflict("Rank partial file exceeds bound")
                            if offset < file.bytes:
                                headers = {"X-Coire-Artifact-Grant": request.grant_secret}
                                if offset:
                                    headers["Range"] = f"bytes={offset}-"
                                async with self.client.stream(
                                    "GET",
                                    request.source_node,
                                    f"/training-components/{manifest.artifact_id}/ranks/{manifest.rank}/files/{file.id}",
                                    port=self.port,
                                    headers=headers,
                                ) as response:
                                    response.raise_for_status()
                                    if response.headers.get(
                                        "content-encoding", "identity"
                                    ) != "identity" or (
                                        offset
                                        and (
                                            response.status_code != 206
                                            or response.headers.get("content-range")
                                            != f"bytes {offset}-{file.bytes - 1}/{file.bytes}"
                                        )
                                    ):
                                        raise TrainingConflict(
                                            "Rank transfer Range/encoding differs"
                                        )
                                    async with await anyio.open_file(path, "ab") as output:
                                        async for chunk in response.aiter_bytes(64 * 1024):
                                            authorized()
                                            offset += len(chunk)
                                            if offset > file.bytes:
                                                raise TrainingConflict(
                                                    "Rank transfer exceeds declared bytes"
                                                )
                                            await output.write(chunk)
                                        await output.flush()
                            await owned_thread(_finish_file, path)
                            if (
                                path.stat().st_size != file.bytes
                                or await owned_thread(sha256_file, path) != file.sha256
                            ):
                                path.unlink()
                                raise TrainingConflict("Rank transfer digest differs")
                            transferred += file.bytes
                            update(transferred_bytes=transferred)
                        await owned_thread(verify_directory, staging, manifest)
                        authorized()
                        write_atomic(
                            staging / "component.json", manifest.model_dump_json().encode()
                        )
                        _fsync_directory(staging)
                        os.rename(staging, destination)
                        _fsync_directory(parent)
                    authorized()
                    receipt = TrainingRankVerificationReceipt(
                        command_id=identity,
                        component=manifest,
                        node=self.components.node_name,
                        verified_bytes=manifest.total_bytes,
                    )
                    update(
                        state="verified",
                        transferred_bytes=manifest.total_bytes,
                        receipt=receipt.model_dump(mode="json"),
                        reason=None,
                    )
                    outcomes.add(1, {"outcome": "verified"})
        except asyncio.CancelledError:
            update(state="cancelled", reason="cancelled")
            raise
        except Exception:
            update(state="failed", reason="replication_failed")
            outcomes.add(1, {"outcome": "failed"})
        finally:
            drained = asyncio.gather(*threads, return_exceptions=True)
            while not drained.done():
                try:
                    await asyncio.shield(drained)
                except asyncio.CancelledError:
                    continue
            self.holds.pop(identity, None)
            self.release_reservations(identity)
            finished = [k for k, task in self.tasks.items() if task.done()]
            for key in finished[:-8]:
                self.tasks.pop(key, None)
                self.records.pop(key, None)
                self.deadlines.pop(key, None)
                self.reservation_ids.pop(key, None)

    async def aclose(self) -> None:
        for task in self.tasks.values():
            task.cancel()
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)
        await self.client.aclose()
        if self.owned_client is not None:
            await self.owned_client.aclose()
