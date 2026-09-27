"""Bounded, node-owned preparation of isolated MCP repository workspaces."""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import os
import shutil
import signal
import socket
import stat
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

from coire_core.models.node import (
    WorkspaceCleanupRequest,
    WorkspacePrepareRequest,
    WorkspacePrepareResult,
)
from coire_core.settings import Settings


class WorkspaceError(RuntimeError):
    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        super().__init__(detail)


class WorkspaceManager:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.root = Path(settings.run_workspace_root).resolve()
        self._locks: dict[uuid.UUID, asyncio.Lock] = {}

    @staticmethod
    def workspace_ref(run_id: uuid.UUID) -> str:
        return f"mcp-{run_id.hex}"

    @staticmethod
    def output_ref(run_id: uuid.UUID) -> str:
        return f"mcp-out-{run_id.hex}"

    def _source_host(self, command: WorkspacePrepareRequest) -> str:
        url = command.source.repository_url
        if url is None:
            raise WorkspaceError(
                "workspace_source_unresolved",
                "registered workspace must be resolved by the API before node preparation",
            )
        host = (url.host or "").lower().rstrip(".")
        allowed = {part.strip().lower() for part in self.settings.mcp_source_hosts.split(",")}
        if not host or host not in allowed or url.port not in {None, 443}:
            raise WorkspaceError("workspace_source_denied", "repository host is not allowed")
        if url.query or url.fragment or url.username or url.password:
            raise WorkspaceError("workspace_source_denied", "repository URL has forbidden parts")
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            raise WorkspaceError(
                "workspace_source_denied", "literal IP repository hosts are denied"
            )
        return host

    async def _check_public_dns(self, host: str) -> None:
        try:
            addresses = await asyncio.get_running_loop().getaddrinfo(
                host, 443, type=socket.SOCK_STREAM
            )
        except OSError as exc:
            raise WorkspaceError(
                "workspace_source_unreachable", "repository host did not resolve"
            ) from exc
        if not addresses or any(
            not ipaddress.ip_address(item[4][0]).is_global for item in addresses
        ):
            raise WorkspaceError(
                "workspace_source_denied", "repository host resolves outside public space"
            )

    @staticmethod
    def _size(path: Path) -> int:
        total = 0
        for base, _directories, files in os.walk(path, followlinks=False):
            for name in files:
                try:
                    total += (Path(base) / name).lstat().st_size
                except FileNotFoundError:
                    continue
        return total

    async def _git(
        self, args: list[str], *, cwd: Path, max_bytes: int, timeout_seconds: int
    ) -> str:
        git = "/usr/bin/git"
        environment = {
            "PATH": "/usr/bin:/bin",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ALLOW_PROTOCOL": "https",
            "GIT_LFS_SKIP_SMUDGE": "1",
            "LANG": "C",
        }
        try:
            proc = await asyncio.create_subprocess_exec(
                git,
                "-c",
                "http.followRedirects=false",
                "-c",
                "core.hooksPath=/dev/null",
                *args,
                cwd=cwd,
                env=environment,
                stdout=asyncio.subprocess.PIPE
                if args[0] == "rev-parse"
                else asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                start_new_session=True,
            )
        except FileNotFoundError as exc:
            raise WorkspaceError(
                "workspace_git_missing", "Studio git executable is unavailable"
            ) from exc
        started = time.monotonic()
        try:
            while True:
                try:
                    await asyncio.wait_for(proc.wait(), timeout=0.2)
                    break
                except TimeoutError as exc:
                    if time.monotonic() - started > timeout_seconds:
                        os.killpg(proc.pid, signal.SIGKILL)
                        await proc.wait()
                        raise WorkspaceError(
                            "workspace_clone_timeout", "repository preparation timed out"
                        ) from exc
                    if self._size(cwd) > max_bytes:
                        os.killpg(proc.pid, signal.SIGKILL)
                        await proc.wait()
                        raise WorkspaceError(
                            "workspace_too_large", "repository exceeded workspace cap"
                        ) from exc
        except asyncio.CancelledError:
            if proc.returncode is None:
                os.killpg(proc.pid, signal.SIGKILL)
                await proc.wait()
            raise
        output = await proc.stdout.read(1024) if proc.stdout is not None else b""
        if proc.returncode != 0:
            raise WorkspaceError(
                "workspace_git_failed", f"repository preparation failed at git {args[0]}"
            )
        if self._size(cwd) > max_bytes:
            raise WorkspaceError("workspace_too_large", "repository exceeded workspace cap")
        return output.decode("utf-8", errors="replace").strip()

    @staticmethod
    def _allow_container_write(path: Path) -> None:
        for base, directories, files in os.walk(path, followlinks=False):
            for name in directories:
                target = Path(base) / name
                if not target.is_symlink():
                    target.chmod(target.stat().st_mode | stat.S_IRWXO)
            for name in files:
                target = Path(base) / name
                if not target.is_symlink():
                    target.chmod(target.stat().st_mode | stat.S_IROTH | stat.S_IWOTH)
        path.chmod(path.stat().st_mode | stat.S_IRWXO)

    async def prepare(self, command: WorkspacePrepareRequest) -> WorkspacePrepareResult:
        lock = self._locks.setdefault(command.run_id, asyncio.Lock())
        async with lock:
            return await self._prepare(command)

    async def _prepare(self, command: WorkspacePrepareRequest) -> WorkspacePrepareResult:
        host = self._source_host(command)
        await self._check_public_dns(host)
        maximum = min(command.max_bytes, self.settings.mcp_workspace_max_bytes)
        timeout = min(command.timeout_seconds, self.settings.mcp_workspace_prepare_timeout_s)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        workspace_ref = self.workspace_ref(command.run_id)
        output_ref = self.output_ref(command.run_id)
        workspace = self.root / workspace_ref
        output = self.root / output_ref
        fingerprint = hashlib.sha256(command.model_dump_json().encode()).hexdigest()
        manifest = workspace / ".coire" / "prepared.json"
        if workspace.exists() or output.exists():
            if not manifest.is_file() or not output.is_dir():
                raise WorkspaceError(
                    "workspace_prepare_conflict", "incomplete workspace requires operator recovery"
                )
            record = json.loads(manifest.read_text())
            if record.get("fingerprint") != fingerprint:
                raise WorkspaceError(
                    "workspace_prepare_conflict", "run ID already has another workspace request"
                )
            return WorkspacePrepareResult.model_validate(record["result"])

        workspace.mkdir(mode=0o700)
        output.mkdir(mode=0o777)
        output.chmod(0o777)
        try:
            await self._git(
                ["init", "--quiet"], cwd=workspace, max_bytes=maximum, timeout_seconds=timeout
            )
            url = command.source.repository_url
            assert url is not None
            await self._git(
                [
                    "fetch",
                    "--quiet",
                    "--depth",
                    "1",
                    "--no-tags",
                    str(url),
                    command.source.revision,
                ],
                cwd=workspace,
                max_bytes=maximum,
                timeout_seconds=timeout,
            )
            await self._git(
                ["checkout", "--quiet", "--detach", "FETCH_HEAD"],
                cwd=workspace,
                max_bytes=maximum,
                timeout_seconds=timeout,
            )
            revision = await self._git(
                ["rev-parse", "HEAD"], cwd=workspace, max_bytes=maximum, timeout_seconds=timeout
            )
            if len(revision) != 40 or any(char not in "0123456789abcdef" for char in revision):
                raise WorkspaceError("workspace_revision_invalid", "fetched revision is invalid")
            control = workspace / ".coire"
            control.mkdir(mode=0o755)
            request_path = control / "request.json"
            request_path.write_text(command.harness_request.model_dump_json(), encoding="utf-8")
            request_path.chmod(0o444)
            result = WorkspacePrepareResult(
                run_id=command.run_id,
                workspace_ref=workspace_ref,
                output_ref=output_ref,
                source_revision=revision,
                prepared_at=datetime.now(UTC),
            )
            temporary = control / "prepared.json.tmp"
            temporary.write_text(
                json.dumps({"fingerprint": fingerprint, "result": result.model_dump(mode="json")}),
                encoding="utf-8",
            )
            temporary.replace(manifest)
            self._allow_container_write(workspace)
            return result
        except BaseException:
            await asyncio.to_thread(shutil.rmtree, workspace)
            await asyncio.to_thread(shutil.rmtree, output)
            raise

    async def cleanup(self, command: WorkspaceCleanupRequest) -> None:
        if command.preserve_for_recovery:
            return
        lock = self._locks.setdefault(command.run_id, asyncio.Lock())
        async with lock:
            for reference in (self.workspace_ref(command.run_id), self.output_ref(command.run_id)):
                target = self.root / reference
                if target.is_symlink():
                    raise WorkspaceError(
                        "workspace_cleanup_conflict", "workspace path is a symlink"
                    )
                if target.is_dir():
                    await asyncio.to_thread(shutil.rmtree, target)
