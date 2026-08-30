"""Node registration.

Nodes are *declared*, never discovered (Principle IV; ARCHITECTURE.md 4.1 declines exo's
auto-discovery explicitly). A name absent from `deploy/cluster/nodes.yaml` is refused with 403
whatever token it presents, so a rogue machine on the network cannot become a worker.

The static per-node token is a time-boxed exception recorded in ADR-0001; feature 005 replaces
it with issued registration tokens.
"""

from __future__ import annotations

import hmac
import logging
import uuid
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path

import yaml
from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select

from coire_api.auth import CurrentPrincipal
from coire_api.db import NodeRow
from coire_api.deps import SessionDep, SettingsDep
from coire_core.models.node import Node, NodeRegistration, NodeRole, Reachability

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/nodes", tags=["nodes"])


@lru_cache(maxsize=4)
def load_inventory(path: str) -> dict[str, dict[str, object]]:
    """Declared node inventory. Cached; the file is baked into the image."""
    try:
        raw = yaml.safe_load(Path(path).read_text()) or {}
    except OSError:
        logger.error("node inventory %s is unreadable; all registration will be refused", path)
        return {}
    nodes = raw.get("nodes", {})
    if not isinstance(nodes, dict):
        return {}
    return {str(k): dict(v or {}) for k, v in nodes.items()}


@router.post("/register", response_model=Node)
async def register_node(
    registration: NodeRegistration,
    principal: CurrentPrincipal,
    session: SessionDep,
    settings: SettingsDep,
) -> Node:
    """Register or re-register a declared node. Idempotent on name."""
    inventory = load_inventory(settings.node_inventory_file)
    declared = inventory.get(registration.name)
    if declared is None:
        logger.warning(
            "registration refused: %s is not in the declared inventory", registration.name
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"{registration.name} is not a declared node",
        )

    expected = settings.node_token_map.get(registration.name, "")
    presented = registration.token.get_secret_value()
    if not expected or not hmac.compare_digest(expected, presented):
        logger.warning("registration refused: bad token for %s", registration.name)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid node token")

    now = datetime.now(UTC)
    row = (
        await session.execute(select(NodeRow).where(NodeRow.name == registration.name))
    ).scalar_one_or_none()

    if row is None:
        row = NodeRow(
            id=uuid.uuid4(),
            name=registration.name,
            role=NodeRole(str(declared.get("role", NodeRole.STUDIO.value))),
            registered_at=now,
        )
        session.add(row)

    row.mesh_address = str(registration.mesh_address)
    row.egress_address = str(registration.egress_address)
    row.memory_total_bytes = registration.memory_total_bytes
    row.disk_total_bytes = registration.disk_total_bytes
    row.gpu_cores = registration.gpu_cores
    row.agent_version = registration.agent_version
    row.last_seen_at = now
    row.reachability = Reachability.HEALTHY
    row.probe_failures = 0

    await session.commit()
    await session.refresh(row)

    return Node(
        id=row.id,
        name=row.name,
        role=row.role,
        mesh_address=row.mesh_address,  # type: ignore[arg-type]
        egress_address=row.egress_address,  # type: ignore[arg-type]
        memory_total_bytes=row.memory_total_bytes,
        disk_total_bytes=row.disk_total_bytes,
        gpu_cores=row.gpu_cores,
        agent_version=row.agent_version,
        registered_at=row.registered_at,
        last_seen_at=row.last_seen_at,
        reachability=row.reachability,
    )
