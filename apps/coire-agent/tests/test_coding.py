"""Exercise repository coding against a disposable local git checkout."""

from __future__ import annotations

import asyncio
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from pydantic import BaseModel

from coire_agent.coding import CodingWorkspace, CodingWorkspaceError, run_coding
from coire_core.models.harness import (
    ContextBudget,
    HarnessRunRequest,
    HarnessRunResult,
    ProfileName,
    TaskClass,
)
from coire_core.models.mcp import FileCitation, McpToolName
from coire_core.models.mcp import TestStatus as CodingTestStatus
from coire_core.models.registry import CapabilityProfile, StructuredOutput, ToolCalling
from coire_core.models.runs import RunActivity


class DraftHarness:
    def __init__(self, output: dict[str, object]) -> None:
        self.output = output

    async def run_structured(
        self, request: HarnessRunRequest, output_type: type[BaseModel]
    ) -> HarnessRunResult:
        draft = output_type.model_validate(self.output)
        return HarnessRunResult(
            run_id=uuid.uuid4(),
            profile=request.profile,
            variant_id=request.variant_id,
            output=draft.model_dump(mode="json"),
            context=ContextBudget(token_limit=4096),
        )


def checkout(tmp_path: Path) -> CodingWorkspace:
    root = tmp_path / "repo"
    root.mkdir()
    output = tmp_path / "output"
    output.mkdir()
    (root / "main.py").write_text("VALUE = 1\n")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", "main.py"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-qm",
            "initial",
        ],
        check=True,
    )
    return CodingWorkspace(root, output)


def request(mode: McpToolName) -> HarnessRunRequest:
    return HarnessRunRequest(
        profile=ProfileName.CODING,
        variant_id=uuid.uuid4(),
        task_class=TaskClass.WRITE if mode is McpToolName.APPLY else TaskClass.READ,
        coding_mode=mode,
        coding_call_id=uuid.uuid4(),
        task="Inspect or change VALUE",
        capability_profile=CapabilityProfile(
            tool_calling=ToolCalling.NATIVE,
            structured_output=StructuredOutput.JSON_MODE,
        ),
        context_window=4096,
    )


def _imported_head(workspace: CodingWorkspace, branch: str, destination: Path) -> str:
    subprocess.run(["git", "clone", "-q", str(workspace.root), str(destination)], check=True)
    subprocess.run(
        ["git", "-C", str(destination), "fetch", str(workspace.output / "branch.bundle"), branch],
        check=True,
    )
    return subprocess.check_output(
        ["git", "-C", str(destination), "rev-parse", "FETCH_HEAD"], text=True
    ).strip()


def _clones(source: CodingWorkspace, tmp_path: Path) -> list[CodingWorkspace]:
    clones: list[CodingWorkspace] = []
    for index in range(2):
        root = tmp_path / f"clone-{index}"
        subprocess.run(["git", "clone", "-q", str(source.root), str(root)], check=True)
        output = tmp_path / f"output-{index}"
        output.mkdir()
        clones.append(CodingWorkspace(root, output))
    return clones


async def test_research_validates_real_source_citations(tmp_path: Path) -> None:
    workspace = checkout(tmp_path)
    run = uuid.uuid4()
    result = await run_coding(
        request(McpToolName.RESEARCH),
        DraftHarness(
            {"answer": "One", "citations": [{"path": "main.py", "line": 1, "excerpt": "VALUE = 1"}]}
        ),  # type: ignore[arg-type]
        workspace=workspace,
        run_id=run,
    )
    assert result.run_id == run
    assert result.output["citations"][0]["line"] == 1
    assert workspace.search("value")[0] == FileCitation(path="main.py", line=1, excerpt="VALUE = 1")
    with pytest.raises(CodingWorkspaceError, match="does not match"):
        workspace.validate_citation(FileCitation(path="main.py", line=1, excerpt="invented"))
    with pytest.raises(CodingWorkspaceError, match="does not match"):
        await run_coding(
            request(McpToolName.RESEARCH),
            DraftHarness(
                {
                    "answer": "Fabricated",
                    "citations": [{"path": "main.py", "line": 1, "excerpt": "invented"}],
                }
            ),  # type: ignore[arg-type]
            workspace=workspace,
            run_id=uuid.uuid4(),
        )


async def test_apply_commits_branch_even_when_tests_fail(tmp_path: Path) -> None:
    workspace = checkout(tmp_path)
    (workspace.root / "test_main.py").write_text("def test_value():\n    assert False\n")
    result = await run_coding(
        request(McpToolName.APPLY),
        DraftHarness(
            {"summary": "Change value", "edits": [{"path": "main.py", "content": "VALUE = 2\n"}]}
        ),  # type: ignore[arg-type]
        workspace=workspace,
        run_id=uuid.uuid4(),
    )
    assert result.output["branch"].startswith("coire/")
    assert result.output["tests"]["status"] == CodingTestStatus.FAILED
    assert "VALUE = 2" in result.output["diff_excerpt"]
    assert (workspace.output / "branch.bundle").stat().st_size > 0
    activity = [
        RunActivity.model_validate_json(line)
        for line in (workspace.output / f"activity-{result.run_id}.jsonl").read_bytes().splitlines()
    ]
    assert [(item.tool_name, item.state) for item in activity] == [
        ("read_file", "started"),
        ("read_file", "completed"),
        ("model_generation", "started"),
        ("model_generation", "completed"),
        ("apply_patch", "started"),
        ("apply_patch", "completed"),
        ("run_tests", "started"),
        ("run_tests", "completed"),
        ("branch_bundle", "started"),
        ("branch_bundle", "completed"),
    ]
    assert result.output["base_revision"] != result.output["head_revision"]
    imported_head = _imported_head(workspace, str(result.output["branch"]), tmp_path / "imported")
    assert imported_head == result.output["head_revision"]


async def test_plan_returns_ordered_criteria_without_modifying_checkout(tmp_path: Path) -> None:
    workspace = checkout(tmp_path)
    before = await workspace.revision()
    result = await run_coding(
        request(McpToolName.PLAN),
        DraftHarness(
            {
                "goal": "Change value",
                "steps": [
                    {
                        "description": "Update main.py",
                        "acceptance_criteria": ["VALUE is 2"],
                    }
                ],
            }
        ),  # type: ignore[arg-type]
        workspace=workspace,
        run_id=uuid.uuid4(),
    )
    assert result.output["steps"][0]["acceptance_criteria"] == ["VALUE is 2"]
    assert await workspace.revision() == before
    assert (workspace.root / "main.py").read_text() == "VALUE = 1\n"


async def test_concurrent_applies_use_separate_clones_and_branches(tmp_path: Path) -> None:
    # Two disposable clones stand in for node-prepared workspaces of the same source.
    source = checkout(tmp_path)
    clones = _clones(source, tmp_path)
    results = await asyncio.gather(
        *(
            run_coding(
                request(McpToolName.APPLY),
                DraftHarness(
                    {
                        "summary": f"Change {index}",
                        "edits": [{"path": "main.py", "content": f"VALUE = {index + 2}\n"}],
                    }
                ),  # type: ignore[arg-type]
                workspace=workspace,
                run_id=uuid.uuid4(),
            )
            for index, workspace in enumerate(clones)
        )
    )
    assert results[0].output["branch"] != results[1].output["branch"]
    assert (source.root / "main.py").read_text() == "VALUE = 1\n"


async def test_missing_and_unsupported_tests_are_distinct(tmp_path: Path) -> None:
    workspace = checkout(tmp_path)
    assert (await workspace.run_tests()).status is CodingTestStatus.NOT_FOUND
    (workspace.root / "package.json").write_text("{}")
    assert (await workspace.run_tests()).status is CodingTestStatus.UNSUPPORTED


async def test_silent_artifact_writer_is_stopped_at_size_cap(tmp_path: Path) -> None:
    workspace = checkout(tmp_path)
    artifact = workspace.output / "oversized"
    script = (
        "from pathlib import Path; import time; "
        f"Path({str(artifact)!r}).write_bytes(b'x'*4096); time.sleep(5)"
    )
    with pytest.raises(CodingWorkspaceError, match="size cap"):
        await workspace.command(
            [sys.executable, "-c", script],
            timeout_seconds=2,
            watch_file=artifact,
            watch_limit=1024,
        )


def test_repository_paths_cannot_escape_or_touch_control_files(tmp_path: Path) -> None:
    workspace = checkout(tmp_path)
    (workspace.root / "link").symlink_to(tmp_path)
    for path in ("../outside", ".git/config", ".coire/request.json", "link/outside"):
        with pytest.raises(CodingWorkspaceError):
            workspace.safe_path(path, for_write=True)
