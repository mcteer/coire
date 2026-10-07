"""Periodic reconciliation must carry the immutable load identity, including private smoke targets."""

import uuid
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.db import EngineProcessRow, NodeRow, PlacementCommandRow
from coire_api.nodes_client import NodeClient
from coire_api.registry.reconciler import RegistryReconciler
from coire_core.models.adapters import InferenceTarget
from coire_core.models.engine import EngineState, EngineStatus, ReconcileRequest, ReconcileResult
from coire_core.models.node import NodeRole
from coire_core.models.registry import EngineBackend
from coire_core.settings import Settings


@pytest.mark.parametrize("kind", ["legacy-base", "exact-visual-base", "private-adapter"])
async def test_reconciliation_preserves_recorded_slug_backend_and_exact_target(kind: str) -> None:
    node = NodeRow(id=uuid.uuid4(), name="coire-edge-a", role=NodeRole.STUDIO)
    model_id, variant_id = uuid.uuid4(), uuid.uuid4()
    adapter_id = uuid.uuid4() if kind == "private-adapter" else None
    backend = EngineBackend.MLX_VLM if kind == "exact-visual-base" else EngineBackend.MLX_LM
    target = (
        InferenceTarget(
            model_id=model_id,
            variant_id=variant_id,
            adapter_id=adapter_id,
            base_manifest_sha256="a" * 64,
            adapter_manifest_sha256="b" * 64 if adapter_id else None,
        )
        if kind != "legacy-base"
        else None
    )
    engine = EngineProcessRow(
        id=uuid.uuid4(),
        model_id=model_id,
        variant_id=variant_id,
        adapter_id=adapter_id,
        node_id=node.id,
        backend=backend.value,
        port=9500,
        pid=123,
        process_create_time=42.0,
        state=EngineState.READY,
        started_at=datetime.now(UTC),
    )
    command = PlacementCommandRow(
        engine_id=engine.id,
        operation="load",
        payload={
            "slug": "registered-exact-variant",
            "backend": backend.value,
            "target": target.model_dump(mode="json") if target else None,
        },
    )

    class Results:
        def __init__(self, rows: list[object]) -> None:
            self.rows = rows

        def scalar_one_or_none(self) -> object | None:
            return self.rows[0] if self.rows else None

        def scalars(self) -> "Results":
            return self

        def all(self) -> list[object]:
            return self.rows

    class Session:
        async def execute(self, query: object) -> Results:
            return Results([node] if "FROM nodes" in str(query) else [engine])

        async def scalar(self, query: object) -> PlacementCommandRow:
            assert "placement_commands" in str(query)
            return command

    class Client:
        async def reconcile(self, name: str, request: ReconcileRequest) -> ReconcileResult:
            assert name == node.name and len(request.expected) == 1
            expected = request.expected[0]
            assert expected.slug == command.payload["slug"]
            assert expected.backend is backend and expected.target == target
            assert expected.pid == engine.pid and expected.process_create_time == 42.0
            return ReconcileResult(
                adopted=[
                    EngineStatus(
                        engine_id=engine.id,
                        slug=expected.slug,
                        backend=backend,
                        target=target,
                        port=9500,
                        pid=123,
                        process_create_time=42.0,
                        state=EngineState.READY,
                        started_at=engine.started_at,
                    )
                ]
            )

    reconciler = RegistryReconciler(Settings(_secrets_dir="/none"))  # type: ignore[call-arg]
    reconciler._node_reconcile.add(node.name)
    await reconciler._reconcile_nodes(cast(AsyncSession, Session()), cast(NodeClient, Client()))
    assert engine.state is EngineState.READY


async def test_recorded_adapter_target_cannot_be_rebound_to_another_adapter() -> None:
    from coire_api.registry.reconciler import engine_reconcile_expectation

    model, variant, adapter = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    engine = EngineProcessRow(
        id=uuid.uuid4(),
        model_id=model,
        variant_id=variant,
        adapter_id=adapter,
        node_id=uuid.uuid4(),
        backend="mlx_lm",
        port=9500,
        state=EngineState.READY,
    )
    changed = InferenceTarget(
        model_id=model,
        variant_id=variant,
        adapter_id=uuid.uuid4(),
        base_manifest_sha256="a" * 64,
        adapter_manifest_sha256="b" * 64,
    )

    class Session:
        async def scalar(self, query: object) -> Any:
            return PlacementCommandRow(
                operation="load",
                payload={
                    "slug": "registered-exact-variant",
                    "backend": "mlx_lm",
                    "target": changed.model_dump(mode="json"),
                },
            )

    with pytest.raises(ValueError, match="identity"):
        await engine_reconcile_expectation(cast(AsyncSession, Session()), engine)
