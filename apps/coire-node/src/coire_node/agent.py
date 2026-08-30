"""Node agent (T055, T056).

Serves `/node/health` on two listeners:

  * the Thunderbolt mesh address — the platform path;
  * the egress (Wi-Fi) address — accepted only with an explicit `X-Coire-Path: fallback`
    marker, counted and logged at WARNING (FR-013b/c).

Both require the per-node token as a bearer credential (FR-013). The mesh is a chain, so
losing the middle node partitions it; refusing the egress path outright would turn a
survivable partition into total loss, but allowing it silently would let the slow path become
the steady state. Hence: allowed, marked, counted, logged.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import socket
from pathlib import Path
from typing import Annotated, Protocol

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from opentelemetry import metrics as otel_metrics

from coire_core.models.node import NodePath, NodeStatus
from coire_core.settings import Settings

logger = logging.getLogger(__name__)

_meter = otel_metrics.get_meter("coire.node")
fallback_counter = _meter.create_counter(
    "coire_node_fallback_requests_total",
    unit="1",
    description="Requests served on the egress listener instead of the mesh.",
)

MESH_SUFFIX = ".mesh"
FALLBACK_HEADER = "x-coire-path"
FALLBACK_VALUE = "fallback"

bearer = HTTPBearer(auto_error=False)
BearerDep = Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]


class SupportsLatest(Protocol):
    """What the agent needs from a metrics source. Keeps the app testable with a stub."""

    def latest(self, *, path: NodePath = ...) -> NodeStatus: ...


def resolve_mesh_address(hostname: str, hosts_file: str = "/etc/hosts") -> str | None:
    """Find this node's mesh address from the managed hosts block (ADR-0002).

    Deliberately reads the hosts file rather than resolving the name: the point is to bind the
    mesh interface specifically, and a resolver could hand back the egress address.
    """
    wanted = f"{hostname}{MESH_SUFFIX}"
    try:
        for line in Path(hosts_file).read_text().splitlines():
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            parts = line.split()
            if len(parts) >= 2 and wanted in parts[1:]:
                return parts[0]
    except OSError as exc:
        logger.warning("cannot read %s: %s", hosts_file, exc)
    return None


def resolve_egress_address() -> str | None:
    """The address on the default route — used only for the alerted fallback listener."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("192.0.2.1", 9))  # TEST-NET-1; no packet is sent
            return str(sock.getsockname()[0])
    except OSError:
        return None


def create_app(settings: Settings, collector: SupportsLatest, *, listener: NodePath) -> FastAPI:
    """Build the agent app for one listener.

    Two apps are created — one per listener — so the egress instance can enforce the fallback
    marker without that check running on the mesh path.
    """
    app = FastAPI(title=f"coire-node ({listener.value})", docs_url=None, openapi_url=None)
    expected_token = settings.node_token.get_secret_value()

    async def require_node_token(credentials: BearerDep) -> None:
        presented = credentials.credentials if credentials else ""
        if not expected_token or not hmac.compare_digest(expected_token, presented):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid node token",
                headers={"WWW-Authenticate": "Bearer"},
            )

    @app.middleware("http")
    async def enforce_path(request: Request, call_next):  # type: ignore[no-untyped-def]
        if listener is NodePath.FALLBACK:
            marker = request.headers.get(FALLBACK_HEADER, "").lower()
            if marker != FALLBACK_VALUE:
                from fastapi.responses import JSONResponse

                return JSONResponse(
                    status_code=status.HTTP_403_FORBIDDEN,
                    content={
                        "detail": (
                            "this is the egress listener; platform traffic belongs on the "
                            "Thunderbolt mesh. Send X-Coire-Path: fallback to use it "
                            "deliberately."
                        )
                    },
                )
            client = request.client.host if request.client else "unknown"
            fallback_counter.add(1, {"node": settings.node_name})
            logger.warning(
                "serving %s on the EGRESS path for %s — ~30x slower than the mesh; "
                "this should not be the steady state (FR-013c)",
                request.url.path,
                client,
            )
        return await call_next(request)

    @app.get("/node/health", response_model=NodeStatus, dependencies=[Depends(require_node_token)])
    async def node_health() -> NodeStatus:
        return collector.latest(path=listener)

    @app.get("/ready")
    async def ready() -> dict[str, object]:
        return {"service": "coire-node", "version": settings.service_version, "ready": True}

    return app


async def serve(settings: Settings, collector: SupportsLatest) -> None:
    """Run both listeners until cancelled."""
    hostname = settings.node_name or socket.gethostname().split(".")[0]
    mesh_addr = resolve_mesh_address(hostname, settings.mesh_hosts_file)
    egress_addr = resolve_egress_address()
    port = settings.node_listen_port

    servers: list[uvicorn.Server] = []
    if mesh_addr:
        servers.append(
            uvicorn.Server(
                uvicorn.Config(
                    create_app(settings, collector, listener=NodePath.MESH),
                    host=mesh_addr,
                    port=port,
                    access_log=False,
                    log_level="info",
                )
            )
        )
        logger.info("mesh listener on %s:%d", mesh_addr, port)
    else:
        logger.error(
            "no mesh address for %s%s in %s — the agent will be reachable only on the "
            "egress path. Run scripts/apply-mesh-hosts.sh.",
            hostname,
            MESH_SUFFIX,
            settings.mesh_hosts_file,
        )

    if egress_addr and egress_addr != mesh_addr:
        servers.append(
            uvicorn.Server(
                uvicorn.Config(
                    create_app(settings, collector, listener=NodePath.FALLBACK),
                    host=egress_addr,
                    port=port,
                    access_log=False,
                    log_level="warning",
                )
            )
        )
        logger.info("egress fallback listener on %s:%d (marker required)", egress_addr, port)

    if not servers:
        raise RuntimeError("no address to bind: neither a mesh nor an egress address resolved")

    await asyncio.gather(*(s.serve() for s in servers))
