"""Real-persistence dataset API boundaries, including quota before the first body byte."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

import anyio
import httpx
import pytest
from fastapi import Request
from pydantic import SecretStr
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from training_postgres import disposable_postgres

from coire_api import db
from coire_api.app import create_app
from coire_api.auth import ANONYMOUS, Principal, PrincipalKind
from coire_api.db import (
    ApiKeyRow,
    AuditRow,
    Base,
    MemoryReservationRow,
    ModelRow,
    ModelVariantRow,
    NodeMemoryLedgerRow,
    NodeRow,
    TrainingCommandRow,
    TrainingDatasetAnalysisRow,
    TrainingDatasetGrantRow,
    TrainingDatasetReferenceRow,
    TrainingDatasetRevisionRow,
    TrainingJobRow,
    TrainingStorageReservationRow,
    UserRow,
    VariantCopyRow,
)
from coire_core.models.acquisition import VariantState
from coire_core.models.auth import UserRole
from coire_core.models.datasets import (
    DatasetAnalysis,
    DatasetAnalysisDispatch,
    DatasetRegistrationCommand,
)
from coire_core.models.training_node import (
    DatasetAnalysisWorkerInput,
    NodeAnalysisCancelRequest,
    NodeDatasetAnalysisRequest,
    NodeDatasetAnalysisStatus,
)
from coire_core.settings import Settings

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("COIRE_INTEGRATION") != "1",
        reason="enable disposable-Postgres dataset contracts",
    ),
]


@pytest.fixture(scope="session")
def training_postgres_url() -> Iterator[str]:
    with disposable_postgres() as url:
        yield url


@dataclass
class API:
    client: httpx.AsyncClient
    sessions: async_sessionmaker[AsyncSession]
    metadata: dict[str, object]
    settings: Settings
    owner: uuid.UUID

    @property
    def headers(self) -> dict[str, str]:
        return {
            "authorization": "Bearer admin",
            "origin": "https://coire.test",
            "idempotency-key": "upload-1",
        }

    async def upload(self, data: bytes, *, headers: dict[str, str] | None = None) -> httpx.Response:
        return await self.client.post(
            "/api/v1/admin/datasets",
            headers=headers or self.headers,
            files={
                "metadata": (None, json.dumps(self.metadata)),
                "file": ("synthetic.jsonl", data, "application/x-ndjson"),
            },
        )


@pytest.fixture
async def api(
    training_postgres_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[API]:
    engine = create_async_engine(training_postgres_url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db, "_sessionmaker", sessions)
    owner, model_id, variant_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with sessions() as session:
        session.add(
            UserRow(
                id=owner,
                email=f"{owner}@dataset.test",
                display_name="Admin",
                role=UserRole.ADMIN,
                active=True,
            )
        )
        session.add(
            ModelRow(
                id=model_id,
                repo_id=f"synthetic/{model_id}",
                slug=f"synthetic--{model_id}",
                display_name="Synthetic base",
                state="ready",
                visibility="admin_only",
                precision="4bit",
                weight_bytes=1024,
                total_bytes=1024,
                file_count=2,
                memory_estimate_bytes=1024,
            )
        )
        await session.flush()
        session.add(
            ModelVariantRow(
                id=variant_id,
                model_id=model_id,
                name="test",
                slug=f"synthetic--{variant_id}",
                source_revision="local-test",
                precision="4bit",
                state="ready",
                backend="mlx_lm",
            )
        )
        await session.flush()
        for index, node in enumerate(("coire-edge-a", "coire-edge-b")):
            node_id = uuid.uuid4()
            session.add(
                NodeRow(
                    id=node_id,
                    name=node,
                    role="studio",
                    memory_total_bytes=4096,
                    disk_total_bytes=8192,
                    agent_version="test",
                )
            )
            await session.flush()
            session.add(
                VariantCopyRow(
                    variant_id=variant_id,
                    node_id=node_id,
                    path="/unused/test-only",
                    bytes=1024,
                    verified=True,
                    manifest_sha256="a" * 64,
                    role="origin" if index == 0 else "replica",
                )
            )
        await session.commit()
    actors = {
        "Bearer admin": Principal(kind=PrincipalKind.USER, user_id=owner, role=UserRole.ADMIN),
        "Bearer user": Principal(kind=PrincipalKind.USER, user_id=owner, role=UserRole.USER),
        "Bearer node": Principal(kind=PrincipalKind.SERVICE, subject="node"),
        "Bearer ops": Principal(
            kind=PrincipalKind.OPS_SERVICE, subject="ops", scopes=frozenset({"admin"})
        ),
    }

    async def authenticate(request: Request) -> Principal:
        return actors.get(request.headers.get("authorization", ""), ANONYMOUS)

    monkeypatch.setattr("coire_api.auth.authenticate_request", authenticate)
    settings = Settings(
        training_enabled=True,
        training_dataset_dir=str(tmp_path / "datasets"),
        chat_browser_origin="https://coire.test",
    )
    app = create_app(settings)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield API(
            client,
            sessions,
            {
                "name": "Synthetic",
                "format": "text",
                "provenance": {"source": "test", "license_note": "synthetic"},
                "analysis_model_id": str(model_id),
                "analysis_variant_id": str(variant_id),
            },
            settings,
            owner,
        )
    await engine.dispose()


@pytest.mark.parametrize(
    "format,data",
    [
        ("text", b'{"text":"one"}\n{"text":"two"}\n'),
        (
            "prompt_completion",
            b'{"prompt":"one","completion":"1"}\n{"prompt":"two","completion":"2"}\n',
        ),
        (
            "conversation",
            b'{"messages":[{"role":"user","content":"one"},{"role":"assistant","content":"1"}]}\n{"messages":[{"role":"user","content":"two"},{"role":"assistant","content":"2"}]}\n',
        ),
    ],
)
async def test_upload_queues_analysis_and_keeps_source_private(
    api: API, format: str, data: bytes
) -> None:
    api.metadata["format"] = format
    response = await api.upload(data)
    assert response.status_code == 202, response.text
    receipt = response.json()
    assert receipt["state"] == "analyzing"
    detail = await api.client.get(
        f"/api/v1/admin/datasets/{receipt['dataset_id']}", headers=api.headers
    )
    assert detail.status_code == 200 and detail.json()["row_count"] == 2
    assert detail.json()["analysis_id"] is not None
    assert "storage_key" not in detail.json()
    async with api.sessions() as session:
        assert (
            await session.scalar(select(func.count()).select_from(TrainingDatasetAnalysisRow)) == 1
        )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(AuditRow)
                .where(AuditRow.action == "dataset.upload")
            )
            == 1
        )
        holds = list(await session.scalars(select(TrainingStorageReservationRow)))
        assert len(holds) == 1 and holds[0].state == "retained"
    original = await api.client.get(
        f"/api/v1/admin/datasets/{receipt['dataset_id']}/content", headers=api.headers
    )
    assert original.status_code == 404


async def test_quota_is_committed_before_first_multipart_body_byte(api: API) -> None:
    request = api.client.build_request(
        "POST",
        "/api/v1/admin/datasets",
        headers=api.headers,
        files={
            "metadata": (None, json.dumps(api.metadata)),
            "file": ("data.jsonl", b'{"text":"a"}\n{"text":"b"}\n'),
        },
    )
    body = request.read()

    async def streamed() -> AsyncIterator[bytes]:
        async with api.sessions() as session:
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(TrainingStorageReservationRow)
                    .where(TrainingStorageReservationRow.state == "held")
                )
                == 1
            )
        yield body

    response = await api.client.post(
        "/api/v1/admin/datasets", headers=dict(request.headers), content=streamed()
    )
    assert response.status_code == 202, response.text


@pytest.mark.parametrize("token", ["user", "node", "ops"])
async def test_privileged_denial_happens_before_any_storage_hold(api: API, token: str) -> None:
    response = await api.upload(
        b"private-content", headers={**api.headers, "authorization": f"Bearer {token}"}
    )
    assert response.status_code == 403
    async with api.sessions() as session:
        assert (
            await session.scalar(select(func.count()).select_from(TrainingStorageReservationRow))
            == 0
        )
        assert (
            await session.scalar(select(func.count()).select_from(TrainingDatasetRevisionRow)) == 0
        )


async def test_wrong_browser_origin_and_revoked_live_admin_are_refused(api: API) -> None:
    response = await api.upload(
        b"private-content", headers={**api.headers, "origin": "https://other.test"}
    )
    assert response.status_code == 403
    async with api.sessions() as session:
        await session.execute(update(UserRow).where(UserRow.id == api.owner).values(role="user"))
        await session.commit()
    assert (await api.upload(b"private-content")).status_code == 403


async def test_invalid_rows_fail_atomically_with_content_free_diagnostics(api: API) -> None:
    response = await api.upload(b'{"text":"valid"}\n{"secret-field":"secret-value"}\n')
    assert response.status_code == 202, response.text
    assert response.json()["state"] == "failed"
    detail = await api.client.get(
        f"/api/v1/admin/datasets/{response.json()['dataset_id']}", headers=api.headers
    )
    assert detail.json()["invalid_count"] == 1
    assert "secret-field" not in detail.text and "secret-value" not in detail.text
    assert not list((Path(api.settings.training_dataset_dir) / "sources").iterdir())


async def test_idempotency_replays_receipt_and_changed_bytes_conflict(api: API) -> None:
    data = b'{"text":"one"}\n{"text":"two"}\n'
    first, second = await api.upload(data), await api.upload(data)
    assert first.status_code == second.status_code == 202
    assert first.json() == second.json()
    assert (await api.upload(data.replace(b"one", b"new"))).status_code == 409
    async with api.sessions() as session:
        assert (
            await session.scalar(select(func.count()).select_from(TrainingDatasetRevisionRow)) == 1
        )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(AuditRow)
                .where(AuditRow.action == "dataset.upload")
            )
            == 1
        )


async def test_disabled_feature_and_compressed_body_refuse_upload(api: API) -> None:
    api.settings.training_enabled = False
    assert (await api.upload(b"private-content")).status_code == 503
    api.settings.training_enabled = True
    response = await api.upload(
        b"private-content", headers={**api.headers, "content-encoding": "gzip"}
    )
    assert response.status_code == 422


async def test_malformed_metadata_never_echoes_input_and_releases_spool_hold(api: API) -> None:
    api.metadata["secret-input-key"] = "never-echo-secret"
    response = await api.upload(b'{"text":"one"}\n{"text":"two"}\n')
    assert response.status_code == 422
    assert "never-echo-secret" not in response.text and "secret-input-key" not in response.text
    assert response.headers["content-type"].startswith("application/problem+json")
    async with api.sessions() as session:
        assert (
            await session.scalar(select(func.count()).select_from(TrainingDatasetRevisionRow)) == 0
        )
        holds = list(await session.scalars(select(TrainingStorageReservationRow)))
        assert len(holds) == 1 and holds[0].state == "released"


async def test_mandatory_audit_failure_rolls_back_dataset_and_publication(
    api: API, monkeypatch: pytest.MonkeyPatch
) -> None:
    from coire_core.errors import TrainingUnavailable

    async def fail(*_args: object, **_kwargs: object) -> None:
        raise TrainingUnavailable("Mutation audit unavailable")

    monkeypatch.setattr("coire_api.training.service.write_principal_audit", fail)
    response = await api.upload(b'{"text":"one"}\n{"text":"two"}\n')
    assert response.status_code == 503
    async with api.sessions() as session:
        assert (
            await session.scalar(select(func.count()).select_from(TrainingDatasetRevisionRow)) == 0
        )
        assert (
            await session.scalar(select(func.count()).select_from(TrainingDatasetAnalysisRow)) == 0
        )
    assert not list((Path(api.settings.training_dataset_dir) / "sources").iterdir())


async def test_storage_quota_denial_never_reads_the_body(api: API) -> None:
    api.settings.training_dataset_quota_bytes = 1
    consumed = False

    async def forbidden_body() -> AsyncIterator[bytes]:
        nonlocal consumed
        consumed = True
        yield b"body-must-not-be-consumed"

    response = await api.client.post(
        "/api/v1/admin/datasets",
        headers={
            **api.headers,
            "content-length": "100",
            "content-type": "multipart/form-data; boundary=test",
        },
        content=forbidden_body(),
    )
    assert response.status_code == 429
    assert consumed is False
    assert not await anyio.Path(api.settings.training_dataset_dir).exists()


async def test_private_source_requires_node_and_exact_live_grant(api: API) -> None:
    from coire_api.training.input_grants import mint_analysis_grant

    data = b'{"text":"one"}\n{"text":"two"}\n'
    response = await api.upload(data)
    identity = response.json()["dataset_id"]
    detail = await api.client.get(f"/api/v1/admin/datasets/{identity}", headers=api.headers)
    api.settings.node_tokens = SecretStr(
        json.dumps({"coire-edge-a": "test-node-a", "coire-edge-b": "test-node-b"})
    )
    async with api.sessions() as session:
        node = await session.scalar(select(NodeRow).where(NodeRow.name == "coire-edge-a"))
        assert node is not None
        grant = await mint_analysis_grant(
            session, uuid.UUID(detail.json()["analysis_id"]), node.id, api.settings
        )
        await session.commit()
    url = f"/api/v1/internal/training/datasets/{identity}/content"
    headers = {
        "authorization": "Bearer test-node-a",
        "x-coire-node": "coire-edge-a",
        "x-coire-dataset-grant": grant.secret,
    }
    result = await api.client.get(url, headers=headers)
    assert result.status_code == 200 and result.content == data
    assert result.headers["cache-control"] == "no-store"
    assert (
        await api.client.get(
            url,
            headers={**headers, "x-coire-dataset-grant": "invalid-grant-value-that-is-at-least-32"},
        )
    ).status_code == 404
    assert (
        await api.client.get(
            url,
            headers={
                **headers,
                "authorization": "Bearer test-node-b",
                "x-coire-node": "coire-edge-b",
            },
        )
    ).status_code == 404
    assert (await api.client.get(url, headers=api.headers)).status_code == 401
    assert (
        await api.client.get(
            f"/api/v1/internal/training/datasets/{uuid.uuid4()}/content", headers=headers
        )
    ).status_code == 404
    async with api.sessions() as session:
        await session.execute(update(UserRow).where(UserRow.id == api.owner).values(active=False))
        await session.commit()
    assert (await api.client.get(url, headers=headers)).status_code == 403


async def test_private_source_grant_expiry_and_revocation_fail_closed(api: API) -> None:
    from coire_api.training.input_grants import mint_analysis_grant

    response = await api.upload(b'{"text":"one"}\n{"text":"two"}\n')
    identity = response.json()["dataset_id"]
    detail = await api.client.get(f"/api/v1/admin/datasets/{identity}", headers=api.headers)
    api.settings.node_tokens = SecretStr(json.dumps({"coire-edge-a": "test-node-a"}))
    async with api.sessions() as session:
        node = await session.scalar(select(NodeRow).where(NodeRow.name == "coire-edge-a"))
        assert node is not None
        grant = await mint_analysis_grant(
            session, uuid.UUID(detail.json()["analysis_id"]), node.id, api.settings
        )
        await session.commit()
    headers = {
        "authorization": "Bearer test-node-a",
        "x-coire-node": "coire-edge-a",
        "x-coire-dataset-grant": grant.secret,
    }
    url = f"/api/v1/internal/training/datasets/{identity}/content"
    async with api.sessions() as session:
        await session.execute(
            update(TrainingDatasetGrantRow)
            .where(TrainingDatasetGrantRow.id == grant.grant_id)
            .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
        await session.commit()
    assert (await api.client.get(url, headers=headers)).status_code == 404
    async with api.sessions() as session:
        await session.execute(
            update(TrainingDatasetGrantRow)
            .where(TrainingDatasetGrantRow.id == grant.grant_id)
            .values(
                expires_at=datetime.now(UTC) + timedelta(seconds=60), revoked_at=datetime.now(UTC)
            )
        )
        await session.commit()
    assert (await api.client.get(url, headers=headers)).status_code == 404


async def test_dataset_pagination_has_stable_timestamp_id_cursor(api: API) -> None:
    identities = []
    for index in range(3):
        response = await api.upload(
            b'{"text":"one"}\n{"text":"two"}\n',
            headers={**api.headers, "idempotency-key": f"upload-{index}"},
        )
        assert response.status_code == 202
        identities.append(response.json()["dataset_id"])
    seen: list[str] = []
    cursor = None
    for _page in range(3):
        result = await api.client.get(
            "/api/v1/admin/datasets",
            headers=api.headers,
            params={"limit": "1", **({"cursor": cursor} if cursor else {})},
        )
        assert result.status_code == 200
        seen.extend(item["id"] for item in result.json()["items"])
        cursor = result.json()["next_cursor"]
    assert seen == identities and cursor is None
    assert (
        await api.client.get("/api/v1/admin/datasets?cursor=invalid", headers=api.headers)
    ).status_code == 422


async def test_durable_cpu_dispatch_ingests_matching_result_and_releases_hold(
    api: API,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from coire_api.nodes_client import NodeError, NodeErrorKind
    from coire_core.models.datasets import DatasetAnalysis, TokenDistribution
    from coire_core.models.training_node import (
        NodeDatasetAnalysisStatus,
    )
    from coire_scheduler.datasets import DatasetAnalysisExecutor

    response = await api.upload(b'{"text":"one"}\n{"text":"two"}\n')
    identity = uuid.UUID(response.json()["dataset_id"])
    async with api.sessions() as session:
        analysis = await session.scalar(
            select(TrainingDatasetAnalysisRow).where(
                TrainingDatasetAnalysisRow.dataset_id == identity
            )
        )
        assert analysis is not None
        analysis_id = analysis.id
        for node in await session.scalars(select(NodeRow)):
            session.add(
                NodeMemoryLedgerRow(
                    node_id=node.id,
                    budget_bytes=4 * 1024**3,
                    sandbox_bytes=0,
                    measured_resident_bytes=0,
                    health="healthy",
                    health_sampled_at=datetime.now(UTC),
                )
            )
        await session.commit()
    requests: list[NodeDatasetAnalysisRequest] = []

    class Client:
        def __init__(self, _settings: Settings) -> None:
            pass

        async def __aenter__(self) -> Client:
            return self

        async def __aexit__(self, *_args: object) -> None:
            pass

        async def dataset_analysis_status(
            self, _node: str, _identity: uuid.UUID
        ) -> NodeDatasetAnalysisStatus:
            raise NodeError(NodeErrorKind.NOT_FOUND, "fake-node", status=404)

        async def start_dataset_analysis(
            self, _node: str, command: NodeDatasetAnalysisRequest
        ) -> NodeDatasetAnalysisStatus:
            requests.append(command)
            return NodeDatasetAnalysisStatus(
                analysis_id=command.analysis_id,
                state="succeeded",
                completed_rows=2,
                result=DatasetAnalysis(
                    id=command.analysis_id,
                    dataset_id=identity,
                    model_id=command.model_id,
                    variant_id=command.variant_id,
                    state="succeeded",
                    tokenizer_sha256="d" * 64,
                    template_sha256="e" * 64,
                    runtime_sha256="f" * 64,
                    tokens=TokenDistribution(
                        minimum=4, maximum=4, p50=4, p95=4, histogram=[2], upper_bounds=[4]
                    ),
                    row_count=2,
                    role_counts={"assistant": 2},
                    created_at=datetime.now(UTC),
                ),
            )

    monkeypatch.setattr("coire_scheduler.datasets.NodeClient", Client)
    await DatasetAnalysisExecutor(api.settings).advance(analysis_id)
    assert len(requests) == 1 and requests[0].binding.dataset_id == identity
    detail = await api.client.get(f"/api/v1/admin/datasets/{identity}", headers=api.headers)
    assert detail.json()["state"] == "ready"
    async with api.sessions() as session:
        hold = await session.scalar(
            select(MemoryReservationRow).where(
                MemoryReservationRow.holder_id == f"dataset-analysis:{analysis_id}"
            )
        )
        assert hold is not None and hold.state.value == "released"
    projection = await api.client.get(
        f"/api/v1/admin/dataset-analyses/{analysis_id}", headers=api.headers
    )
    assert projection.status_code == 200 and projection.json()["tokens"]["p50"] == 4


class AnalysisNode:
    """Typed node transport double; all scheduler effects use actual Postgres transactions."""

    def __init__(self, api: API, analysis_id: uuid.UUID, dataset_id: uuid.UUID) -> None:
        self.api, self.analysis_id, self.dataset_id = api, analysis_id, dataset_id
        self.starts: list[NodeDatasetAnalysisRequest] = []
        self.cancels: list[NodeAnalysisCancelRequest] = []
        self.observed: NodeDatasetAnalysisStatus | None = None
        self.start_state: Literal["running", "succeeded", "failed"] = "succeeded"
        self.cancel_state: Literal["running", "cancelled"] = "cancelled"
        self.fetch_source = False
        self.revoke_on_start = False
        self.lose_start_reply = False
        self.unreachable = False
        self.mismatch: str | None = None

    async def __aenter__(self) -> AnalysisNode:
        return self

    async def __aexit__(self, *_args: object) -> None:
        pass

    async def dataset_analysis_status(
        self, _node: str, _identity: uuid.UUID
    ) -> NodeDatasetAnalysisStatus:
        from coire_api.nodes_client import NodeError, NodeErrorKind

        if self.unreachable:
            raise NodeError(NodeErrorKind.UNREACHABLE, "isolated-node")
        if self.observed is None:
            raise NodeError(NodeErrorKind.NOT_FOUND, "isolated-node", status=404)
        return self.observed

    async def start_dataset_analysis(
        self, node: str, command: NodeDatasetAnalysisRequest
    ) -> NodeDatasetAnalysisStatus:
        from coire_api.nodes_client import NodeError, NodeErrorKind
        from coire_core.models.datasets import DatasetAnalysis, TokenDistribution
        from coire_core.models.training_node import (
            NodeDatasetAnalysisRequest,
            NodeDatasetAnalysisStatus,
        )

        assert isinstance(command, NodeDatasetAnalysisRequest)
        self.starts.append(command)
        async with self.api.sessions() as session:
            row = await session.get(TrainingDatasetAnalysisRow, self.analysis_id)
            assert row is not None and row.state == "running" and row.result is not None
            assert "worker" in row.result and "secret" not in json.dumps(row.result)
            worker = DatasetAnalysisWorkerInput.model_validate(row.result["worker"])
            expected = DatasetAnalysisWorkerInput(
                command_id=command.command_id,
                analysis_id=command.analysis_id,
                binding=command.binding,
                source_bytes=command.input_grant.max_bytes,
                memory_bytes=command.memory_bytes,
                max_sequence_length=command.max_sequence_length,
                deadline=command.deadline,
            )
            assert worker == expected
            assert (
                command.request_sha256
                == hashlib.sha256(expected.model_dump_json().encode()).hexdigest()
            )
            hold = await session.get(MemoryReservationRow, command.reservation_id)
            assert hold is not None and hold.state.value == "held"
        if self.fetch_source:
            response = await asyncio.wait_for(
                self.api.client.get(
                    f"/api/v1/internal/training/datasets/{self.dataset_id}/content",
                    headers={
                        "authorization": "Bearer synthetic-node",
                        "x-coire-node": node,
                        "x-coire-dataset-grant": command.input_grant.secret,
                    },
                ),
                timeout=5,
            )
            assert response.status_code == 200, response.text
        if self.revoke_on_start:
            async with self.api.sessions() as session:
                await session.execute(
                    update(UserRow).where(UserRow.id == self.api.owner).values(active=False)
                )
                await session.commit()
        result = DatasetAnalysis(
            id=self.analysis_id,
            dataset_id=self.dataset_id,
            model_id=command.model_id,
            variant_id=command.variant_id,
            state="succeeded",
            row_count=2,
            tokenizer_sha256="d" * 64,
            template_sha256="e" * 64,
            runtime_sha256="f" * 64,
            tokens=TokenDistribution(
                minimum=4, maximum=4, p50=4, p95=4, histogram=[2], upper_bounds=[4]
            ),
            created_at=datetime.now(UTC),
        )
        if self.mismatch:
            changes: dict[str, object] = {
                self.mismatch: uuid.uuid4() if self.mismatch.endswith("id") else 3
            }
            result = result.model_copy(update=changes)
        self.observed = NodeDatasetAnalysisStatus(
            analysis_id=self.analysis_id,
            state=self.start_state,
            completed_rows=2,
            result=result if self.start_state == "succeeded" else None,
        )
        if self.lose_start_reply:
            raise NodeError(NodeErrorKind.UNREACHABLE, "isolated-node")
        return self.observed

    async def cancel_dataset_analysis(
        self, _node: str, command: NodeAnalysisCancelRequest
    ) -> NodeDatasetAnalysisStatus:
        from coire_core.models.training_node import NodeDatasetAnalysisStatus

        self.cancels.append(command)
        self.observed = NodeDatasetAnalysisStatus(
            analysis_id=self.analysis_id, state=self.cancel_state, completed_rows=0
        )
        return self.observed


@pytest.fixture
async def analysis_node(api: API, monkeypatch: pytest.MonkeyPatch) -> AnalysisNode:
    response = await api.upload(b'{"text":"one"}\n{"text":"two"}\n')
    identity = uuid.UUID(response.json()["dataset_id"])
    async with api.sessions() as session:
        analysis = await session.scalar(
            select(TrainingDatasetAnalysisRow).where(
                TrainingDatasetAnalysisRow.dataset_id == identity
            )
        )
        assert analysis is not None
        for node in await session.scalars(select(NodeRow)):
            session.add(
                NodeMemoryLedgerRow(
                    node_id=node.id,
                    budget_bytes=4 * 1024**3,
                    sandbox_bytes=0,
                    measured_resident_bytes=0,
                    health="healthy",
                    health_sampled_at=datetime.now(UTC),
                )
            )
        await session.commit()
    transport = AnalysisNode(api, analysis.id, identity)
    monkeypatch.setattr("coire_scheduler.datasets.NodeClient", lambda _settings: transport)
    return transport


async def analysis_hold(api: API, identity: uuid.UUID) -> tuple[str, str]:
    async with api.sessions() as session:
        row = await session.get(TrainingDatasetAnalysisRow, identity)
        hold = await session.scalar(
            select(MemoryReservationRow).where(
                MemoryReservationRow.holder_id == f"dataset-analysis:{identity}"
            )
        )
        assert row is not None and hold is not None
        return row.state, hold.state.value


@pytest.mark.parametrize("state", ["queued", "running"])
async def test_pending_analysis_status_preserves_scope_without_fabricated_identities(
    api: API, analysis_node: AnalysisNode, state: str
) -> None:
    from coire_scheduler.datasets import DatasetAnalysisExecutor

    if state == "running":
        analysis_node.start_state = "running"
        api.settings.training_max_sequence_length = 1024
        await DatasetAnalysisExecutor(api.settings).advance(analysis_node.analysis_id)
        assert len(analysis_node.starts) == 1
        assert analysis_node.starts[0].max_sequence_length == 1024
    response = await api.client.get(
        f"/api/v1/admin/dataset-analyses/{analysis_node.analysis_id}", headers=api.headers
    )
    assert response.status_code == 200, response.text
    projection = DatasetAnalysis.model_validate(response.json())
    assert projection.id == analysis_node.analysis_id
    assert projection.dataset_id == analysis_node.dataset_id
    assert projection.model_id == uuid.UUID(str(api.metadata["analysis_model_id"]))
    assert projection.variant_id == uuid.UUID(str(api.metadata["analysis_variant_id"]))
    assert projection.state == state
    assert projection.tokenizer_sha256 is None
    assert projection.template_sha256 is None
    assert projection.runtime_sha256 is None
    assert projection.tokens is None and projection.role_counts == {}
    assert projection.row_count == projection.invalid_count == projection.duplicate_rows == 0
    assert projection.diagnostics == []
    async with api.sessions() as session:
        row = await session.get(TrainingDatasetAnalysisRow, analysis_node.analysis_id)
        assert row is not None and row.state == state
        assert projection.created_at == row.created_at
        assert row.tokenizer_sha256 is row.template_sha256 is row.runtime_sha256 is None
        if state == "queued":
            assert row.result is None
        else:
            assert row.result is not None and "dispatch" in row.result and "worker" in row.result


async def test_concurrent_executors_own_dispatch_and_can_fetch_source_without_deadlock(
    api: API, analysis_node: AnalysisNode
) -> None:
    from coire_scheduler.datasets import DatasetAnalysisExecutor

    analysis_node.fetch_source = True
    api.settings.node_tokens = SecretStr(
        json.dumps({"coire-edge-a": "synthetic-node", "coire-edge-b": "synthetic-node"})
    )
    await asyncio.wait_for(
        asyncio.gather(
            *(
                DatasetAnalysisExecutor(api.settings).advance(analysis_node.analysis_id)
                for _ in range(4)
            )
        ),
        timeout=10,
    )
    assert len(analysis_node.starts) == 1
    assert await analysis_hold(api, analysis_node.analysis_id) == ("succeeded", "released")
    async with api.sessions() as session:
        holds = list(await session.scalars(select(MemoryReservationRow)))
        assert len(holds) == 1
        grants = list(await session.scalars(select(TrainingDatasetGrantRow)))
        assert grants and all(grant.revoked_at is not None for grant in grants)


async def test_lost_start_ack_readopts_persisted_command_without_restarting(
    api: API, analysis_node: AnalysisNode
) -> None:
    from coire_api.nodes_client import NodeError
    from coire_scheduler.datasets import DatasetAnalysisExecutor

    analysis_node.lose_start_reply = True
    executor = DatasetAnalysisExecutor(api.settings)
    with pytest.raises(NodeError):
        await executor.advance(analysis_node.analysis_id)
    assert await analysis_hold(api, analysis_node.analysis_id) == ("running", "held")
    api.settings.training_analysis_memory_bytes = 128 * 1024**2
    await DatasetAnalysisExecutor(api.settings).advance(analysis_node.analysis_id)
    assert len(analysis_node.starts) == 1
    assert await analysis_hold(api, analysis_node.analysis_id) == ("succeeded", "released")


@pytest.mark.parametrize("reason", ["revoked", "disabled"])
async def test_authority_revoked_inflight_cancels_and_keeps_hold_until_cleanup_receipt(
    api: API, analysis_node: AnalysisNode, reason: str
) -> None:
    from coire_scheduler.datasets import DatasetAnalysisExecutor

    analysis_node.start_state = "running"
    analysis_node.cancel_state = "running"
    analysis_node.revoke_on_start = reason == "revoked"
    executor = DatasetAnalysisExecutor(api.settings)
    await executor.advance(analysis_node.analysis_id)
    if reason == "disabled":
        api.settings.training_enabled = False
    await executor.advance(analysis_node.analysis_id)
    assert len(analysis_node.cancels) == 1
    assert await analysis_hold(api, analysis_node.analysis_id) == ("running", "held")
    analysis_node.cancel_state = "cancelled"
    await executor.advance(analysis_node.analysis_id)
    assert await analysis_hold(api, analysis_node.analysis_id) == ("failed", "released")
    assert len(analysis_node.starts) == 1


async def test_revocation_before_dispatch_never_acquires_memory_or_contacts_node(
    api: API, analysis_node: AnalysisNode
) -> None:
    from coire_scheduler.datasets import DatasetAnalysisExecutor

    async with api.sessions() as session:
        await session.execute(update(UserRow).where(UserRow.id == api.owner).values(role="user"))
        await session.commit()
    await DatasetAnalysisExecutor(api.settings).advance(analysis_node.analysis_id)
    assert not analysis_node.starts
    async with api.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(MemoryReservationRow)) == 0
        row = await session.get(TrainingDatasetAnalysisRow, analysis_node.analysis_id)
        assert row is not None and row.state == "failed"


async def test_expired_analysis_partition_retains_memory_and_never_restarts(
    api: API, analysis_node: AnalysisNode
) -> None:
    from coire_scheduler.datasets import DatasetAnalysisExecutor

    analysis_node.start_state = "running"
    executor = DatasetAnalysisExecutor(api.settings)
    await executor.advance(analysis_node.analysis_id)
    async with api.sessions() as session:
        row = await session.get(TrainingDatasetAnalysisRow, analysis_node.analysis_id)
        assert row is not None and row.result is not None
        value = dict(row.result)
        deadline = datetime.now(UTC) - timedelta(seconds=1)
        dispatch = DatasetAnalysisDispatch.model_validate(value["dispatch"])
        worker = DatasetAnalysisWorkerInput.model_validate(value["worker"])
        value["dispatch"] = dispatch.model_copy(update={"deadline": deadline}).model_dump(
            mode="json"
        )
        value["worker"] = worker.model_copy(update={"deadline": deadline}).model_dump(mode="json")
        row.result = value
        await session.commit()
    analysis_node.unreachable = True
    await executor.advance(analysis_node.analysis_id)
    assert await analysis_hold(api, analysis_node.analysis_id) == ("running", "held")
    analysis_node.unreachable = False
    await executor.advance(analysis_node.analysis_id)
    assert len(analysis_node.cancels) == 1
    assert len(analysis_node.starts) == 1
    assert await analysis_hold(api, analysis_node.analysis_id) == ("failed", "released")


@pytest.mark.parametrize("mismatch", ["id", "dataset_id", "model_id", "variant_id", "row_count"])
async def test_wrong_result_scope_or_counts_cannot_publish_or_release(
    api: API, analysis_node: AnalysisNode, mismatch: str
) -> None:
    from coire_scheduler.datasets import DatasetAnalysisExecutor

    analysis_node.mismatch = mismatch
    reason = (
        "dataset analysis differs from its input binding"
        if mismatch in {"dataset_id", "model_id", "variant_id"}
        else "scope or counts"
    )
    with pytest.raises(ValueError, match=reason):
        await DatasetAnalysisExecutor(api.settings).advance(analysis_node.analysis_id)
    assert await analysis_hold(api, analysis_node.analysis_id) == ("running", "held")
    async with api.sessions() as session:
        row = await session.get(TrainingDatasetRevisionRow, analysis_node.dataset_id)
        assert row is not None and row.state == "analyzing"


async def test_reanalysis_preserves_old_result_and_command_replay(
    api: API, analysis_node: AnalysisNode
) -> None:
    from coire_scheduler.datasets import DatasetAnalysisExecutor

    await DatasetAnalysisExecutor(api.settings).advance(analysis_node.analysis_id)
    old = (
        await api.client.get(
            f"/api/v1/admin/dataset-analyses/{analysis_node.analysis_id}", headers=api.headers
        )
    ).json()
    async with api.sessions() as session:
        model = await session.get(ModelRow, uuid.UUID(str(api.metadata["analysis_model_id"])))
        assert model is not None
        model.chat_template = "frozen synthetic template"
        await session.commit()
    body = {
        "model_id": api.metadata["analysis_model_id"],
        "variant_id": api.metadata["analysis_variant_id"],
    }
    url = f"/api/v1/admin/datasets/{analysis_node.dataset_id}/analyze"
    headers = {**api.headers, "idempotency-key": "reanalysis"}
    response = await api.client.post(url, headers=headers, json=body)
    assert response.status_code == 202, response.text
    assert response.json()["analysis_id"] != str(analysis_node.analysis_id)
    assert (await api.client.post(url, headers=headers, json=body)).json() == response.json()
    assert (
        await api.client.post(url, headers={**headers, "idempotency-key": "another"}, json=body)
    ).status_code == 409
    assert (
        await api.client.get(
            f"/api/v1/admin/dataset-analyses/{analysis_node.analysis_id}", headers=api.headers
        )
    ).json() == old
    async with api.sessions() as session:
        new = await session.get(
            TrainingDatasetAnalysisRow, uuid.UUID(response.json()["analysis_id"])
        )
        prior = await session.get(TrainingDatasetAnalysisRow, analysis_node.analysis_id)
        assert (
            new is not None and prior is not None and new.identity_sha256 != prior.identity_sha256
        )
        command = await session.get(TrainingCommandRow, new.command_id)
        assert command is not None
        payload = DatasetRegistrationCommand.model_validate(command.payload)
        assert payload.analysis is not None
        assert payload.analysis.template_override == "frozen synthetic template"


async def test_delete_active_analysis_refuses_then_purges_retaining_provenance(
    api: API, analysis_node: AnalysisNode
) -> None:
    from coire_scheduler.datasets import DatasetAnalysisExecutor

    url = f"/api/v1/admin/datasets/{analysis_node.dataset_id}"
    blocked = await api.client.request(
        "DELETE",
        url,
        headers={**api.headers, "idempotency-key": "delete"},
        json={"expected_version": 1},
    )
    assert blocked.status_code == 409
    await DatasetAnalysisExecutor(api.settings).advance(analysis_node.analysis_id)
    detail = (await api.client.get(url, headers=api.headers)).json()
    headers = {**api.headers, "idempotency-key": "delete"}
    body = {"expected_version": detail["version"]}
    receipt = await api.client.request("DELETE", url, headers=headers, json=body)
    assert receipt.status_code == 202, receipt.text
    assert receipt.json()["state"] == "retired"
    purged = (await api.client.get(url, headers=api.headers)).json()
    assert purged["state"] == "purged"
    assert (
        purged["source_sha256"] == detail["source_sha256"]
        and purged["provenance"] == detail["provenance"]
    )
    assert (
        await api.client.request("DELETE", url, headers=headers, json=body)
    ).json() == receipt.json()
    assert not (
        Path(api.settings.training_dataset_dir) / "sources" / str(analysis_node.dataset_id)
    ).exists()
    async with api.sessions() as session:
        holds = list(await session.scalars(select(TrainingStorageReservationRow)))
        assert all(hold.state == "released" for hold in holds)
        assert await session.get(TrainingDatasetAnalysisRow, analysis_node.analysis_id) is not None


@pytest.mark.parametrize("state", ["queued", "running", "paused", "recovering"])
async def test_nonterminal_job_reference_blocks_delete_then_terminal_purge_marks_nonreproducible(
    api: API, analysis_node: AnalysisNode, state: str
) -> None:
    from coire_scheduler.datasets import DatasetAnalysisExecutor

    await DatasetAnalysisExecutor(api.settings).advance(analysis_node.analysis_id)
    job_id = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
    async with api.sessions() as session:
        row = await session.get(TrainingDatasetRevisionRow, analysis_node.dataset_id)
        assert row is not None
        version = row.version
        session.add(
            TrainingJobRow(
                id=job_id,
                owner_user_id=api.owner,
                model_id=uuid.UUID(str(api.metadata["analysis_model_id"])),
                base_variant_id=uuid.UUID(str(api.metadata["analysis_variant_id"])),
                idempotency_key="reference",
                output_slug="synthetic",
                source_yaml="synthetic",
                source_sha256="a" * 64,
                intent_sha256="b" * 64,
                submitted_spec={},
                state=state,
                queue_deadline_at=datetime.now(UTC) + timedelta(hours=1),
                execution_deadline_at=datetime.now(UTC) + timedelta(hours=1),
            )
        )
        await session.flush()
        session.add(
            TrainingDatasetReferenceRow(
                job_id=job_id,
                dataset_id=row.id,
                analysis_id=analysis_node.analysis_id,
                source_sha256=row.source_sha256,
                split_sha256=row.split_sha256,
            )
        )
        await session.commit()
    url = f"/api/v1/admin/datasets/{analysis_node.dataset_id}"
    headers = {**api.headers, "idempotency-key": "delete-reference"}
    body = {"expected_version": version}
    blocked = await api.client.request("DELETE", url, headers=headers, json=body)
    assert blocked.status_code == 409, blocked.text
    async with api.sessions() as session:
        job = await session.get(TrainingJobRow, job_id)
        assert job is not None and job.reproducible
        job.state = "succeeded"
        await session.commit()
    receipt = await api.client.request("DELETE", url, headers=headers, json=body)
    assert receipt.status_code == 202, receipt.text
    async with api.sessions() as session:
        job = await session.get(TrainingJobRow, job_id)
        assert job is not None and not job.reproducible
        assert (
            await session.get(TrainingDatasetReferenceRow, (job_id, analysis_node.dataset_id))
            is not None
        )


async def test_cleanup_failure_remains_counted_and_scheduler_retries(
    api: API, analysis_node: AnalysisNode, monkeypatch: pytest.MonkeyPatch
) -> None:
    from coire_api.training.storage import DatasetStore
    from coire_scheduler.datasets import DatasetAnalysisExecutor

    executor = DatasetAnalysisExecutor(api.settings)
    await executor.advance(analysis_node.analysis_id)
    original = DatasetStore.purge_source

    async def unavailable(_self: DatasetStore, _identity: uuid.UUID) -> None:
        raise OSError("synthetic cleanup failure")

    monkeypatch.setattr(DatasetStore, "purge_source", unavailable)
    url = f"/api/v1/admin/datasets/{analysis_node.dataset_id}"
    detail = (await api.client.get(url, headers=api.headers)).json()
    receipt = await api.client.request(
        "DELETE",
        url,
        headers={**api.headers, "idempotency-key": "delete"},
        json={"expected_version": detail["version"]},
    )
    assert receipt.status_code == 202
    assert (await api.client.get(url, headers=api.headers)).json()["state"] == "retired"
    async with api.sessions() as session:
        hold = await session.scalar(select(TrainingStorageReservationRow))
        assert hold is not None and hold.state == "retained"
    monkeypatch.setattr(DatasetStore, "purge_source", original)
    await executor.pass_once()
    assert (await api.client.get(url, headers=api.headers)).json()["state"] == "purged"
    async with api.sessions() as session:
        hold = await session.scalar(select(TrainingStorageReservationRow))
        assert hold is not None and hold.state == "released"


async def test_analysis_failure_can_retry_without_rewriting_source_or_old_result(
    api: API, analysis_node: AnalysisNode
) -> None:
    from coire_scheduler.datasets import DatasetAnalysisExecutor

    analysis_node.start_state = "failed"
    await DatasetAnalysisExecutor(api.settings).advance(analysis_node.analysis_id)
    url = f"/api/v1/admin/datasets/{analysis_node.dataset_id}"
    detail = (await api.client.get(url, headers=api.headers)).json()
    assert detail["state"] == "analysis_failed"
    receipt = await api.client.post(
        url + "/analyze",
        headers={**api.headers, "idempotency-key": "retry"},
        json={
            "model_id": api.metadata["analysis_model_id"],
            "variant_id": api.metadata["analysis_variant_id"],
        },
    )
    assert receipt.status_code == 202, receipt.text
    after = (await api.client.get(url, headers=api.headers)).json()
    assert after["source_sha256"] == detail["source_sha256"]
    assert after["split_manifest_sha256"] == detail["split_manifest_sha256"]
    async with api.sessions() as session:
        old = await session.get(TrainingDatasetAnalysisRow, analysis_node.analysis_id)
        assert old is not None and old.state == "failed" and old.result is not None


@pytest.mark.parametrize("change", ["rotation", "revocation", "scope"])
async def test_originating_key_version_is_frozen_and_rechecked_before_dispatch(
    api: API, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    from coire_scheduler.datasets import DatasetAnalysisExecutor

    key_id = uuid.uuid4()
    async with api.sessions() as session:
        session.add(
            ApiKeyRow(
                id=key_id,
                user_id=api.owner,
                name="synthetic key",
                prefix="synthetic",
                secret_hash="not-a-credential",
                scopes=["admin"],
                credential_version=7,
                requests_per_minute=100,
                monthly_budget_tokens=1000,
            )
        )
        await session.commit()
    principal = Principal(
        kind=PrincipalKind.API_KEY,
        user_id=api.owner,
        role=UserRole.ADMIN,
        api_key_id=key_id,
        credential_version=7,
        scopes=frozenset({"admin"}),
    )

    async def authenticate(_request: Request) -> Principal:
        return principal

    monkeypatch.setattr("coire_api.auth.authenticate_request", authenticate)
    response = await api.upload(b'{"text":"one"}\n{"text":"two"}\n')
    assert response.status_code == 202, response.text
    async with api.sessions() as session:
        analysis = await session.scalar(select(TrainingDatasetAnalysisRow))
        assert analysis is not None
        command = await session.get(TrainingCommandRow, analysis.command_id)
        assert (
            command is not None
            and command.payload["originating_key_id"] == str(key_id)
            and command.payload["originating_key_version"] == 7
        )
        key = await session.get(ApiKeyRow, key_id)
        assert key is not None
        if change == "rotation":
            key.credential_version = 8
        elif change == "revocation":
            key.revoked_at = datetime.now(UTC)
        else:
            key.scopes = []
        await session.commit()
    await DatasetAnalysisExecutor(api.settings).advance(analysis.id)
    async with api.sessions() as session:
        row = await session.get(TrainingDatasetAnalysisRow, analysis.id)
        assert row is not None and row.state == "failed"
        assert await session.scalar(select(func.count()).select_from(MemoryReservationRow)) == 0


async def test_real_concurrent_aggregate_quota_cannot_overbook(api: API) -> None:
    from coire_api.training.quota import reserve_upload, upload_reservation_bytes
    from coire_core.errors import TrainingQuotaExceeded

    api.settings.training_dataset_quota_bytes = upload_reservation_bytes(1000, api.settings)
    principal = Principal(kind=PrincipalKind.USER, user_id=api.owner, role=UserRole.ADMIN)

    async def reserve() -> str:
        async with api.sessions() as session:
            try:
                await reserve_upload(
                    session, principal, api.settings, declared_bytes=1000, subject_id=uuid.uuid4()
                )
                await session.commit()
                return "held"
            except TrainingQuotaExceeded:
                await session.rollback()
                return "refused"

    outcomes = await asyncio.gather(*(reserve() for _ in range(4)))
    assert outcomes.count("held") == 1 and outcomes.count("refused") == 3
    async with api.sessions() as session:
        holds = list(await session.scalars(select(TrainingStorageReservationRow)))
        assert len(holds) == 1 and holds[0].bytes == api.settings.training_dataset_quota_bytes


async def test_orphan_sweep_removes_expired_staging_but_preserves_committed_source(
    api: API, analysis_node: AnalysisNode
) -> None:
    from coire_api.training.quota import reconcile_upload_holds
    from coire_api.training.storage import DatasetStore

    store = DatasetStore(api.settings)
    old = datetime.now(UTC) - timedelta(hours=25)
    orphan_id, orphan_dataset = uuid.uuid4(), uuid.uuid4()
    stage = store.stage_path(orphan_id)
    stage.mkdir(mode=0o700)
    (stage / "source.jsonl").write_bytes(b"synthetic orphan")
    async with api.sessions() as session:
        retained = await session.scalar(select(TrainingStorageReservationRow))
        assert retained is not None
        retained.state = "releasing"
        retained.created_at = old
        session.add(
            TrainingStorageReservationRow(
                id=orphan_id,
                owner_user_id=api.owner,
                node_id=None,
                subject_id=str(orphan_dataset),
                bytes=1024,
                state="releasing",
                created_at=old,
            )
        )
        await session.commit()
    async with api.sessions() as session:
        await reconcile_upload_holds(session, api.settings)
        await session.commit()
    assert not stage.exists()
    assert store.source_path(analysis_node.dataset_id).exists()
    async with api.sessions() as session:
        orphan = await session.get(TrainingStorageReservationRow, orphan_id)
        retained = await session.scalar(
            select(TrainingStorageReservationRow).where(
                TrainingStorageReservationRow.subject_id == str(analysis_node.dataset_id)
            )
        )
        assert orphan is not None and orphan.state == "released"
        assert retained is not None and retained.state == "retained"


async def test_result_after_inflight_revocation_cannot_become_ready(
    api: API, analysis_node: AnalysisNode
) -> None:
    from coire_scheduler.datasets import DatasetAnalysisExecutor

    analysis_node.revoke_on_start = True
    await DatasetAnalysisExecutor(api.settings).advance(analysis_node.analysis_id)
    assert await analysis_hold(api, analysis_node.analysis_id) == ("failed", "released")
    async with api.sessions() as session:
        row = await session.get(TrainingDatasetRevisionRow, analysis_node.dataset_id)
        analysis = await session.get(TrainingDatasetAnalysisRow, analysis_node.analysis_id)
        assert row is not None and row.state == "analysis_failed"
        assert analysis is not None and analysis.tokenizer_sha256 is None


@pytest.mark.parametrize("change", ["failed", "copy_manifest", "copy_unverified", "slug"])
async def test_dispatch_rechecks_ready_variant_and_frozen_acquisition_identity(
    api: API, analysis_node: AnalysisNode, change: str
) -> None:
    from coire_scheduler.datasets import DatasetAnalysisExecutor

    variant_id = uuid.UUID(str(api.metadata["analysis_variant_id"]))
    async with api.sessions() as session:
        variant = await session.get(ModelVariantRow, variant_id)
        assert variant is not None
        if change == "failed":
            variant.state = VariantState.FAILED
        elif change == "slug":
            variant.slug = "different-registered-slug"
        else:
            copy = await session.scalar(
                select(VariantCopyRow).where(VariantCopyRow.variant_id == variant_id).limit(1)
            )
            assert copy is not None
            if change == "copy_manifest":
                copy.manifest_sha256 = "b" * 64
            else:
                copy.verified = False
        await session.commit()
    await DatasetAnalysisExecutor(api.settings).advance(analysis_node.analysis_id)
    assert not analysis_node.starts
    async with api.sessions() as session:
        row = await session.get(TrainingDatasetAnalysisRow, analysis_node.analysis_id)
        assert row is not None and row.state == "failed"
        assert await session.scalar(select(func.count()).select_from(MemoryReservationRow)) == 0


async def test_memory_hold_ownership_mismatch_cannot_restart_or_release(
    api: API, analysis_node: AnalysisNode
) -> None:
    from coire_scheduler.datasets import DatasetAnalysisExecutor

    analysis_node.start_state = "running"
    executor = DatasetAnalysisExecutor(api.settings)
    await executor.advance(analysis_node.analysis_id)
    async with api.sessions() as session:
        hold = await session.scalar(select(MemoryReservationRow))
        assert hold is not None
        hold.holder_id = "another-owner"
        await session.commit()
    with pytest.raises(ValueError, match="ownership"):
        await executor.advance(analysis_node.analysis_id)
    assert len(analysis_node.starts) == 1
    async with api.sessions() as session:
        hold = await session.scalar(select(MemoryReservationRow))
        assert hold is not None and hold.state.value == "held"


async def test_concurrent_reanalysis_creates_one_new_pending_identity(
    api: API, analysis_node: AnalysisNode
) -> None:
    from coire_scheduler.datasets import DatasetAnalysisExecutor

    await DatasetAnalysisExecutor(api.settings).advance(analysis_node.analysis_id)
    url = f"/api/v1/admin/datasets/{analysis_node.dataset_id}/analyze"
    body = {
        "model_id": api.metadata["analysis_model_id"],
        "variant_id": api.metadata["analysis_variant_id"],
    }
    outcomes = await asyncio.gather(
        *(
            api.client.post(
                url, headers={**api.headers, "idempotency-key": f"reanalysis-{index}"}, json=body
            )
            for index in range(3)
        )
    )
    assert sorted(response.status_code for response in outcomes) == [202, 409, 409]
    async with api.sessions() as session:
        analyses = list(await session.scalars(select(TrainingDatasetAnalysisRow)))
        assert len(analyses) == 2 and sum(row.state == "queued" for row in analyses) == 1


async def test_queue_timeout_has_no_memory_hold_and_no_late_start(
    api: API, analysis_node: AnalysisNode
) -> None:
    from coire_scheduler.datasets import DatasetAnalysisExecutor

    async with api.sessions() as session:
        row = await session.get(TrainingDatasetAnalysisRow, analysis_node.analysis_id)
        assert row is not None
        row.created_at = datetime.now(UTC) - timedelta(hours=1)
        await session.commit()
    await DatasetAnalysisExecutor(api.settings).advance(analysis_node.analysis_id)
    assert not analysis_node.starts
    async with api.sessions() as session:
        row = await session.get(TrainingDatasetAnalysisRow, analysis_node.analysis_id)
        assert row is not None and row.state == "failed"
        assert await session.scalar(select(func.count()).select_from(MemoryReservationRow)) == 0


@pytest.mark.parametrize("operation", ["analyze", "delete"])
async def test_dataset_mutation_audit_failure_rolls_back_before_source_cleanup(
    api: API, analysis_node: AnalysisNode, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    from coire_core.errors import TrainingUnavailable
    from coire_scheduler.datasets import DatasetAnalysisExecutor

    await DatasetAnalysisExecutor(api.settings).advance(analysis_node.analysis_id)
    url = f"/api/v1/admin/datasets/{analysis_node.dataset_id}"
    before = (await api.client.get(url, headers=api.headers)).json()

    async def unavailable(*_args: object, **_kwargs: object) -> None:
        raise TrainingUnavailable("Mutation audit unavailable")

    monkeypatch.setattr("coire_api.training.service.write_principal_audit", unavailable)
    if operation == "analyze":
        response = await api.client.post(
            url + "/analyze",
            headers={**api.headers, "idempotency-key": "audit-failure"},
            json={
                "model_id": api.metadata["analysis_model_id"],
                "variant_id": api.metadata["analysis_variant_id"],
            },
        )
    else:
        response = await api.client.request(
            "DELETE",
            url,
            headers={**api.headers, "idempotency-key": "audit-failure"},
            json={"expected_version": before["version"]},
        )
    assert response.status_code == 503, response.text
    assert (await api.client.get(url, headers=api.headers)).json() == before
    assert (
        Path(api.settings.training_dataset_dir)
        / "sources"
        / str(analysis_node.dataset_id)
        / "source.jsonl"
    ).exists()
    async with api.sessions() as session:
        assert (
            await session.scalar(select(func.count()).select_from(TrainingDatasetAnalysisRow)) == 1
        )
        hold = await session.scalar(select(TrainingStorageReservationRow))
        assert hold is not None and hold.state == "retained"


@pytest.mark.parametrize("operation", ["analyze", "delete"])
@pytest.mark.parametrize("actor", ["user", "node", "ops", "wrong-origin"])
async def test_new_dataset_mutations_enforce_authority_and_browser_origin(
    api: API, analysis_node: AnalysisNode, operation: str, actor: str
) -> None:
    headers = {**api.headers, "idempotency-key": "refused"}
    if actor == "wrong-origin":
        headers["origin"] = "https://other.test"
    else:
        headers["authorization"] = f"Bearer {actor}"
    url = f"/api/v1/admin/datasets/{analysis_node.dataset_id}"
    if operation == "analyze":
        response = await api.client.post(
            url + "/analyze",
            headers=headers,
            json={
                "model_id": api.metadata["analysis_model_id"],
                "variant_id": api.metadata["analysis_variant_id"],
            },
        )
    else:
        response = await api.client.request(
            "DELETE", url, headers=headers, json={"expected_version": 1}
        )
    assert response.status_code == 403
    async with api.sessions() as session:
        row = await session.get(TrainingDatasetRevisionRow, analysis_node.dataset_id)
        assert row is not None and row.state == "analyzing" and row.version == 1
        assert (
            await session.scalar(
                select(func.count())
                .select_from(AuditRow)
                .where(AuditRow.action == "training.refused")
            )
            == 1
        )
