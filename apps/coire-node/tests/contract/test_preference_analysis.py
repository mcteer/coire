"""Preference analysis commands preserve authenticated control-fabric routing."""

import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from coire_core.models.datasets import DatasetAnalysisBinding, DatasetFormat
from coire_core.models.node import NetworkPath
from coire_core.models.training_node import (
    DatasetInputGrant,
    NodeDatasetAnalysisRequest,
    NodeDatasetAnalysisStatus,
)
from coire_node.agent import create_app
from coire_node.testing.harness import TOKEN, Agent
from coire_node.training.analysis_supervisor import AnalysisSupervisor


def command(data: bytes) -> NodeDatasetAnalysisRequest:
    dataset_id, analysis_id, model_id, variant_id = (uuid.uuid4() for _ in range(4))
    source_sha = hashlib.sha256(data).hexdigest()
    return NodeDatasetAnalysisRequest(
        command_id=uuid.uuid4(),
        request_sha256="a" * 64,
        analysis_id=analysis_id,
        model_id=model_id,
        variant_id=variant_id,
        base_manifest_sha256="b" * 64,
        input_grant=DatasetInputGrant(
            grant_id=uuid.uuid4(),
            node="coire-edge-a",
            dataset_id=dataset_id,
            source_sha256=source_sha,
            max_bytes=len(data),
            analysis_id=analysis_id,
            expires_at=datetime.now(UTC) + timedelta(minutes=1),
            secret="private-test-grant" * 3,
        ),
        reservation_id=uuid.uuid4(),
        memory_bytes=1024**3,
        deadline=datetime.now(UTC) + timedelta(minutes=1),
        binding=DatasetAnalysisBinding(
            dataset_id=dataset_id,
            model_id=model_id,
            variant_id=variant_id,
            base_manifest_sha256="b" * 64,
            source_sha256=source_sha,
            split_sha256="c" * 64,
            format=DatasetFormat.TEXT,
            model_slug="synthetic--base",
        ),
    )


def test_preference_analysis_route_binds_template_and_refuses_data_listener(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = Agent(tmp_path / "node", node_name="coire-edge-a")
    agent.settings.training_enabled = True
    supervisor = AnalysisSupervisor(agent.settings, agent.reservations)
    template = "synthetic-template"
    request = command(b"synthetic preference bytes")
    request.binding.format = DatasetFormat.PREFERENCE
    request.binding.template_override = template
    request.template_sha256 = hashlib.sha256(template.encode()).hexdigest()
    seen: list[NodeDatasetAnalysisRequest] = []

    async def start(value: NodeDatasetAnalysisRequest) -> NodeDatasetAnalysisStatus:
        seen.append(value)
        return NodeDatasetAnalysisStatus(
            analysis_id=value.analysis_id, state="queued", completed_rows=0
        )

    monkeypatch.setattr(supervisor, "start", start)
    try:
        app = create_app(
            agent.settings,
            agent.collector,
            listener=NetworkPath.CONTROL,
            training_analyses=supervisor,
        )
        with TestClient(app) as client:
            assert (
                client.post(
                    "/node/training/analyses", json=request.model_dump(mode="json")
                ).status_code
                == 401
            )
            headers = {"Authorization": f"Bearer {TOKEN}"}
            assert (
                client.post(
                    "/node/training/analyses", json=request.model_dump(mode="json"), headers=headers
                ).status_code
                == 202
            )
            assert seen[0].binding.format is DatasetFormat.PREFERENCE
            forged = request.model_dump(mode="json")
            forged["template_sha256"] = "a" * 64
            assert (
                client.post("/node/training/analyses", json=forged, headers=headers).status_code
                == 422
            )
        data = create_app(
            agent.settings, agent.collector, listener=NetworkPath.DATA, training_analyses=supervisor
        )
        with TestClient(data) as client:
            assert (
                client.post(
                    "/node/training/analyses", json=request.model_dump(mode="json"), headers=headers
                ).status_code
                == 404
            )
    finally:
        agent.close()
