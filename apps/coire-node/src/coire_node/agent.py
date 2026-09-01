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

from coire_core.models.node import NetworkPath, NodePath, NodeStatus, NodeStatusV2
from coire_core.settings import Settings
from coire_node.benchmarks import BenchmarkRunner
from coire_node.docker_api import DockerAPI
from coire_node.engines import EngineManager
from coire_node.grants import Grants
from coire_node.jobs import JobSupervisor
from coire_node.link_probes import LinkProbeRunner
from coire_node.reservations import ReservationLedger
from coire_node.routes import benchmarks as benchmark_routes
from coire_node.routes import engines as engines_routes
from coire_node.routes import export as export_routes
from coire_node.routes import jobs as jobs_routes
from coire_node.routes import link_probes as link_probe_routes
from coire_node.routes import models as models_routes
from coire_node.routes import runs as runs_routes
from coire_node.routes import sharding as sharding_routes
from coire_node.runs import RunManager
from coire_node.sharding import ShardGroupManager
from coire_node.store import Store

logger = logging.getLogger(__name__)

_meter = otel_metrics.get_meter("coire.node")
fallback_counter = _meter.create_counter(
    "coire_node_fallback_requests_total",
    unit="1",
    description="Requests served on the egress listener instead of the mesh.",
)
forbidden_path_counter = _meter.create_counter(
    "coire_forbidden_cross_fabric_requests_total",
    unit="1",
    description="Requests received on a listener for the wrong network purpose.",
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


def resolve_data_address(hostname: str, hosts_file: str = "/etc/hosts") -> str | None:
    """Resolve this Studio's managed ``.fabric`` binding without using control DNS."""
    wanted = f"{hostname}.fabric"
    try:
        for line in Path(hosts_file).read_text().splitlines():
            parts = line.split("#", 1)[0].split()
            if len(parts) >= 2 and wanted in parts[1:]:
                return parts[0]
    except OSError as exc:
        logger.warning("cannot read %s: %s", hosts_file, exc)
    return None


def resolve_control_address(hostname: str) -> str | None:
    try:
        return socket.gethostbyname(hostname)
    except OSError as exc:
        logger.error("cannot resolve control host %s: %s", hostname, exc)
        return None


def resolve_egress_address() -> str | None:
    """The address on the default route — used only for the alerted fallback listener."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("192.0.2.1", 9))  # TEST-NET-1; no packet is sent
            return str(sock.getsockname()[0])
    except OSError:
        return None


def create_app(
    settings: Settings,
    collector: SupportsLatest,
    *,
    listener: NodePath | NetworkPath,
    store: Store | None = None,
    jobs: JobSupervisor | None = None,
    engines: EngineManager | None = None,
    grants: Grants | None = None,
    reservations: ReservationLedger | None = None,
    shard_groups: ShardGroupManager | None = None,
    link_probes: LinkProbeRunner | None = None,
    benchmarks: BenchmarkRunner | None = None,
    runs: RunManager | None = None,
) -> FastAPI:
    """Build the agent app for one listener.

    Two apps are created — one per listener — so the egress instance can enforce the fallback
    marker without that check running on the mesh path, and so the **export routes exist only
    on the mesh app**. A model copy may not cross the egress interface (spec FR-007); the
    surest way to guarantee that is for the route not to be there.
    """
    app = FastAPI(title=f"coire-node ({listener.value})", docs_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.store = store
    app.state.jobs = jobs
    app.state.engines = engines
    app.state.grants = grants
    app.state.reservations = reservations
    app.state.shard_groups = shard_groups
    app.state.link_probes = link_probes
    app.state.benchmarks = benchmarks
    app.state.runs = runs
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
        if listener is NetworkPath.DATA and not request.url.path.startswith(
            ("/node/export/", "/ready")
        ):
            forbidden_path_counter.add(
                1, {"network_path": "data", "node": settings.node_name, "peer": "unknown"}
            )
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

    if listener is not NetworkPath.DATA:

        @app.get(
            "/node/health",
            response_model=NodeStatus | NodeStatusV2,
            dependencies=[Depends(require_node_token)],
        )
        async def node_health() -> NodeStatus | NodeStatusV2:
            legacy_path = listener if isinstance(listener, NodePath) else NodePath.MESH
            status_value = collector.latest(path=legacy_path)
            if listener is NetworkPath.CONTROL:
                return NodeStatusV2.model_validate(
                    status_value.model_dump(exclude={"path"}) | {"path": "control"}
                )
            return status_value

        @app.get("/node/data-link", dependencies=[Depends(require_node_token)])
        async def data_link_status():  # type: ignore[no-untyped-def]
            measure = getattr(collector, "data_link_status", None)
            if measure is None:
                raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "link probe unavailable")
            return measure(port=settings.node_data_listen_port)

    @app.get("/ready")
    async def ready() -> dict[str, object]:
        return {"service": "coire-node", "version": settings.service_version, "ready": True}

    # Feature 001 verbs. All bearer-authenticated, like /node/health.
    if store is not None and listener is not NetworkPath.DATA:
        guard = [Depends(require_node_token)]
        app.include_router(models_routes.router, dependencies=guard)
        app.include_router(jobs_routes.router, dependencies=guard)
        app.include_router(engines_routes.router, dependencies=guard)
        app.include_router(sharding_routes.router, dependencies=guard)
        app.include_router(link_probe_routes.router, dependencies=guard)
        app.include_router(benchmark_routes.router, dependencies=guard)
        app.include_router(runs_routes.router, dependencies=guard)

        # The data path for peer replication. Mesh listener only, and authorised by the grant
        # in the URL rather than the node bearer, because the peer does not hold that token
        # and should not (spec FR-007, research R3).
        if listener in (NodePath.MESH, NetworkPath.DATA):
            app.include_router(export_routes.router)

    if store is not None and listener is NetworkPath.DATA:
        app.include_router(export_routes.router)

    return app


async def serve(settings: Settings, collector: SupportsLatest) -> None:
    """Run both listeners until cancelled."""
    hostname = settings.node_name or socket.gethostname().split(".")[0]
    mesh_addr = resolve_mesh_address(hostname, settings.mesh_hosts_file)
    egress_addr = resolve_egress_address()
    control_addr = resolve_control_address(settings.node_control_host or hostname)
    data_addr = resolve_data_address(hostname, settings.mesh_hosts_file)
    port = settings.node_listen_port

    # Feature 001's collaborators, built once and shared by both listeners.
    store = Store(settings.node_store_dir)
    store.ensure_root()
    jobs = JobSupervisor(settings, store)
    grants = Grants()
    # Bare engines are private implementation details of coire-node. The authenticated
    # control listener is their sole network boundary; no engine port binds to Wi-Fi.
    engines = EngineManager(settings, store, "127.0.0.1")
    reservations = ReservationLedger(settings, store, engines.committed_bytes)
    shard_groups = ShardGroupManager(settings, store)
    link_probes = LinkProbeRunner(settings)
    benchmarks = BenchmarkRunner(settings, store)
    docker = DockerAPI(settings.run_docker_socket)
    runs = RunManager(settings, docker)

    # Re-own engines that outlived the previous agent process, and re-attach to jobs that were
    # running. Both happen before the listeners bind, so the first /node/health after a restart
    # already tells the truth (spec FR-015, edge case 3).
    adopted = engines.adopt_from_state()
    if adopted:
        logger.info("adopted %d running engine(s) after restart", len(adopted))
    resumed = jobs.resume_all()
    if resumed:
        logger.info("resumed %d acquisition job(s)", resumed)
    engines.start_health_loop()
    if hasattr(collector, "attach"):
        collector.attach(store=store, jobs=jobs, engines=engines)

    servers: list[uvicorn.Server] = []
    if not settings.legacy_network_mode and control_addr:
        servers.append(
            uvicorn.Server(
                uvicorn.Config(
                    create_app(
                        settings,
                        collector,
                        listener=NetworkPath.CONTROL,
                        store=store,
                        jobs=jobs,
                        engines=engines,
                        grants=grants,
                        reservations=reservations,
                        shard_groups=shard_groups,
                        link_probes=link_probes,
                        benchmarks=benchmarks,
                        runs=runs,
                    ),
                    host=control_addr,
                    port=port,
                    access_log=False,
                    log_level="info",
                )
            )
        )
        logger.info("control listener on %s:%d", control_addr, port)
        if data_addr:
            servers.append(
                uvicorn.Server(
                    uvicorn.Config(
                        create_app(
                            settings,
                            collector,
                            listener=NetworkPath.DATA,
                            store=store,
                            jobs=jobs,
                            engines=engines,
                            grants=grants,
                            reservations=reservations,
                            shard_groups=shard_groups,
                            link_probes=link_probes,
                            benchmarks=benchmarks,
                        ),
                        host=data_addr,
                        port=settings.node_data_listen_port,
                        access_log=False,
                        log_level="info",
                    )
                )
            )
            logger.info("data listener on %s:%d", data_addr, settings.node_data_listen_port)
    elif settings.legacy_network_mode and mesh_addr:
        servers.append(
            uvicorn.Server(
                uvicorn.Config(
                    create_app(
                        settings,
                        collector,
                        listener=NodePath.MESH,
                        store=store,
                        jobs=jobs,
                        engines=engines,
                        grants=grants,
                        reservations=reservations,
                        runs=runs,
                    ),
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

    if settings.legacy_network_mode and egress_addr and egress_addr != mesh_addr:
        servers.append(
            uvicorn.Server(
                uvicorn.Config(
                    create_app(
                        settings,
                        collector,
                        listener=NodePath.FALLBACK,
                        store=store,
                        jobs=jobs,
                        engines=engines,
                        grants=grants,
                        reservations=reservations,
                        runs=runs,
                    ),
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

    try:
        await asyncio.gather(*(s.serve() for s in servers))
    finally:
        await engines_routes.close_engine_client()
        await docker.close()
        # Engines are deliberately left running: the agent is restartable and they are not
        # (spec FR-015).
        engines.shutdown()
        jobs.shutdown()
