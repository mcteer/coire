"""Bounded repository context and coding operations inside a Studio run container."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import re
import signal
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from pydantic import BaseModel

from coire_agent.activity import ActivitySpool
from coire_agent.harness import Harness
from coire_core.models.harness import HarnessRunRequest, HarnessRunResult, TaskClass
from coire_core.models.mcp import (
    ApplyDraft,
    ApplyResult,
    FileCitation,
    McpToolName,
    PlanDraft,
    PlanResult,
    ResearchDraft,
    ResearchResult,
    TestStatus,
    TestSummary,
)

MAX_CONTEXT_BYTES = 72 * 1024
MAX_FILE_BYTES = 64 * 1024
MAX_DIFF_BYTES = 1_048_576
MAX_ARTIFACT_BYTES = 64 * 1024 * 1024
MAX_TEST_OUTPUT_BYTES = 16_384
MAX_FILES = 256
MAX_SEARCH_HITS = 128


class CodingWorkspaceError(ValueError):
    pass


@dataclass(frozen=True)
class CommandResult:
    status: int
    head: bytes
    tail: bytes
    truncated: bool


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except FileNotFoundError:
        return -1


class CodingWorkspace:
    def __init__(self, root: Path, output: Path) -> None:
        self.root = root.resolve(strict=True)
        self.output = output.resolve(strict=True)
        if self.root == self.output or self.output.is_relative_to(self.root):
            raise CodingWorkspaceError("output directory must be separate from repository")

    def safe_path(self, relative: str, *, for_write: bool = False) -> Path:
        pure = PurePosixPath(relative)
        if (
            not relative
            or pure.is_absolute()
            or ".." in pure.parts
            or ".git" in pure.parts
            or ".coire" in pure.parts
            or relative.startswith("./")
        ):
            raise CodingWorkspaceError("repository path is not allowed")
        target = self.root.joinpath(*pure.parts)
        current = self.root
        for part in pure.parts:
            current = current / part
            if current.is_symlink():
                raise CodingWorkspaceError("repository symlink is not allowed")
        if not target.resolve(strict=False).is_relative_to(self.root):
            raise CodingWorkspaceError("repository path escapes workspace")
        if target.exists() and not target.is_file():
            raise CodingWorkspaceError("repository target is not a regular file")
        if not for_write and not target.is_file():
            raise CodingWorkspaceError("repository file is absent")
        return target

    def files(self) -> list[str]:
        result: list[str] = []
        for base, directories, files in os.walk(self.root, followlinks=False):
            directories[:] = sorted(
                name
                for name in directories
                if name not in {".git", ".coire"} and not (Path(base) / name).is_symlink()
            )
            for name in sorted(files):
                path = Path(base) / name
                if path.is_symlink() or not path.is_file():
                    continue
                relative = path.relative_to(self.root).as_posix()
                result.append(relative)
                if len(result) >= MAX_FILES:
                    return result
        return result

    def read_file(self, relative: str, *, max_bytes: int = MAX_FILE_BYTES) -> str:
        path = self.safe_path(relative)
        if path.stat().st_size > max_bytes:
            raise CodingWorkspaceError("repository file exceeds read cap")
        try:
            return path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise CodingWorkspaceError("repository file is not UTF-8 text") from exc

    def search(self, query: str) -> list[FileCitation]:
        needle = query.casefold().strip()
        if not needle or len(needle) > 256:
            raise CodingWorkspaceError("search query size is invalid")
        hits: list[FileCitation] = []
        for relative in self.files():
            try:
                content = self.read_file(relative)
            except CodingWorkspaceError:
                continue
            for line, value in enumerate(content.splitlines(), start=1):
                if needle in value.casefold():
                    hits.append(FileCitation(path=relative, line=line, excerpt=value[:2000]))
                    if len(hits) >= MAX_SEARCH_HITS:
                        return hits
        return hits

    def validate_citation(self, citation: FileCitation) -> None:
        lines = self.read_file(citation.path).splitlines()
        if citation.line > len(lines):
            raise CodingWorkspaceError("citation line is outside the source file")
        if (
            citation.excerpt is not None
            and citation.excerpt.strip() not in lines[citation.line - 1]
        ):
            raise CodingWorkspaceError("citation excerpt does not match the source line")

    def context(self) -> str:
        pieces: list[str] = []
        used = 0
        for relative in self.files():
            try:
                content = self.read_file(relative, max_bytes=8192)
            except CodingWorkspaceError:
                continue
            lines = "\n".join(
                f"{index}: {line[:500]}"
                for index, line in enumerate(content.splitlines()[:100], start=1)
            )
            piece = f"\n### {relative}\n{lines}\n"
            size = len(piece.encode("utf-8"))
            if used + size > MAX_CONTEXT_BYTES:
                break
            pieces.append(piece)
            used += size
        if not pieces:
            raise CodingWorkspaceError("repository has no readable source files")
        return "".join(pieces)

    def apply_edits(self, draft: ApplyDraft) -> list[str]:
        paths: list[str] = []
        for edit in draft.edits:
            target = self.safe_path(edit.path, for_write=True)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(edit.content, encoding="utf-8")
            paths.append(edit.path)
        return paths

    async def command(
        self,
        argv: list[str],
        *,
        timeout_seconds: float = 30,
        cap: int = 16_384,
        watch_file: Path | None = None,
        watch_limit: int | None = None,
    ) -> CommandResult:
        env = {
            "PATH": "/usr/bin:/bin:/app/.venv/bin",
            "HOME": "/tmp",
            "LANG": "C.UTF-8",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_ALLOW_PROTOCOL": "file",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        }
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=self.root,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
        head = bytearray()
        tail = bytearray()
        truncated = False

        async def drain() -> None:
            nonlocal truncated
            assert proc.stdout is not None
            while True:
                try:
                    chunk = await asyncio.wait_for(
                        proc.stdout.read(16_384), timeout=0.1 if watch_file else None
                    )
                except TimeoutError:
                    chunk = None
                if (
                    watch_file is not None
                    and watch_limit is not None
                    and await asyncio.to_thread(_file_size, watch_file) > watch_limit
                ):
                    raise CodingWorkspaceError("branch artifact exceeds size cap")
                if chunk is None:
                    continue
                if not chunk:
                    break
                remaining = max(0, cap - len(head))
                head.extend(chunk[:remaining])
                if len(chunk) > remaining:
                    truncated = True
                tail.extend(chunk)
                if len(tail) > MAX_TEST_OUTPUT_BYTES:
                    del tail[:-MAX_TEST_OUTPUT_BYTES]

        try:
            async with asyncio.timeout(timeout_seconds):
                await drain()
                await proc.wait()
        except BaseException:
            if proc.returncode is None:
                os.killpg(proc.pid, signal.SIGKILL)
                await proc.wait()
            raise
        return CommandResult(proc.returncode or 0, bytes(head), bytes(tail), truncated)

    async def git(
        self, *args: str, cap: int = 16_384, timeout_seconds: float = 30
    ) -> CommandResult:
        result = await self.command(
            [
                "/usr/bin/git",
                "-c",
                f"safe.directory={self.root}",
                "-c",
                "core.hooksPath=/dev/null",
                *args,
            ],
            cap=cap,
            timeout_seconds=timeout_seconds,
        )
        if result.status != 0:
            raise CodingWorkspaceError(
                f"git {args[0]} failed: {result.tail.decode(errors='replace')[:500]}"
            )
        return result

    async def revision(self) -> str:
        revision = (await self.git("rev-parse", "HEAD")).head.decode().strip()
        if not re.fullmatch(r"[a-f0-9]{40}", revision):
            raise CodingWorkspaceError("repository revision is invalid")
        return revision

    async def run_tests(self) -> TestSummary:
        files = self.files()
        python_tests = any(
            PurePosixPath(path).name.startswith("test_") and path.endswith(".py") for path in files
        ) or any(name in files for name in ("pytest.ini", "pyproject.toml"))
        if not python_tests:
            unsupported = next(
                (name for name in ("package.json", "Cargo.toml", "go.mod") if name in files),
                None,
            )
            if unsupported is not None:
                return TestSummary(status=TestStatus.UNSUPPORTED, command=[unsupported])
            return TestSummary(status=TestStatus.NOT_FOUND)
        argv = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"]
        if importlib.util.find_spec("pytest") is None:
            return TestSummary(status=TestStatus.UNSUPPORTED, command=argv)
        try:
            result = await self.command(argv, timeout_seconds=120, cap=MAX_TEST_OUTPUT_BYTES)
        except TimeoutError:
            return TestSummary(
                status=TestStatus.FAILED,
                command=argv,
                output_excerpt="pytest exceeded the 120 second test budget",
            )
        output = result.tail.decode("utf-8", errors="replace")
        counts = {
            name: int(match.group(1)) if (match := re.search(rf"(\d+) {name}", output)) else 0
            for name in ("passed", "failed", "skipped")
        }
        status = TestStatus.PASSED if result.status == 0 else TestStatus.FAILED
        if result.status == 5:
            status = TestStatus.NOT_FOUND
        return TestSummary(
            status=status,
            command=argv,
            passed=counts["passed"],
            failed=counts["failed"],
            skipped=counts["skipped"],
            output_excerpt=output[-MAX_TEST_OUTPUT_BYTES:],
        )


def _prompt(request: HarnessRunRequest, context: str, mode: McpToolName, schema: str) -> str:
    mode_instruction = {
        McpToolName.RESEARCH: "Answer the question using cited repository path and line numbers.",
        McpToolName.PLAN: "Return ordered implementation steps with acceptance criteria. Do not modify files.",
        McpToolName.APPLY: "Return complete UTF-8 content for each file to create or replace. Do not mention paths outside the repository.",
    }[mode]
    instruction = (
        f"{mode_instruction}\nReturn only JSON matching the requested schema. "
        "Surround the JSON object with <output> and </output>. Do not use Markdown fences.\n"
        f"Schema:\n{schema}\nUser task:\n{request.task}\nRepository files with line numbers:\n{context}"
    )
    if len(instruction) > 100_000:
        raise CodingWorkspaceError("coding context exceeds task cap")
    return instruction


async def run_coding(
    request: HarnessRunRequest,
    harness: Harness,
    *,
    workspace: CodingWorkspace,
    run_id: uuid.UUID,
) -> HarnessRunResult:
    mode, call_id = request.coding_mode, request.coding_call_id
    if mode is None or call_id is None:
        raise CodingWorkspaceError("MCP coding request is missing call identity")
    if request.task_class is TaskClass.READ and mode is McpToolName.APPLY:
        raise CodingWorkspaceError("read task cannot apply edits")
    activity = ActivitySpool(workspace.output / f"activity-{run_id}.jsonl", run_id)
    with activity.step("read_file"):
        revision = await workspace.revision()
        context = workspace.context()
    output_type: type[BaseModel]
    if mode is McpToolName.RESEARCH:
        output_type = ResearchDraft
    elif mode is McpToolName.PLAN:
        output_type = PlanDraft
    else:
        output_type = ApplyDraft
    schema = json.dumps(output_type.model_json_schema(), separators=(",", ":"))
    prompted = request.model_copy(update={"task": _prompt(request, context, mode, schema)})
    with activity.step("model_generation"):
        draft_result = await harness.run_structured(prompted, output_type)
    if mode is McpToolName.RESEARCH:
        research_draft = ResearchDraft.model_validate(draft_result.output)
        with activity.step("read_file"):
            for citation in research_draft.citations:
                workspace.validate_citation(citation)
        result: ResearchResult | PlanResult | ApplyResult = ResearchResult(
            result_id=call_id,
            run_id=run_id,
            source_revision=revision,
            answer=research_draft.answer,
            citations=research_draft.citations,
        )
    elif mode is McpToolName.PLAN:
        plan_draft = PlanDraft.model_validate(draft_result.output)
        result = PlanResult(
            result_id=call_id,
            run_id=run_id,
            source_revision=revision,
            goal=plan_draft.goal,
            steps=plan_draft.steps,
        )
    else:
        apply_draft = ApplyDraft.model_validate(draft_result.output)
        with activity.step("apply_patch"):
            branch = f"coire/{run_id.hex[:12]}-{uuid.uuid4().hex[:8]}"
            await workspace.git("switch", "-c", branch)
            paths = workspace.apply_edits(apply_draft)
            await workspace.git("add", "--", *paths)
            status = await workspace.command(
                [
                    "/usr/bin/git",
                    "-c",
                    f"safe.directory={workspace.root}",
                    "diff",
                    "--cached",
                    "--quiet",
                ],
            )
            if status.status == 0:
                raise CodingWorkspaceError("apply produced no changed files")
            if status.status != 1:
                raise CodingWorkspaceError("git diff failed")
            await workspace.git(
                "-c",
                "user.name=Coire",
                "-c",
                "user.email=coire@localhost",
                "commit",
                "-m",
                f"coire: {apply_draft.summary[:120]}",
            )
            head = await workspace.revision()
            diff = await workspace.git(
                "diff", "--no-ext-diff", f"{revision}..{head}", "--", cap=MAX_DIFF_BYTES + 1
            )
        with activity.step("run_tests"):
            tests = await workspace.run_tests()
        with activity.step("branch_bundle"):
            bundle = workspace.output / "branch.bundle"
            bundle_result = await workspace.command(
                [
                    "/usr/bin/git",
                    "-c",
                    f"safe.directory={workspace.root}",
                    "bundle",
                    "create",
                    str(bundle),
                    branch,
                ],
                timeout_seconds=120,
                watch_file=bundle,
                watch_limit=MAX_ARTIFACT_BYTES,
            )
            bundle_size = await asyncio.to_thread(_file_size, bundle)
            if bundle_result.status != 0 or bundle_size < 0:
                raise CodingWorkspaceError("branch artifact collection failed")
            if bundle_size > MAX_ARTIFACT_BYTES:
                raise CodingWorkspaceError("branch artifact exceeds size cap")
        excerpt = diff.head[:MAX_DIFF_BYTES].decode("utf-8", errors="replace")
        artifact_id = uuid.uuid4()
        result = ApplyResult(
            run_id=run_id,
            branch=branch,
            base_revision=revision,
            head_revision=head,
            diff_excerpt=excerpt,
            diff_truncated=diff.truncated or len(diff.head) > MAX_DIFF_BYTES,
            tests=tests,
            artifact_id=artifact_id,
            artifact_url=f"/api/v1/mcp/artifacts/{artifact_id}",
        )
    draft_result.output = result.model_dump(mode="json")
    draft_result.run_id = run_id
    return draft_result
