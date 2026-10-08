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
import hashlib
import hmac
import json
import logging
import shutil
import socket
import sys
import threading
import uuid
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Protocol

import psutil
import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from opentelemetry import metrics as otel_metrics

from coire_core.errors import TrainingConflict
from coire_core.models.acquisition import ReservationRequest, ReservationState
from coire_core.models.engine import EngineState
from coire_core.models.node import NetworkPath, NodePath, NodeStatus, NodeStatusV2
from coire_core.models.training_node import (
    NodeTrainingCapabilities,
    TrainingAdapterExtractRequest,
    TrainingArtifactImportRequest,
    TrainingArtifactManifest,
    TrainingMeasurementPrepare,
)
from coire_core.settings import Settings
from coire_node import __version__
from coire_node.benchmarks import BenchmarkRunner
from coire_node.docker_api import DockerAPI
from coire_node.engines import EngineManager
from coire_node.grants import Grants
from coire_node.image_dispatch import ImageNodeDispatcher
from coire_node.image_jobs import ImageJobJournal
from coire_node.image_runtime.supervisor import ImageProcessSupervisor, ImageProcessUnavailable
from coire_node.jobs import JobSupervisor
from coire_node.link_probes import LinkProbeRunner
from coire_node.reservations import ReservationLedger, TrainingDiskBudget
from coire_node.routes import benchmarks as benchmark_routes
from coire_node.routes import engines as engines_routes
from coire_node.routes import evaluations as evaluations_routes
from coire_node.routes import export as export_routes
from coire_node.routes import failover as failover_routes
from coire_node.routes import image_jobs as image_jobs_routes
from coire_node.routes import image_workers as image_workers_routes
from coire_node.routes import jobs as jobs_routes
from coire_node.routes import link_probes as link_probe_routes
from coire_node.routes import models as models_routes
from coire_node.routes import runs as runs_routes
from coire_node.routes import sharding as sharding_routes
from coire_node.routes import workspaces as workspaces_routes
from coire_node.runs import RunManager
from coire_node.sharding import ShardGroupManager
from coire_node.store import Store

if TYPE_CHECKING:
    from coire_node.training.artifacts import TrainingArtifacts
from coire_node.training.analysis_supervisor import AnalysisSupervisor
from coire_node.training.components import RankImporter
from coire_node.training.extraction import AdapterExtractor
from coire_node.training.importer import ArtifactImporter
from coire_node.training.journal import TrainingJournal
from coire_node.training.lease_snapshot import TrainingLeaseSnapshotReader
from coire_node.training.measurement import MeasurementSupervisor
from coire_node.training.supervisor import TrainingSupervisor

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


class AccountedArtifactImporter(ArtifactImporter):
    """Bind the existing transfer implementation to shared durable admission."""

    def __init__(
        self,
        artifacts: TrainingArtifacts,
        journal_root: Path,
        *,
        port: int,
        reservations: ReservationLedger,
        training: TrainingSupervisor,
        max_bytes: int = 200 * 1024**3,
        disk_floor_bytes: int = 20 * 1024**3,
    ) -> None:
        super().__init__(
            artifacts,
            journal_root,
            port=port,
            max_bytes=max_bytes,
            disk_floor_bytes=disk_floor_bytes,
        )
        self.reservations = reservations
        self.training = training
        self._reservation_scopes: dict[uuid.UUID, list[uuid.UUID]] = {}
        for identity, workflow in reservations.owner_scopes("artifact-import"):
            if self._record(workflow) is None:
                raise TrainingConflict("Import reservation intent needs reconciliation")
            self._release_scope(identity)

    def _new_scope(self, command: uuid.UUID) -> uuid.UUID:
        identity = uuid.uuid4()
        self._reservation_scopes.setdefault(command, []).append(identity)
        return identity

    def _release_scope(self, identity: uuid.UUID) -> None:
        if self.reservations.get(identity) is not None:
            self.reservations.bind_owner(
                identity, release_check=lambda: True, footprint_bytes=lambda: None
            )
            self.reservations.release(identity)

    async def _run(self, request: TrainingArtifactImportRequest) -> None:
        copy = asyncio.create_task(super()._run(request))
        try:
            await asyncio.shield(copy)
        except asyncio.CancelledError:
            # Do not abandon a filesystem thread then advertise its hold as free.
            await asyncio.shield(copy)
            raise
        finally:
            # Base transfer has drained its streams/thread work; restart has no copying child.
            if not copy.done():
                await asyncio.shield(copy)
            if not copy.cancelled():
                for identity in self._reservation_scopes.pop(request.command_id, []):
                    self._release_scope(identity)

    async def _fetch_manifest(
        self, request: TrainingArtifactImportRequest
    ) -> TrainingArtifactManifest:
        self.reservations.hold(
            ReservationRequest(
                idempotency_key=self._new_scope(request.command_id),
                workflow_id=request.command_id,
                variant_id=request.artifact_id,
                memory_bytes=192 * 1024**2,
                disk_bytes=1,
            ),
            disk_path=self.artifacts.root,
            disk_floor_bytes=self.disk_floor_bytes,
            require_stop=True,
            owner_kind="artifact-import",
        )
        manifest = await super()._fetch_manifest(request)
        with self.training.journal.lock:
            if (self.artifacts.root / str(request.artifact_id)).exists():
                return manifest  # Existing bytes are already in the retained quota projection.
            credit = 0
            if manifest.kind == "checkpoint":
                for record in self.training.journal.records():
                    prepared = record["prepare"]
                    if (
                        record["attempt_id"] == request.attempt_id == manifest.attempt_id
                        and prepared["fence"] == request.fence == manifest.fence
                        and record["disk_bytes"] >= manifest.total_bytes
                    ):
                        materialized = 0
                        directory = self.training.journal.root / record["attempt_id"]
                        for path in directory.rglob("*"):
                            if path.is_symlink():
                                raise TrainingConflict("Training disk coverage is linked")
                            if path.is_file():
                                materialized += path.stat().st_size
                        for path in self.artifacts.root.glob("*/manifest.json"):
                            other = TrainingArtifactManifest.model_validate_json(path.read_bytes())
                            if other.attempt_id == request.attempt_id:
                                materialized += sum(
                                    file.stat().st_size
                                    for file in path.parent.iterdir()
                                    if file.is_file() and not file.is_symlink()
                                )
                        credit = min(
                            manifest.total_bytes, max(0, record["disk_bytes"] - materialized)
                        )
            self.reservations.hold(
                ReservationRequest(
                    idempotency_key=self._new_scope(request.command_id),
                    workflow_id=request.command_id,
                    variant_id=request.artifact_id,
                    memory_bytes=1,
                    disk_bytes=manifest.total_bytes,
                ),
                disk_path=self.artifacts.root,
                disk_floor_bytes=self.disk_floor_bytes,
                require_stop=True,
                owner_kind="artifact-import",
                disk_credit_bytes=credit,
                materialized_paths=(
                    self.artifacts.root / f".import-{request.artifact_id}",
                    self.artifacts.root / str(request.artifact_id),
                ),
            )
        return manifest


class AccountedAdapterExtractor(AdapterExtractor):
    """Use the aggregate ledger quota instead of a second physical-plus-holds quota."""

    def _hold(self, command: TrainingAdapterExtractRequest) -> None:
        held, _ = self.reservations.hold(
            self._reservation(command),
            disk_path=self.artifacts.root,
            disk_floor_bytes=self.settings.training_artifact_disk_floor_bytes,
            require_stop=True,
        )
        if held.state is not ReservationState.HELD:
            raise TrainingConflict("Extraction reservation is no longer held")
        self.reservations.bind_owner(
            held.id,
            release_check=lambda: (
                not self._staging(command).exists()
                and self.status(command.command_id).state in {"succeeded", "failed"}
            ),
            footprint_bytes=lambda: None,
        )


def hardware_sha256(settings: Settings, gpu_cores: int | None) -> str:
    if gpu_cores is None:
        raise TrainingConflict("Local GPU inventory is unavailable")
    value = {
        "node": settings.node_name,
        "memory_total_bytes": psutil.virtual_memory().total,
        "gpu_cores": gpu_cores,
        "agent_version": __version__,
    }
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def guard_measurement(
    command: TrainingMeasurementPrepare,
    engines: EngineManager,
    image: ImageProcessSupervisor,
    training: TrainingSupervisor,
    reservations: ReservationLedger,
    measurements: MeasurementSupervisor | None,
    active_request_leases: Callable[[set[uuid.UUID]], int] | None = None,
) -> None:
    engines.find_orphans()
    statuses = [s for s in engines.statuses() if s.state.value not in {"stopped", "failed"}]
    owned = {s.pid for s in statuses if s.pid is not None}
    own = [] if measurements is None else measurements.statuses()
    if any(s.attempt_id != command.prepare.attempt_id for s in own):
        raise TrainingConflict("Another measurement owns the accelerator slot")
    owned.update(s.pid for s in own if s.pid is not None and s.liveness == "running")
    if image.committed_bytes() or training.committed_bytes() or reservations.held_bytes():
        raise TrainingConflict("Other node work holds the measurement slot")
    # Access-denied process inventory is uncertainty, never proof of vacancy.
    markers = (
        "mlx_lm",
        "mlx_vlm",
        "mflux",
        "coire_node.training",
        "coire_node.image",
        "mlx.launch",
    )
    for process in psutil.process_iter():
        try:
            argv = process.cmdline()
        except psutil.NoSuchProcess:
            continue
        except psutil.AccessDenied:
            from coire_node.training.process_inventory import verified_native_system_process

            if verified_native_system_process(process):
                continue
            raise TrainingConflict("Accelerator process inventory is unresolved") from None
        if process.pid not in owned and any(marker in arg for marker in markers for arg in argv):
            raise TrainingConflict("An unowned accelerator process exists")
    if command.mode == "memory":
        if statuses:
            raise TrainingConflict("Memory measurement requires an actually isolated accelerator")
        if active_request_leases is None:
            raise TrainingConflict("Initial gateway request lease inventory is unavailable")
        count = active_request_leases(set())
        if type(count) is not int or count != 0:
            raise TrainingConflict("Initial gateway request leases are active or unresolved")
    else:
        expected = {item.instance_id: item.target for item in command.resident_targets}
        identities = command.resident_engine_ids
        if (
            set(identities) != set(expected)
            or len(set(identities.values())) != len(identities)
            or {s.engine_id: s.target for s in statuses}
            != {identities[i]: target for i, target in expected.items()}
            or any(s.state.value != "ready" for s in statuses)
        ):
            raise TrainingConflict(
                "Resident engine inventory differs from the exact measurement set"
            )
        # Core's ordinary gateway uses private engine ports; node proxy counters cannot prove
        # its active lease inventory. A shared authenticated lease snapshot is required.
        if active_request_leases is None:
            raise TrainingConflict("Initial gateway request lease inventory is unavailable")
        count = active_request_leases(set(expected))
        if type(count) is not int or count != 0:
            raise TrainingConflict("Initial gateway request leases are active or unresolved")


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
    image_workers: ImageProcessSupervisor | None = None,
    image_dispatcher: ImageNodeDispatcher | None = None,
    grants: Grants | None = None,
    reservations: ReservationLedger | None = None,
    shard_groups: ShardGroupManager | None = None,
    link_probes: LinkProbeRunner | None = None,
    benchmarks: BenchmarkRunner | None = None,
    runs: RunManager | None = None,
    training_artifacts: TrainingArtifacts | None = None,
    training_artifact_importer: ArtifactImporter | None = None,
    training_analyses: AnalysisSupervisor | None = None,
    training: TrainingSupervisor | None = None,
    training_measurements: MeasurementSupervisor | None = None,
    training_adapter_extractor: AdapterExtractor | None = None,
    training_rank_importer: RankImporter | None = None,
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
    app.state.image_workers = image_workers
    app.state.image_dispatcher = image_dispatcher
    app.state.grants = grants
    app.state.reservations = reservations
    app.state.shard_groups = shard_groups
    app.state.link_probes = link_probes
    app.state.benchmarks = benchmarks
    app.state.runs = runs
    app.state.training_artifacts = training_artifacts
    app.state.training_artifact_importer = training_artifact_importer
    app.state.training_analyses = training_analyses
    app.state.training = training
    expected_token = settings.node_token.get_secret_value()

    async def require_node_token(credentials: BearerDep, request: Request) -> None:
        presented = credentials.credentials if credentials else ""
        if not expected_token or not hmac.compare_digest(expected_token, presented):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid node token",
                headers={"WWW-Authenticate": "Bearer"},
            )
        if getattr(request.state, "training_mutation_disabled", False):
            raise HTTPException(503, "training is disabled")

    app.state.require_node_token = require_node_token
    if training_rank_importer is not None:
        from coire_node.routes.training_components import control_router as components_control
        from coire_node.routes.training_components import data_router as components_data

        app.state.training_components = training_rank_importer.components
        if listener is NetworkPath.CONTROL:
            app.state.training_rank_importer = training_rank_importer
            app.include_router(components_control, dependencies=[Depends(require_node_token)])
        elif listener is NetworkPath.DATA:
            app.include_router(components_data)
    if listener is NetworkPath.CONTROL:
        if training_measurements is not None:
            from coire_node.routes.training_measurements import attach

            attach(app, training_measurements)
        if training_adapter_extractor is not None:
            training_adapter_extractor.attach(app)
    if (training_analyses is not None or training is not None) and listener is NetworkPath.CONTROL:
        from coire_node.routes.training import router as training_router

        app.include_router(training_router, dependencies=[Depends(require_node_token)])

    if training_artifacts is not None:
        from coire_node.routes.training_artifacts import control_router, data_router

        if listener is NetworkPath.CONTROL:
            app.include_router(control_router, dependencies=[Depends(require_node_token)])
        elif listener is NetworkPath.DATA:
            app.include_router(data_router)

    @app.middleware("http")
    async def enforce_path(request: Request, call_next):  # type: ignore[no-untyped-def]
        if (
            not settings.training_enabled
            and request.method == "POST"
            and (
                request.url.path == "/node/training/adapters/extractions"
                or (
                    request.url.path.startswith("/node/training/measurements/")
                    and request.url.path.rsplit("/", 1)[-1]
                    in {"prepare", "inputs", "start", "lease", "begin"}
                )
            )
        ):
            # Authentication must run first; return the feature gate from the route dependency.
            request.state.training_mutation_disabled = True
        if listener is NetworkPath.DATA and not request.url.path.startswith(
            ("/node/export/", "/training-artifacts/", "/training-components/", "/ready")
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
                    status_value.model_dump(exclude={"path"})
                    | {
                        "path": "control",
                        "run_images_configured": bool(
                            settings.run_agent_image and settings.run_relay_image
                        ),
                        "training_capabilities": NodeTrainingCapabilities(
                            spec_versions=[1, 2], evaluation_checkpoint_ack_versions=[1]
                        ).model_dump(mode="json")
                        if training is not None
                        else None,
                    }
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
        if image_workers is not None:
            app.include_router(image_workers_routes.router, dependencies=guard)
        if image_dispatcher is not None:
            app.include_router(image_jobs_routes.router, dependencies=guard)
        app.include_router(sharding_routes.router, dependencies=guard)
        app.include_router(link_probe_routes.router, dependencies=guard)
        app.include_router(benchmark_routes.router, dependencies=guard)
        app.include_router(runs_routes.router, dependencies=guard)
        app.include_router(workspaces_routes.router, dependencies=guard)
        app.include_router(evaluations_routes.router, dependencies=guard)
        # Failover's separately scoped credential has access only to resident metadata and
        # this inference relay. It cannot use the broad node-registration credential.
        if listener in (NetworkPath.CONTROL, NodePath.MESH):
            app.include_router(failover_routes.router)

        # The data path for peer replication. Mesh listener only, and authorised by the grant
        # in the URL rather than the node bearer, because the peer does not hold that token
        # and should not (spec FR-007, research R3).
        if listener in (NodePath.MESH, NetworkPath.DATA):
            app.include_router(export_routes.router)

    if store is not None and listener is NetworkPath.DATA:
        app.include_router(export_routes.router)

    return app


async def serve(
    settings: Settings,
    collector: SupportsLatest,
    *,
    measurement_active_leases: Callable[[set[uuid.UUID]], int] | None = None,
) -> None:
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
    memory_lock = threading.RLock()
    image_workers: ImageProcessSupervisor | None = None
    reservations: ReservationLedger | None = None
    training: TrainingSupervisor | None = None
    measurements: MeasurementSupervisor | None = None
    disk_budget = TrainingDiskBudget(
        Path(settings.node_state_dir) / "training",
        memory_lock,
        settings.training_artifact_quota_bytes,
    )
    engines = EngineManager(
        settings,
        store,
        "127.0.0.1",
        memory_lock=memory_lock,
        additional_committed_bytes=lambda: (
            (image_workers.committed_bytes() if image_workers else 0)
            + (reservations.held_bytes() if reservations else 0)
            + (training.committed_bytes() if training else 0)
            + (measurements.committed_bytes() if measurements else 0)
        ),
    )
    image_workers = ImageProcessSupervisor(
        settings,
        store,
        lambda: (
            engines.committed_bytes()
            + (reservations.held_bytes() if reservations else 0)
            + (
                engines.budget_bytes()
                if (training and training.committed_bytes())
                or (measurements and measurements.committed_bytes())
                else 0
            )
        ),
        memory_lock=memory_lock,
    )
    image_journal = ImageJobJournal(settings.node_state_dir, hostname)
    image_dispatcher = ImageNodeDispatcher(image_journal, image_workers)
    reservations = ReservationLedger(
        settings,
        store,
        lambda: (
            engines.committed_bytes()
            + image_workers.committed_bytes()
            + (training.committed_bytes() if training else 0)
            + (measurements.committed_bytes() if measurements else 0)
        ),
        memory_lock=memory_lock,
        additional_held_disk_bytes=lambda path: (
            (training.held_disk_bytes(path) if training else 0)
            + (measurements.held_disk_bytes(path) if measurements else 0)
        ),
        disk_quota_bytes=settings.training_artifact_quota_bytes,
        disk_usage_bytes=disk_budget.committed_bytes,
    )
    disk_budget.ledger = reservations
    shard_groups = ShardGroupManager(settings, store)
    link_probes = LinkProbeRunner(settings)
    benchmarks = BenchmarkRunner(settings, store)
    docker = DockerAPI(settings.run_docker_socket)
    runs = RunManager(settings, docker)
    training_artifacts: TrainingArtifacts | None = None
    training_artifact_importer: ArtifactImporter | None = None
    training_analyses: AnalysisSupervisor | None = None
    extractor: AdapterExtractor | None = None
    rank_importer: RankImporter | None = None
    lease_reader: TrainingLeaseSnapshotReader | None = None
    if not settings.legacy_network_mode and hostname in {"coire-edge-a", "coire-edge-b"}:
        journal = TrainingJournal(
            Path(settings.node_state_dir) / "training" / "attempts",
            node=hostname,
            admission_lock=memory_lock,
        )
        training = TrainingSupervisor(
            journal,
            otlp_endpoint=settings.otlp_endpoint,
            interpreter=Path(sys.executable),
            store_root=store.root,
            artifact_root=Path(settings.node_state_dir) / "training" / "artifacts",
            memory_available=lambda: (
                max(
                    0,
                    engines.budget_bytes()
                    - engines.committed_bytes()
                    - reservations.held_bytes()
                    - (measurements.committed_bytes() if measurements else 0),
                )
                if not image_workers.committed_bytes()
                and not (measurements and measurements.committed_bytes())
                else 0
            ),
            disk_available=lambda: min(
                disk_budget.available_for(journal),
                max(
                    0,
                    shutil.disk_usage(journal.root).free
                    - reservations.held_disk_bytes(journal.root, include_external=False)
                    - (measurements.held_disk_bytes(journal.root) if measurements else 0),
                    # The measurement envelope is separately journaled on this filesystem.
                ),
            ),
            disk_floor=settings.training_artifact_disk_floor_bytes,
            jaccl_hostfile=Path(settings.sharding_jaccl_hostfile),
        )
        # Re-adopt by immutable argv/PID/create-time before exposing the first health response.
        training.statuses()
        rank_importer = RankImporter(
            training.components,
            Path(settings.node_state_dir) / "training" / "component-imports",
            port=settings.node_data_listen_port,
            max_bytes=settings.training_artifact_quota_bytes,
            disk_floor_bytes=settings.training_artifact_disk_floor_bytes,
            reservations=reservations,
        )
        if measurement_active_leases is None:
            lease_reader = TrainingLeaseSnapshotReader(settings, node=hostname)
            measurement_active_leases = lease_reader
        disk_budget.journals.append(journal)
        from coire_node.register import read_gpu_cores

        gpu_cores = await asyncio.to_thread(read_gpu_cores)
        measurement_journal = TrainingJournal(
            Path(settings.node_state_dir) / "training" / "measurements",
            node=hostname,
            admission_lock=memory_lock,
        )
        measurements = MeasurementSupervisor(
            measurement_journal,
            otlp_endpoint=settings.otlp_endpoint,
            interpreter=Path(sys.executable),
            store_root=store.root,
            artifact_root=Path(settings.node_state_dir) / "training" / "probe-artifacts",
            accelerator_guard=lambda command: guard_measurement(
                command,
                engines,
                image_workers,
                training,
                reservations,
                measurements,
                measurement_active_leases,
            ),
            hardware_sha256=lambda: hardware_sha256(settings, gpu_cores),
            memory_available=lambda: max(
                0,
                engines.budget_bytes()
                - engines.committed_bytes()
                - image_workers.committed_bytes()
                - reservations.held_bytes()
                - training.committed_bytes(),
            ),
            disk_available=lambda: min(
                disk_budget.available_for(measurement_journal),
                max(
                    0,
                    shutil.disk_usage(measurement_journal.root).free
                    - reservations.held_disk_bytes(measurement_journal.root, include_external=False)
                    - training.held_disk_bytes(measurement_journal.root),
                ),
            ),
        )
        disk_budget.journals.append(measurement_journal)
        measurements.statuses()
    if training is not None:
        from coire_node.training.artifacts import TrainingArtifacts

        artifact_root = Path(settings.node_state_dir) / "training" / "artifacts"
        training_artifacts = TrainingArtifacts(
            artifact_root, node_name=hostname, admission_lock=memory_lock
        )
        training_artifact_importer = AccountedArtifactImporter(
            training_artifacts,
            artifact_root.parent / "imports",
            port=settings.node_data_listen_port,
            max_bytes=settings.training_artifact_quota_bytes,
            disk_floor_bytes=settings.training_artifact_disk_floor_bytes,
            reservations=reservations,
            training=training,
        )
        extractor = AccountedAdapterExtractor(training_artifacts, reservations, settings, store)
        training_analyses = AnalysisSupervisor(settings, reservations)

        def artifact_referenced(identity: uuid.UUID) -> bool:
            # Fail closed on any live/uncertain trainer. Core decides which old
            # checkpoint is eligible; this local guard independently prevents
            # removal while native ownership or transfer/copy work is unresolved.
            assert training is not None
            if training.references_artifact(identity):
                return True
            with training.components.lock:
                if any(
                    scope.artifact_id == identity and scope.expires_at > datetime.now(UTC)
                    for scope, _, _ in training.components.grants.values()
                ):
                    return True
            if rank_importer is not None:
                for path in rank_importer.root.glob("*.json"):
                    component_status = rank_importer.status(uuid.UUID(path.stem))
                    if (
                        component_status.component.artifact_id == identity
                        and component_status.state
                        not in {
                            "verified",
                            "failed",
                            "cancelled",
                        }
                    ):
                        return True
            if any(
                engine.target is not None
                and engine.target.adapter_id == identity
                and engine.state is not EngineState.STOPPED
                for engine in engines.statuses()
            ):
                return True
            assert training_artifact_importer is not None and extractor is not None
            for path in training_artifact_importer.root.glob("*.json"):
                status = training_artifact_importer.status(uuid.UUID(path.stem))
                if status.artifact_id == identity and status.state not in {
                    "verified",
                    "failed",
                    "cancelled",
                }:
                    return True
            for path in extractor.root.glob("*.json"):
                extraction_status = extractor.status(uuid.UUID(path.stem))
                if identity in {
                    extraction_status.checkpoint_id,
                    extraction_status.adapter_id,
                } and extraction_status.state in {
                    "queued",
                    "running",
                }:
                    return True
            return False

        training_artifacts.referenced = artifact_referenced

    # Re-own engines that outlived the previous agent process, and re-attach to jobs that were
    # running. Both happen before the listeners bind, so the first /node/health after a restart
    # already tells the truth (spec FR-015, edge case 3).
    adopted = engines.adopt_from_state()
    try:
        adopted_image = image_workers.adopt_from_state()
    except ImageProcessUnavailable:
        logger.warning("image worker state uncertain; retaining memory budget")
    else:
        if adopted_image is not None:
            logger.info("adopted image worker %s after restart", adopted_image.instance_id)
    if adopted:
        logger.info("adopted %d running engine(s) after restart", len(adopted))
    resumed = jobs.resume_all()
    if resumed:
        logger.info("resumed %d acquisition job(s)", resumed)
    engines.start_health_loop()
    if hasattr(collector, "attach"):
        collector.attach(
            store=store,
            jobs=jobs,
            engines=engines,
            image_workers=image_workers,
            reservations=reservations,
            training=training,
            measurements=measurements,
        )
        if hasattr(collector, "sample"):
            await asyncio.to_thread(collector.sample)

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
                        image_workers=image_workers,
                        image_dispatcher=image_dispatcher,
                        grants=grants,
                        reservations=reservations,
                        shard_groups=shard_groups,
                        link_probes=link_probes,
                        benchmarks=benchmarks,
                        runs=runs,
                        training_artifacts=training_artifacts,
                        training_artifact_importer=training_artifact_importer,
                        training_analyses=training_analyses,
                        training=training,
                        training_measurements=measurements,
                        training_adapter_extractor=extractor,
                        training_rank_importer=rank_importer,
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
                            image_workers=image_workers,
                            image_dispatcher=image_dispatcher,
                            grants=grants,
                            reservations=reservations,
                            shard_groups=shard_groups,
                            link_probes=link_probes,
                            benchmarks=benchmarks,
                            training_artifacts=training_artifacts,
                            training_artifact_importer=training_artifact_importer,
                            training_rank_importer=rank_importer,
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
                        image_workers=image_workers,
                        image_dispatcher=image_dispatcher,
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
                        image_workers=image_workers,
                        image_dispatcher=image_dispatcher,
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
        if rank_importer is not None:
            await rank_importer.aclose()
        if lease_reader is not None:
            await lease_reader.aclose()
        if training_artifact_importer is not None:
            await training_artifact_importer.aclose()
        if measurements is not None:
            await measurements.aclose()
        if training is not None:
            await training.aclose()
        await docker.close()
        engines.shutdown()
        jobs.shutdown()
        raise RuntimeError("no address to bind: neither a mesh nor an egress address resolved")

    if lease_reader is not None:
        await lease_reader.start()

    from coire_node.failover.poller import build_controller

    failover_controller = build_controller(settings, docker)
    if failover_controller is not None:
        await failover_controller.start()
    image_sweep_stop = asyncio.Event()
    image_sweep_task = asyncio.create_task(
        _sweep_terminal_image_scratch(image_dispatcher, image_sweep_stop),
        name="image-terminal-scratch-maintenance",
    )
    training_watchdog = (
        asyncio.create_task(_watch_native(training), name="training-watchdog") if training else None
    )
    measurement_watchdog = (
        asyncio.create_task(_watch_native(measurements), name="training-measurement-watchdog")
        if measurements
        else None
    )
    try:
        await asyncio.gather(*(s.serve() for s in servers))
    finally:
        if lease_reader is not None:
            await lease_reader.aclose()
        if rank_importer is not None:
            await rank_importer.aclose()
        if measurement_watchdog is not None:
            measurement_watchdog.cancel()
            with suppress(asyncio.CancelledError):
                await measurement_watchdog
        if training_artifact_importer is not None:
            await training_artifact_importer.aclose()
        if measurements is not None:
            await measurements.aclose()
        if training_watchdog is not None:
            training_watchdog.cancel()
            with suppress(asyncio.CancelledError):
                await training_watchdog
        if training is not None:
            await training.aclose()
        image_sweep_stop.set()
        await image_sweep_task
        if failover_controller is not None:
            await failover_controller.stop()
        await engines_routes.close_engine_client()
        await docker.close()
        # Engines are deliberately left running: the agent is restartable and they are not
        # (spec FR-015).
        engines.shutdown()
        jobs.shutdown()


async def _sweep_terminal_image_scratch(
    dispatcher: ImageNodeDispatcher, stop: asyncio.Event
) -> None:
    """Replay a bounded journal page each pass, including after node restart."""
    cursor: str | None = None
    while not stop.is_set():
        try:
            cursor = await dispatcher.sweep_terminal_scratch(after_job=cursor)
        except Exception as exc:
            logger.error("image terminal scratch sweep failed error_type=%s", type(exc).__name__)
            cursor = None
        try:
            await asyncio.wait_for(stop.wait(), timeout=30.0)
        except TimeoutError:
            continue


async def _watch_native(owner: TrainingSupervisor) -> None:
    while True:
        try:
            await owner.watchdog()
        except Exception as exc:
            logger.error(
                "native watchdog observation failed", extra={"error_type": type(exc).__name__}
            )
        await asyncio.sleep(0.5)
