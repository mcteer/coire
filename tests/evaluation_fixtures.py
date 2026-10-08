"""Synthetic durable evaluation intent shared by isolated Postgres tests."""

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.auth import Principal, PrincipalKind
from coire_api.db import EvaluationGroupRow, EvaluationRunRow, EvaluationSuiteRow, UserRow
from coire_api.evaluation.catalog import build_suite
from coire_core.models.auth import UserRole
from coire_core.models.evaluation import EvaluationSuiteRegistration, EvaluationWorkload

FIXTURE = Path(__file__).parent / "fixtures/evaluations/workload.json"


async def seed_evaluation(session: AsyncSession, *, expired: bool = False) -> str:
    now = datetime.now(UTC)
    owner = uuid.uuid4()
    session.add(
        UserRow(
            id=owner,
            email=f"{owner}@eval.test",
            display_name="Admin",
            role=UserRole.ADMIN,
            active=True,
        )
    )
    await session.flush()
    suite = build_suite(
        EvaluationSuiteRegistration(
            suite_id="task-recovery", version=1, template_id="task-coding-instructions"
        ),
        owner=owner,
        judge=None,
        now=now,
    )
    catalog = EvaluationSuiteRow(
        id=uuid.uuid4(),
        suite_id=suite.suite_id,
        version=1,
        registry_version=1,
        definition=suite.model_dump(mode="json"),
        content_sha256=suite.content_sha256,
        owner_user_id=owner,
        retired=False,
    )
    session.add(catalog)
    await session.flush()
    fixture = EvaluationWorkload.model_validate_json(FIXTURE.read_bytes())
    identity = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
    session.add(
        EvaluationGroupRow(
            id=identity,
            owner_user_id=owner,
            origin="manual",
            subjects=[fixture.target.model_dump(mode="json")],
        )
    )
    await session.flush()
    session.add(
        EvaluationRunRow(
            id=identity,
            group_id=identity,
            owner_user_id=owner,
            suite_row_id=catalog.id,
            suite_snapshot=suite.model_dump(mode="json"),
            subjects=[fixture.target.model_dump(mode="json")],
            authorization_snapshot=Principal(
                kind=PrincipalKind.USER, user_id=owner, role=UserRole.ADMIN
            ).model_dump(mode="json"),
            request_sha256="a" * 64,
            idempotency_key_sha256="b" * 64,
            state="queued",
            fence=1,
            version=1,
            next_event_sequence=1,
            cleanup_state="complete",
            evidence_reserved_bytes=0,
            queue_deadline_at=now + timedelta(seconds=-1 if expired else 60),
            execution_deadline_at=now + timedelta(minutes=10),
        )
    )
    await session.commit()
    return identity


async def seed_evaluation_resident(session: AsyncSession) -> tuple[uuid.UUID, uuid.UUID]:
    """One inert acquired target and accounted ready resident; never starts an engine."""
    from coire_api.db import (
        EngineProcessRow,
        InstanceMemberRow,
        MemoryReservationRow,
        ModelInstanceRow,
        ModelRow,
        ModelVariantRow,
        NodeRow,
        VariantCopyRow,
    )
    from coire_core.models.acquisition import VariantState
    from coire_core.models.engine import EngineState
    from coire_core.models.instance import InstanceState
    from coire_core.models.node import NodeRole, Reachability
    from coire_core.models.placement import MemoryReservationState, ReservationHolder
    from coire_core.models.registry import CopyRole, ModelState

    work = EvaluationWorkload.model_validate_json(FIXTURE.read_bytes())
    session.add(
        ModelRow(
            id=work.target.target.model_id,
            repo_id="synthetic/evaluation",
            slug="synthetic-evaluation",
            display_name="Synthetic",
            state=ModelState.READY,
            visibility="admin_only",
            placement_policy="single:auto",
            memory_estimate_bytes=1024,
            idle_ttl_seconds=900,
            precision="bf16",
            weight_bytes=1024,
            total_bytes=1024,
            file_count=2,
        )
    )
    await session.flush()
    session.add(
        ModelVariantRow(
            id=work.target.target.variant_id,
            model_id=work.target.target.model_id,
            name="fixture",
            slug="synthetic-evaluation",
            precision="bf16",
            source_revision="synthetic",
            memory_estimate_bytes=1024,
            state=VariantState.READY,
            validated=True,
        )
    )
    node = NodeRow(
        id=uuid.uuid4(),
        name="coire-edge-a",
        role=NodeRole.STUDIO,
        reachability=Reachability.HEALTHY,
        memory_total_bytes=128 * 1024**3,
        disk_total_bytes=1024**4,
        gpu_cores=60,
        agent_version="fixture",
    )
    session.add(node)
    await session.flush()
    session.add(
        VariantCopyRow(
            variant_id=work.target.target.variant_id,
            node_id=node.id,
            path="/synthetic/evaluation",
            bytes=1024,
            manifest_sha256=work.target.target.base_manifest_sha256,
            verified=True,
            verified_at=datetime.now(UTC),
            role=CopyRole.ORIGIN,
        )
    )
    instance = ModelInstanceRow(
        id=uuid.uuid4(),
        model_id=work.target.target.model_id,
        variant_id=work.target.target.variant_id,
        policy="single:coire-edge-a",
        state=InstanceState.READY,
    )
    session.add(instance)
    await session.flush()
    hold = MemoryReservationRow(
        id=uuid.uuid4(),
        node_id=node.id,
        holder_type=ReservationHolder.MODEL,
        holder_id=str(instance.id),
        bytes=1024,
        pinned=True,
        state=MemoryReservationState.HELD,
    )
    process = EngineProcessRow(
        id=uuid.uuid4(),
        instance_id=instance.id,
        model_id=instance.model_id,
        variant_id=instance.variant_id,
        node_id=node.id,
        port=9999,
        pid=5,
        process_create_time=1,
        state=EngineState.READY,
        estimate_bytes=1024,
    )
    session.add_all([hold, process])
    await session.flush()
    session.add(
        InstanceMemberRow(
            instance_id=instance.id,
            node_id=node.id,
            rank=0,
            host="127.0.0.1",
            port=9999,
            engine_id=process.id,
            reservation_id=hold.id,
        )
    )
    await session.commit()
    return node.id, instance.id
