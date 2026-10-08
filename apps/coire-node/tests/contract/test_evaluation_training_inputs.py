"""Studio input preparation uses exact grants and owned checkpoint metadata only."""

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from evaluation_input_fixtures import staged_workload
from fastapi import HTTPException, Request

from coire_core.models.evaluation import EvaluationWorkspacePrepare, canonical_digest
from coire_core.models.evaluation_inputs import (
    EvaluationDatasetGrant,
    EvaluationTrainingInputsRequest,
)
from coire_core.settings import Settings
from coire_node.evaluations import EvaluationWorkspaceManager
from coire_node.routes.evaluations import stage_training_inputs


@pytest.mark.parametrize("condition", ["valid", "wrong_node", "wrong_attempt", "bad_digest"])
async def test_studio_stages_exact_sources_and_rejects_foreign_or_changed_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    condition: str,
) -> None:
    seed = tmp_path / "seed"
    seed.mkdir()
    work = staged_workload(seed)
    assert work.training is not None
    binding = work.training
    source = binding.sources[0]
    raw = (seed / f"source-{source.dataset_id}.jsonl").read_bytes()
    workspace = EvaluationWorkspaceManager(
        Settings(run_workspace_root=str(tmp_path / "workspaces"), node_name="coire-edge-a")
    )
    await workspace.prepare(
        EvaluationWorkspacePrepare(workload=work, request_sha256=canonical_digest(work))
    )
    entry = SimpleNamespace(
        id=binding.state_file_id, sha256=binding.state_sha256, bytes=binding.state_bytes
    )
    manifest = SimpleNamespace(
        files=[entry], canonical_sha256=lambda: binding.checkpoint_manifest_sha256
    )
    artifacts = SimpleNamespace(
        manifest=lambda identity: manifest,
        file=lambda manifest, file_id: seed / "training-state.json",
    )
    app = SimpleNamespace(
        state=SimpleNamespace(
            runs=SimpleNamespace(evaluations=workspace, settings=workspace.settings),
            training_artifacts=artifacts,
        )
    )
    request = Request({"type": "http", "app": app, "headers": []})
    grant = EvaluationDatasetGrant(
        grant_id=uuid.uuid4(),
        evaluation_attempt_id=uuid.uuid4() if condition == "wrong_attempt" else work.attempt_id,
        dataset_id=source.dataset_id,
        node="coire-edge-b" if condition == "wrong_node" else "coire-edge-a",
        source_sha256=source.source_sha256,
        max_bytes=source.source_bytes,
        expires_at=datetime.now(UTC) + timedelta(seconds=60),
        secret="s" * 43,
    )
    command = EvaluationTrainingInputsRequest(
        run_id=work.run_id, request_sha256=canonical_digest(work), grants=[grant]
    )

    def respond(incoming: httpx.Request) -> httpx.Response:
        assert incoming.headers["X-Coire-Dataset-Grant"] == grant.secret
        assert incoming.headers["X-Coire-Node"] == "coire-edge-a"
        assert incoming.url.path.endswith(f"/{source.dataset_id}/content")
        return httpx.Response(200, content=raw if condition != "bad_digest" else b"changed")

    client_type = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: client_type(**kwargs, transport=httpx.MockTransport(respond)),
    )
    if condition == "valid":
        receipts = await stage_training_inputs(work.run_id, command, request)
        assert {item.name for item in receipts.items} == {
            "training-state.json",
            f"source-{source.dataset_id}.jsonl",
        }
        await workspace.stage_input(
            work.run_id,
            canonical_digest(work),
            f"split-{source.dataset_id}.json",
            (seed / f"split-{source.dataset_id}.json").read_bytes(),
        )
        workspace.validate_inputs(work.run_id)
        assert await stage_training_inputs(work.run_id, command, request) == receipts
    else:
        with pytest.raises(HTTPException) as failure:
            await stage_training_inputs(work.run_id, command, request)
        assert failure.value.status_code == 409
        assert grant.secret not in str(failure.value)
