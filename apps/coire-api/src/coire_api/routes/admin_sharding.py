"""Authenticated and audited sharding administration."""

from __future__ import annotations

import hashlib
import uuid
from pathlib import Path

import anyio
from fastapi import APIRouter, HTTPException, status
from opentelemetry import metrics, trace
from sqlalchemy import select

from coire_api.audit import write_principal_audit
from coire_api.auth import CurrentAdmin
from coire_api.benchmarks import project_run
from coire_api.db import (
    BenchmarkCommandRow,
    BenchmarkRunRow,
    ModelVariantRow,
    NodeRow,
    VariantCopyRow,
)
from coire_api.deps import SessionDep, SettingsDep
from coire_api.nodes_client import NodeClient, NodeError
from coire_api.sharding import append_observation, link_projection
from coire_core.models import (
    BenchmarkCommand,
    BenchmarkRequest,
    BenchmarkRun,
    BenchmarkRunState,
    LinkProbeCommand,
    LinkProbeRequest,
    ProbeTransport,
    StudioLinkProjection,
)

router = APIRouter(prefix="/api/v1/admin", tags=["admin: sharding"])
tracer = trace.get_tracer("coire.api.sharding")
meter = metrics.get_meter("coire.api.sharding")
link_probes = meter.create_counter("coire_sharding_link_probes_total", unit="1")


@router.get("/links/studios", response_model=StudioLinkProjection)
async def studio_link(
    principal: CurrentAdmin, session: SessionDep, settings: SettingsDep
) -> StudioLinkProjection:
    return await link_projection(session, settings)


@router.post("/benchmarks", response_model=BenchmarkRun, status_code=status.HTTP_202_ACCEPTED)
async def create_benchmark(
    body: BenchmarkRequest,
    principal: CurrentAdmin,
    session: SessionDep,
    settings: SettingsDep,
) -> BenchmarkRun:
    variant = await session.get(ModelVariantRow, body.variant_id)
    if variant is None or not variant.validated:
        raise HTTPException(status.HTTP_409_CONFLICT, "benchmark variant is not verified")
    nodes = list(
        (
            await session.execute(
                select(NodeRow)
                .where(NodeRow.name.in_(["coire-edge-a", "coire-edge-b"]))
                .order_by(NodeRow.name)
            )
        )
        .scalars()
        .all()
    )
    copies = set(
        (
            await session.execute(
                select(VariantCopyRow.node_id).where(
                    VariantCopyRow.variant_id == variant.id,
                    VariantCopyRow.verified.is_(True),
                )
            )
        ).scalars()
    )
    if [node.name for node in nodes] != ["coire-edge-a", "coire-edge-b"] or any(
        node.id not in copies for node in nodes
    ):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "both declared Studios require a verified copy for the full comparison",
        )
    digests: dict[str, str | None] = {"single:coire-edge-a": None}
    for placement, configured in (
        ("sharded:tp", settings.sharding_jaccl_hostfile),
        ("sharded:pp", settings.sharding_ring_hostfile),
    ):
        try:
            payload = await anyio.to_thread.run_sync(Path(configured).read_bytes)
        except OSError as exc:
            raise HTTPException(
                status.HTTP_409_CONFLICT, f"{placement} hostfile unavailable"
            ) from exc
        digests[placement] = hashlib.sha256(payload).hexdigest()
    run = BenchmarkRunRow(
        variant_id=variant.id,
        state=BenchmarkRunState.QUEUED,
        prompt_tokens=body.prompt_tokens,
        generation_tokens=body.generation_tokens,
    )
    session.add(run)
    await session.flush()
    for sequence, placement in enumerate(body.placements):
        command_id = uuid.uuid5(uuid.NAMESPACE_URL, f"coire:benchmark:{run.id}:{placement}")
        command = BenchmarkCommand(
            command_id=command_id,
            run_id=run.id,
            variant_id=variant.id,
            slug=variant.slug,
            placement=placement,
            prompt_tokens=body.prompt_tokens,
            generation_tokens=body.generation_tokens,
            hostfile_sha256=digests[placement],
        )
        session.add(
            BenchmarkCommandRow(
                id=command_id,
                run_id=run.id,
                node_id=nodes[0].id,
                sequence=sequence,
                payload=command.model_dump(mode="json"),
            )
        )
    await write_principal_audit(
        session,
        principal=principal,
        action="benchmark.create",
        target_type="benchmark_run",
        target_id=str(run.id),
        detail={"variant_id": str(variant.id), "placements": body.placements},
    )
    await session.commit()
    await session.refresh(run)
    return await project_run(session, run)


@router.get("/benchmarks", response_model=list[BenchmarkRun])
async def list_benchmarks(principal: CurrentAdmin, session: SessionDep) -> list[BenchmarkRun]:
    rows = list(
        (await session.execute(select(BenchmarkRunRow).order_by(BenchmarkRunRow.created_at.desc())))
        .scalars()
        .all()
    )
    return [await project_run(session, row) for row in rows]


@router.post(
    "/links/studios/probe",
    response_model=StudioLinkProjection,
    status_code=status.HTTP_202_ACCEPTED,
)
async def probe_studio_link(
    body: LinkProbeRequest,
    principal: CurrentAdmin,
    session: SessionDep,
    settings: SettingsDep,
) -> StudioLinkProjection:
    before = await link_projection(session, settings)
    if not body.force and before.latest and before.required_after is not None:
        current = {
            item.transport for item in before.latest if item.observed_at >= before.required_after
        }
        if current == {ProbeTransport.JACCL, ProbeTransport.RING}:
            return before
    commands: list[LinkProbeCommand] = []
    for transport, configured in (
        (ProbeTransport.JACCL, settings.sharding_jaccl_hostfile),
        (ProbeTransport.RING, settings.sharding_ring_hostfile),
    ):
        try:
            payload = await anyio.to_thread.run_sync(Path(configured).read_bytes)
            digest = hashlib.sha256(payload).hexdigest()
        except OSError as exc:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"generated {transport.value} hostfile is unavailable",
            ) from exc
        commands.append(
            LinkProbeCommand(command_id=uuid.uuid4(), transport=transport, hostfile_sha256=digest)
        )
    try:
        async with NodeClient(settings, timeout=settings.sharding_start_timeout_s) as client:
            for command in commands:
                observation = await client.run_link_probe("coire-edge-a", command)
                await append_observation(session, observation)
    except (NodeError, LookupError) as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    await write_principal_audit(
        session,
        principal=principal,
        action="link.probe",
        target_type="studio_link",
        target_id="coire-edge-a:coire-edge-b",
        detail={"transports": [item.transport.value for item in commands]},
    )
    await session.commit()
    for observation in (await link_projection(session, settings)).latest[:2]:
        link_probes.add(
            1,
            {"transport": observation.transport.value, "outcome": observation.outcome.value},
        )
    return await link_projection(session, settings)
