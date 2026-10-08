"""Private source grants retain fresh authority, exact phase and Studio identity."""

import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from evaluation_input_fixtures import staged_workload

from coire_api.auth import Principal, PrincipalKind
from coire_api.db import (
    EvaluationAttemptRow,
    EvaluationRunRow,
    NodeRow,
    TrainingCommandRow,
    TrainingDatasetRevisionRow,
)
from coire_core.errors import EvaluationForbidden, TrainingNotFound
from coire_core.models.auth import UserRole
from coire_core.models.evaluation import canonical_digest
from coire_core.models.evaluation_inputs import EvaluationDatasetGrant

WORKLOAD_FIXTURE = Path(__file__).resolve().parents[4] / "tests/fixtures/evaluations/workload.json"


@pytest.mark.parametrize(
    "condition",
    [
        "valid",
        "foreign_node",
        "expired",
        "cancelled",
        "stale_fence",
        "revoked",
        "changed_source",
        "wrong_attempt_node",
    ],
)
async def test_private_source_grant_refuses_scope_or_live_authority_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    condition: str,
) -> None:
    from coire_api.evaluation import inputs as module

    work = staged_workload(tmp_path)
    assert work.training is not None
    source = work.training.sources[0]
    owner = uuid.uuid4()
    principal = Principal(kind=PrincipalKind.USER, user_id=owner, role=UserRole.ADMIN)
    secret = "s" * 43
    grant = EvaluationDatasetGrant(
        grant_id=uuid.uuid4(),
        evaluation_attempt_id=work.attempt_id,
        dataset_id=source.dataset_id,
        node="coire-edge-a",
        source_sha256=source.source_sha256,
        max_bytes=source.source_bytes,
        expires_at=datetime.now(UTC) + timedelta(seconds=-1 if condition == "expired" else 60),
        secret=secret,
    )
    node_id = uuid.uuid4()
    command = TrainingCommandRow(
        actor_user_id=owner,
        state="succeeded",
        request_sha256=hashlib.sha256(secret.encode()).hexdigest(),
        payload={
            "grant": grant.model_dump(mode="json", exclude={"secret"}),
            "run_id": work.evaluation_id,
            "fence": work.fence,
            "workload_sha256": canonical_digest(work),
        },
    )
    run = EvaluationRunRow(
        id=work.evaluation_id,
        owner_user_id=owner,
        fence=2 if condition == "stale_fence" else work.fence,
        state="cancelling" if condition == "cancelled" else "running",
        authorization_snapshot=principal.model_dump(mode="json"),
    )
    attempt = EvaluationAttemptRow(
        id=work.attempt_id,
        run_id=work.evaluation_id,
        agent_run_id=work.run_id,
        fence=work.fence,
        node_id=node_id,
        state="running",
        workload=work.model_dump(mode="json"),
    )
    revision = TrainingDatasetRevisionRow(
        id=source.dataset_id,
        state="ready",
        source_sha256=source.source_sha256,
        source_bytes=source.source_bytes + (1 if condition == "changed_source" else 0),
    )
    session = AsyncMock()
    session.scalar.return_value = command
    node = NodeRow(
        id=node_id, name="coire-edge-b" if condition == "wrong_attempt_node" else "coire-edge-a"
    )
    rows = {
        EvaluationRunRow: run,
        EvaluationAttemptRow: attempt,
        TrainingDatasetRevisionRow: revision,
        NodeRow: node,
    }
    session.get.side_effect = lambda cls, identity, **kwargs: rows[cls]
    authority = AsyncMock(side_effect=EvaluationForbidden() if condition == "revoked" else None)
    monkeypatch.setattr(module, "authorize_live_evaluation_action", authority)
    if condition == "valid":
        assert (
            await module.authorized_source(session, source.dataset_id, "coire-edge-a", secret)
            is revision
        )
        authority.assert_awaited_once()
    else:
        with pytest.raises(EvaluationForbidden if condition == "revoked" else TrainingNotFound):
            await module.authorized_source(
                session,
                source.dataset_id,
                "coire-edge-b" if condition == "foreign_node" else "coire-edge-a",
                secret,
            )


async def test_freezing_inputs_uses_complete_checkpoint_and_exact_ready_source(
    tmp_path: Path,
) -> None:
    import json

    from coire_api.db import TrainingCheckpointRow
    from coire_api.evaluation.inputs import freeze_training_inputs
    from coire_api.training.service import payload_digest
    from coire_core.models.training import parse_resolved_training_spec
    from coire_core.models.training_node import TrainingArtifactManifest

    work = staged_workload(tmp_path)
    assert work.training is not None
    binding = work.training
    source = binding.sources[0]
    legacy_path = WORKLOAD_FIXTURE.parent / "legacy_training/resolved.json"
    legacy = json.loads(legacy_path.read_bytes())
    legacy["spec"]["data"]["train"] = binding.mixture.model_dump(mode="json")
    legacy["spec"]["data"]["validation"]["dataset_ids"] = [str(source.dataset_id)]
    legacy["spec"]["optim"]["updates"] = 2
    legacy["spec"]["optim"]["batch_size"] = binding.batch_size
    legacy["spec"]["optim"]["accumulation_steps"] = binding.accumulation_steps
    legacy["datasets"] = [
        {
            "dataset_id": str(source.dataset_id),
            "analysis_id": str(uuid.uuid4()),
            "source_sha256": source.source_sha256,
            "split_sha256": source.split_sha256,
            "analysis_sha256": "a" * 64,
        }
    ]
    resolved = parse_resolved_training_spec(legacy)
    manifest_value = json.loads(
        (WORKLOAD_FIXTURE.parent / "legacy_training/checkpoint.json").read_bytes()
    )
    manifest_value.update(
        artifact_id=str(binding.checkpoint_id),
        job_id=binding.job_id,
        attempt_id=binding.training_attempt_id,
        fence=binding.training_fence,
        update=binding.completed_update,
        resolved_spec_sha256=payload_digest(resolved),
        runtime_sha256=binding.runtime_sha256,
    )
    for rank in manifest_value["ranks"]:
        rank["update"] = binding.completed_update
        if rank["rank"] == 0:
            rank["state_file_id"] = binding.state_file_id
    state_id = manifest_value["ranks"][0]["state_file_id"]
    for file in manifest_value["files"]:
        if "state" in file["id"]:
            file.update(id=state_id, sha256=binding.state_sha256, bytes=binding.state_bytes)
    manifest_value["total_bytes"] = sum(file["bytes"] for file in manifest_value["files"])
    manifest = TrainingArtifactManifest.model_validate(manifest_value)
    checkpoint = TrainingCheckpointRow(
        id=binding.checkpoint_id,
        job_id=binding.job_id,
        attempt_id=binding.training_attempt_id,
        fence=1,
        completed_update=1,
        state="committed",
        manifest=manifest.model_dump(mode="json"),
        manifest_sha256=manifest.canonical_sha256(),
    )
    revision = TrainingDatasetRevisionRow(
        id=source.dataset_id,
        state="ready",
        format=source.format.value,
        source_sha256=source.source_sha256,
        source_bytes=source.source_bytes,
        split_sha256=source.split_sha256,
        split_manifest=json.loads((tmp_path / f"split-{source.dataset_id}.json").read_bytes()),
    )
    run = EvaluationRunRow(
        data_snapshot={
            "job_id": binding.job_id,
            "checkpoint_id": str(binding.checkpoint_id),
            "completed_update": 1,
            "resolved_spec": resolved.model_dump(mode="json"),
            "resolved_sha256": payload_digest(resolved),
        }
    )
    session = AsyncMock()
    rows = {TrainingCheckpointRow: checkpoint, TrainingDatasetRevisionRow: revision}
    session.get.side_effect = lambda cls, identity, **kwargs: rows[cls]
    actual, files = await freeze_training_inputs(session, run)
    assert actual is not None and actual.sources == binding.sources
    assert actual.state_sha256 == binding.state_sha256 and actual.completed_update == 1
    assert {file.purpose for file in files} == {
        "training_state",
        "split_manifest",
        "training_source",
    }
    revision.state = "retired"
    assert await freeze_training_inputs(session, run) == (None, [])
