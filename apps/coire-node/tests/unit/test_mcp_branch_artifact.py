"""Studio branch bundle identity, size, and symlink boundaries."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from coire_core.models.harness import ContextBudget, HarnessRunResult, ProfileName
from coire_core.models.mcp import ApplyResult
from coire_core.models.mcp import TestStatus as CodingTestStatus
from coire_core.models.mcp import TestSummary as CodingTestSummary
from coire_core.settings import Settings
from coire_node.workspaces import WorkspaceError, WorkspaceManager


def _manager(tmp_path: Path) -> WorkspaceManager:
    settings = Settings(_secrets_dir="/nonexistent", run_workspace_root=str(tmp_path))  # type: ignore[call-arg]
    return WorkspaceManager(settings)


async def test_artifact_status_is_bound_to_run_and_payload(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    run_id = uuid.uuid4()
    output = tmp_path / manager.output_ref(run_id)
    output.mkdir()
    artifact_id = uuid.uuid4()
    payload = ApplyResult(
        run_id=run_id,
        branch="coire/123abc",
        base_revision="a" * 40,
        head_revision="b" * 40,
        diff_excerpt="x",
        diff_truncated=False,
        tests=CodingTestSummary(status=CodingTestStatus.NOT_FOUND),
        artifact_id=artifact_id,
    )
    (output / "result.json").write_text(
        HarnessRunResult(
            run_id=run_id,
            profile=ProfileName.CODING,
            variant_id=uuid.uuid4(),
            output=payload.model_dump(mode="json"),
            context=ContextBudget(token_limit=4096),
        ).model_dump_json()
    )
    (output / "branch.bundle").write_bytes(b"bundle")
    status, path = await manager.artifact(run_id)
    assert status.artifact_id == artifact_id
    assert status.size_bytes == 6
    assert path.read_bytes() == b"bundle"
    (output / "branch.bundle").unlink()
    (output / "branch.bundle").symlink_to(tmp_path / "outside")
    with pytest.raises(WorkspaceError, match="unavailable"):
        await manager.artifact(run_id)
