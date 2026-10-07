"""Private immutable artifact reads with exact manifest/file/peer-bound short grants."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import shutil
import socket
import stat
import threading
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import FastAPI

from coire_core.models.training_node import (
    TrainingArtifactDeleteRequest,
    TrainingArtifactDeletionReceipt,
    TrainingArtifactGrantIssued,
    TrainingArtifactGrantRequest,
    TrainingArtifactManifest,
)
from coire_node.store import sha256_file

if TYPE_CHECKING:
    from coire_node.training.importer import ArtifactImporter


def data_peer_addresses(peer: str) -> set[str]:
    if peer not in {"coire-edge-a", "coire-edge-b"}:
        return set()
    try:
        return {
            str(info[4][0])
            for info in socket.getaddrinfo(f"{peer}.fabric", None, type=socket.SOCK_STREAM)
        }
    except OSError:
        return set()


class TrainingArtifacts:
    def __init__(
        self,
        root: Path,
        *,
        node_name: str,
        peer_addresses: Callable[[str], set[str]] = data_peer_addresses,
        referenced: Callable[[uuid.UUID], bool] | None = None,
        admission_lock: threading.RLock | None = None,
    ) -> None:
        if node_name not in {"coire-edge-a", "coire-edge-b"} or root.is_symlink():
            raise ValueError("artifact store requires a declared Studio and contained root")
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.root = root.resolve()
        self.node_name = node_name
        self.peer_addresses = peer_addresses
        self._lock = admission_lock or threading.RLock()
        self.referenced = referenced
        self._grants: dict[
            uuid.UUID, tuple[TrainingArtifactGrantRequest, bytes, TrainingArtifactGrantIssued]
        ] = {}
        self._commands: dict[uuid.UUID, uuid.UUID] = {}

    def directory(self, identity: uuid.UUID) -> Path:
        if not isinstance(identity, uuid.UUID):
            raise ValueError("artifact identity must be a UUID")
        if (self.root / f".deletion-{identity}.json").exists():
            raise ValueError("artifact is retired")
        directory = self.root / str(identity)
        if directory.is_symlink() or not directory.is_dir():
            raise ValueError("artifact is missing or linked")
        return directory

    def manifest(self, identity: uuid.UUID) -> TrainingArtifactManifest:
        path = self.directory(identity) / "manifest.json"
        info = path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_mode & 0o077
            or info.st_size > 64 * 1024**2
        ):
            raise ValueError("artifact manifest is unsafe")
        manifest = TrainingArtifactManifest.model_validate_json(path.read_bytes())
        if manifest.artifact_id != identity:
            raise ValueError("artifact manifest identity differs")
        return manifest

    def file(self, manifest: TrainingArtifactManifest, file_id: str) -> Path:
        entry = next((entry for entry in manifest.files if entry.id == file_id), None)
        if entry is None:
            raise ValueError("file is outside the artifact manifest")
        path = self.directory(manifest.artifact_id) / entry.name
        info = path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_mode & 0o077
            or info.st_size != entry.bytes
        ):
            raise ValueError("artifact file is missing, linked or differs")
        return path

    def issue(self, request: TrainingArtifactGrantRequest) -> TrainingArtifactGrantIssued:
        request = TrainingArtifactGrantRequest.model_validate(request.model_dump(mode="json"))
        now = datetime.now(UTC)
        with self._lock:
            prior_id = self._commands.get(request.command_id)
            if prior_id in self._grants:
                original, _digest, issued = self._grants[prior_id]
                if original != request:
                    raise ValueError("artifact grant command identifies different intent")
                return issued
            expired = [
                identity
                for identity, (scope, _digest, _issued) in self._grants.items()
                if scope.expires_at <= now
            ]
            for identity in expired:
                del self._grants[identity]
            self._commands = {
                command: identity
                for command, identity in self._commands.items()
                if identity in self._grants
            }
            if len(self._grants) >= 32:
                raise ValueError("artifact grant capacity is exhausted")
            if (
                request.source_node != self.node_name
                or request.expires_at <= now
                or (request.expires_at - now).total_seconds() > 60
            ):
                raise ValueError("artifact grant source or expiry is invalid")
            manifest = self.manifest(request.artifact_id)
            if manifest.canonical_sha256() != request.manifest_sha256:
                raise ValueError("artifact grant manifest differs")
            granted = [entry for entry in manifest.files if entry.id in request.file_ids]
            if (
                len(granted) != len(request.file_ids)
                or sum(entry.bytes for entry in granted) > request.max_bytes
            ):
                raise ValueError("artifact grant file/byte scope is invalid")
            for entry in granted:
                if sha256_file(self.file(manifest, entry.id)) != entry.sha256:
                    raise ValueError("artifact grant file checksum differs")
            issued = TrainingArtifactGrantIssued(
                grant_id=uuid.uuid4(),
                secret=secrets.token_urlsafe(32),
                expires_at=request.expires_at,
            )
            digest = hashlib.sha256(issued.secret.encode()).digest()
            self._grants[issued.grant_id] = request, digest, issued
            self._commands[request.command_id] = issued.grant_id
            return issued

    def authorize(
        self, secret: str, identity: uuid.UUID, peer_address: str, *, file_id: str | None = None
    ) -> TrainingArtifactManifest | None:
        if not 32 <= len(secret) <= 256:
            return None
        candidate = hashlib.sha256(secret.encode()).digest()
        with self._lock:
            for scope, digest, _issued in self._grants.values():
                if not hmac.compare_digest(candidate, digest):
                    continue
                if (
                    scope.expires_at <= datetime.now(UTC)
                    or scope.artifact_id != identity
                    or peer_address not in self.peer_addresses(scope.destination_node)
                    or (file_id is not None and file_id not in scope.file_ids)
                ):
                    return None
                manifest = self.manifest(identity)
                return manifest if manifest.canonical_sha256() == scope.manifest_sha256 else None
        return None

    def revoke(self, identity: uuid.UUID) -> None:
        with self._lock:
            self._grants.pop(identity, None)

    def verify(self, identity: uuid.UUID, expected_sha256: str) -> TrainingArtifactManifest:
        manifest = self.manifest(identity)
        if manifest.canonical_sha256() != expected_sha256:
            raise ValueError("artifact verification manifest differs")
        for entry in manifest.files:
            if sha256_file(self.file(manifest, entry.id)) != entry.sha256:
                raise ValueError("artifact verification checksum differs")
        return manifest

    def _commit_deletion_intent(self, path: Path, value: dict[str, object]) -> None:
        staging = self.root / f".deletion-staging-{uuid.uuid4()}"
        try:
            with staging.open("x", encoding="utf-8") as stream:
                staging.chmod(0o600)
                json.dump(value, stream, sort_keys=True, separators=(",", ":"), allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(staging, path)
            self._sync_root()
        finally:
            staging.unlink(missing_ok=True)

    def _sync_root(self) -> None:
        fd = os.open(self.root, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    @staticmethod
    def _contained_tree(directory: Path, allowed: set[str] | None = None) -> None:
        if directory.is_symlink() or not directory.is_dir():
            raise ValueError("artifact cleanup directory is missing or linked")
        for entry in directory.rglob("*"):
            info = entry.lstat()
            if (
                stat.S_ISLNK(info.st_mode)
                or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode))
                or (stat.S_ISREG(info.st_mode) and info.st_nlink != 1)
            ):
                raise ValueError("artifact cleanup contains linked or special content")
            if (
                allowed is not None
                and stat.S_ISREG(info.st_mode)
                and entry.relative_to(directory).as_posix() not in allowed
            ):
                raise ValueError("artifact cleanup contains unlisted content")

    def delete(
        self, identity: uuid.UUID, request: TrainingArtifactDeleteRequest
    ) -> TrainingArtifactDeletionReceipt:
        """Persist retirement before hiding bytes; replay resumes only that exact purge.

        Core owns reference authority. The local callback independently protects
        native trainers/engines/imports under the shared admission lock. Missing
        proof, active grants and uncertain filesystem state retain counted bytes.
        """
        request = TrainingArtifactDeleteRequest.model_validate(request.model_dump(mode="json"))
        if not isinstance(identity, uuid.UUID) or request.expected_version != 1:
            raise ValueError("artifact immutable version differs")
        with self._lock:
            intent = self.root / f".deletion-{identity}.json"
            hidden = self.root / f".deleting-{identity}"
            visible = self.root / str(identity)
            staging = self.root / f".import-{identity}"
            value: dict[str, object] = {
                "artifact_id": str(identity),
                "request": request.model_dump(mode="json"),
                "purged": False,
            }
            prior: dict[str, object] | None = None
            if intent.exists() or intent.is_symlink():
                info = intent.lstat()
                # Retirement journals embed the authoritative manifest plus its
                # file-name inventory. Allow the bounded manifest envelope, not
                # just a small receipt, so full-state purges survive restart.
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_nlink != 1
                    or info.st_mode & 0o077
                    or info.st_size > 128 * 1024**2
                ):
                    raise ValueError("artifact deletion intent is unsafe")
                prior = json.loads(intent.read_bytes())
                if (
                    not isinstance(prior, dict)
                    or prior.get("artifact_id") != str(identity)
                    or (
                        TrainingArtifactDeleteRequest.model_validate(prior.get("request"))
                        != request
                    )
                ):
                    raise ValueError("artifact deletion intent differs")
                if prior.get("purged") is True:
                    if (
                        visible.exists()
                        or hidden.exists()
                        or staging.exists()
                        or (self.root / f".rank-components-{identity}").exists()
                    ):
                        raise ValueError("purged artifact reappeared")
                    return TrainingArtifactDeletionReceipt(
                        command_id=request.command_id,
                        artifact_id=identity,
                        purged=True,
                    )
            else:
                # Erasure authority binds the immutable manifest, not the current
                # tensor checksum: corruption must not make unreferenced bytes immortal.
                if visible.exists() or visible.is_symlink():
                    manifest = self.manifest(identity)
                else:
                    if request.expected_manifest is None:
                        raise ValueError("absent artifact requires authoritative manifest scope")
                    manifest = request.expected_manifest
                    if manifest.artifact_id != identity:
                        raise ValueError("absent artifact identity differs from cleanup scope")
                if manifest.canonical_sha256() != request.manifest_sha256:
                    raise ValueError("artifact cleanup manifest differs")
                if visible.exists():
                    self._contained_tree(
                        visible, {"manifest.json", *(f.name for f in manifest.files)}
                    )
                value["kind"] = manifest.kind
                value["files"] = ["manifest.json", *(file.name for file in manifest.files)]
                if hidden.exists() or hidden.is_symlink():
                    raise ValueError("artifact cleanup has unowned staging")
            if prior is not None:
                value = prior
            if self.referenced is None or self.referenced(identity):
                raise ValueError("artifact reference proof unavailable")
            if any(
                scope.artifact_id == identity and scope.expires_at > datetime.now(UTC)
                for scope, _, _ in self._grants.values()
            ):
                raise ValueError("artifact transfer grant is still active")
            if not intent.exists():
                self._commit_deletion_intent(intent, value)
            if visible.exists() or visible.is_symlink():
                self._contained_tree(visible)
                if hidden.exists() or hidden.is_symlink():
                    raise ValueError("artifact cleanup has competing directories")
                os.rename(visible, hidden)
                self._sync_root()
            if hidden.exists() or hidden.is_symlink():
                self._contained_tree(hidden)
                shutil.rmtree(hidden)
                self._sync_root()
            if staging.exists() or staging.is_symlink():
                names = value.get("files")
                if not isinstance(names, list) or any(not isinstance(name, str) for name in names):
                    raise ValueError("import cleanup has no immutable file scope")
                self._contained_tree(staging, set(names))
                shutil.rmtree(staging)
                self._sync_root()
            components = self.root / f".rank-components-{identity}"
            if components.exists() or components.is_symlink():
                if value.get("kind") != "checkpoint":
                    raise ValueError("component cleanup lacks checkpoint ownership")
                self._contained_tree(components)
                shutil.rmtree(components)
                self._sync_root()
            self._commit_deletion_intent(intent, {**value, "purged": True})
            return TrainingArtifactDeletionReceipt(
                command_id=request.command_id,
                artifact_id=identity,
                purged=True,
            )

    def attach(
        self, control: FastAPI, data: FastAPI, *, importer: ArtifactImporter | None = None
    ) -> None:
        from fastapi import Depends

        from coire_node.routes.training_artifacts import control_router, data_router

        control.state.training_artifacts = self
        control.state.training_artifact_importer = importer
        data.state.training_artifacts = self
        control.include_router(
            control_router, dependencies=[Depends(control.state.require_node_token)]
        )
        data.include_router(data_router)
