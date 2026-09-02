"""Typed client for the node agent API.

One method per operation in `specs/001-model-registry-node-agent/contracts/node-api.yaml`,
each returning a `coire-core` model. Routes and the reconciler never see an httpx response, so
a contract change surfaces as a validation error here rather than as a `KeyError` three layers
away.

Every call goes to the node's declared control DNS name. There is no data-fabric fallback.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from enum import StrEnum
from types import TracebackType
from typing import Any

import httpx

from coire_core.models.acquisition import Reservation, ReservationRequest, VariantRecipe
from coire_core.models.engine import EngineStatus, ReconcileRequest, ReconcileResult
from coire_core.models.jobs import ChecksumManifest, JobStatus, RepoInspection
from coire_core.models.link import StudioDataLinkStatus
from coire_core.models.node import NodeStatus, NodeStatusV2
from coire_core.models.runs import (
    RunCollectedResult,
    RunContainerCreate,
    RunContainerObservation,
    RunContainerStatus,
    RunLogChunk,
    RunReconcileRequest,
    RunReconcileResult,
)
from coire_core.models.sharding import (
    BenchmarkCommand,
    BenchmarkMeasurement,
    LinkObservation,
    LinkProbeCommand,
    ShardCapabilityRequest,
    ShardCapabilityResult,
    ShardGroupCommand,
    ShardGroupStatus,
)
from coire_core.net import ControlClient, FabricUnreachable
from coire_core.settings import Settings

logger = logging.getLogger(__name__)


class NodeErrorKind(StrEnum):
    UNREACHABLE = "unreachable"
    UNAUTHORIZED = "unauthorized"
    NOT_FOUND = "not_found"
    GATED = "gated"
    CONFLICT = "conflict"
    NO_SPACE = "no_space"
    BUDGET = "budget"
    UNAVAILABLE = "unavailable"
    PROTOCOL = "protocol"
    SERVER = "server"


class NodeError(RuntimeError):
    """A node refused or could not be reached.

    Carries a `kind` because the reconciler's response differs sharply by cause: `unreachable`
    means wait and retry, `not_found` means the node lost a copy, `gated` is a message for the
    admin, and `conflict` usually means another job already owns the slug.
    """

    def __init__(
        self,
        kind: NodeErrorKind,
        node: str,
        *,
        status: int | None = None,
        detail: str = "",
        body: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(f"{node}: {kind.value}{f' (HTTP {status})' if status else ''} {detail}")
        self.kind = kind
        self.node = node
        self.status = status
        self.detail = detail
        self.body = body or {}

    @property
    def retryable(self) -> bool:
        """Whether waiting could plausibly help. A refusal on the merits could not."""
        return self.kind in (
            NodeErrorKind.UNREACHABLE,
            NodeErrorKind.UNAVAILABLE,
            NodeErrorKind.SERVER,
        )


_STATUS_KINDS = {
    401: NodeErrorKind.UNAUTHORIZED,
    403: NodeErrorKind.UNAUTHORIZED,
    404: NodeErrorKind.NOT_FOUND,
    409: NodeErrorKind.CONFLICT,
    423: NodeErrorKind.GATED,
    503: NodeErrorKind.UNAVAILABLE,
    507: NodeErrorKind.NO_SPACE,
}


class NodeClient:
    """Talks to node agents. One instance per reconciler pass or request."""

    def __init__(self, settings: Settings, *, timeout: float = 30.0) -> None:
        self._settings = settings
        self._control = ControlClient(timeout=timeout)

    async def __aenter__(self) -> NodeClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._control.aclose()

    # -- plumbing ----------------------------------------------------------
    def _headers(self, node: str) -> dict[str, str]:
        token = self._settings.node_token_map.get(node, "")
        if not token:
            logger.error("no token for node %s; the request will be refused", node)
        return {"Authorization": f"Bearer {token}"}

    async def _call(
        self,
        method: str,
        node: str,
        path: str,
        *,
        json: Any = None,
        expect: tuple[int, ...] = (200, 202, 204),
    ) -> tuple[int, dict[str, Any]]:
        try:
            resp = await self._control.request(
                method,
                node,
                path,
                port=self._settings.node_listen_port,
                headers=self._headers(node),
                json=json,
            )
        except (httpx.HTTPError, FabricUnreachable) as exc:
            raise NodeError(NodeErrorKind.UNREACHABLE, node, detail=str(exc)) from exc

        body: dict[str, Any] = {}
        if resp.content:
            try:
                parsed = resp.json()
                body = parsed if isinstance(parsed, dict) else {"items": parsed}
            except ValueError:
                body = {"raw": resp.text[:500]}

        if resp.status_code not in expect:
            kind = _STATUS_KINDS.get(
                resp.status_code,
                NodeErrorKind.SERVER if resp.status_code >= 500 else NodeErrorKind.PROTOCOL,
            )
            raise NodeError(
                kind,
                node,
                status=resp.status_code,
                detail=str(body.get("detail") or body.get("error") or "")[:300],
                body=body,
            )
        return resp.status_code, body

    # -- health ------------------------------------------------------------
    async def health(self, node: str) -> NodeStatus | NodeStatusV2:
        _, body = await self._call("GET", node, "/node/health", expect=(200,))
        if body.get("path") == "control":
            return NodeStatusV2.model_validate(body)
        return NodeStatus.model_validate(body)

    async def data_link_status(self, node: str) -> StudioDataLinkStatus:
        _, body = await self._call("GET", node, "/node/data-link", expect=(200,))
        return StudioDataLinkStatus.model_validate(body)

    async def run_link_probe(self, node: str, command: LinkProbeCommand) -> LinkObservation:
        _, body = await self._call(
            "POST",
            node,
            "/node/link-probes",
            json=command.model_dump(mode="json"),
            expect=(200,),
        )
        return LinkObservation.model_validate(body)

    async def run_benchmark(self, node: str, command: BenchmarkCommand) -> BenchmarkMeasurement:
        _, body = await self._call(
            "POST",
            node,
            "/node/benchmarks",
            json=command.model_dump(mode="json"),
            expect=(200,),
        )
        return BenchmarkMeasurement.model_validate(body)

    async def prepare_shard_group(self, node: str, command: ShardGroupCommand) -> ShardGroupStatus:
        _, body = await self._call(
            "POST",
            node,
            "/node/shard-groups",
            json=command.model_dump(mode="json"),
            expect=(200, 202),
        )
        return ShardGroupStatus.model_validate(body)

    async def shard_capability(
        self, node: str, request: ShardCapabilityRequest
    ) -> ShardCapabilityResult:
        _, body = await self._call(
            "POST",
            node,
            "/node/shard-groups/capabilities",
            json=request.model_dump(mode="json"),
            expect=(200,),
        )
        return ShardCapabilityResult.model_validate(body)

    async def shard_group(self, node: str, group_id: uuid.UUID) -> ShardGroupStatus:
        _, body = await self._call("GET", node, f"/node/shard-groups/{group_id}", expect=(200,))
        return ShardGroupStatus.model_validate(body)

    async def stop_shard_group(self, node: str, group_id: uuid.UUID) -> ShardGroupStatus:
        _, body = await self._call(
            "DELETE", node, f"/node/shard-groups/{group_id}", expect=(200, 202)
        )
        return ShardGroupStatus.model_validate(body)

    async def mark_shard_group_ready(self, node: str, group_id: uuid.UUID) -> ShardGroupStatus:
        _, body = await self._call(
            "POST", node, f"/node/shard-groups/{group_id}/ready", expect=(200,)
        )
        return ShardGroupStatus.model_validate(body)

    # -- repositories and copies -------------------------------------------
    async def inspect(self, node: str, repo_id: str, revision: str = "main") -> RepoInspection:
        _, body = await self._call(
            "POST",
            node,
            "/node/models/inspect",
            json={"repo_id": repo_id, "revision": revision},
            expect=(200,),
        )
        return RepoInspection.model_validate(body)

    async def list_models(self, node: str) -> list[dict[str, Any]]:
        _, body = await self._call("GET", node, "/node/models", expect=(200,))
        items = body.get("items", [])
        return list(items) if isinstance(items, list) else []

    async def delete_model(self, node: str, slug: str) -> None:
        await self._call("DELETE", node, f"/node/models/{slug}", expect=(204, 404))

    async def grant_export(self, node: str, slug: str, grant: str, expires_at: datetime) -> None:
        await self._call(
            "POST",
            node,
            f"/node/models/{slug}/export",
            json={"grant": grant, "expires_at": expires_at.astimezone(UTC).isoformat()},
            expect=(204,),
        )

    async def revoke_export(self, node: str, slug: str) -> None:
        await self._call("DELETE", node, f"/node/models/{slug}/export", expect=(204, 404))

    # -- jobs --------------------------------------------------------------
    async def start_pull(
        self,
        node: str,
        *,
        job_id: uuid.UUID,
        repo_id: str,
        slug: str,
        revision: str = "main",
        expected_total_bytes: int | None = None,
    ) -> JobStatus:
        payload: dict[str, Any] = {
            "job_id": str(job_id),
            "repo_id": repo_id,
            "slug": slug,
            "revision": revision,
        }
        if expected_total_bytes is not None:
            payload["expected_total_bytes"] = expected_total_bytes
        _, body = await self._call("POST", node, "/node/jobs/pull", json=payload, expect=(200, 202))
        return JobStatus.model_validate(body)

    async def start_import(
        self,
        node: str,
        *,
        job_id: uuid.UUID,
        slug: str,
        source_node: str,
        grant: str,
        manifest: ChecksumManifest,
    ) -> JobStatus:
        _, body = await self._call(
            "POST",
            node,
            "/node/jobs/import",
            json={
                "job_id": str(job_id),
                "slug": slug,
                "source_node": source_node,
                "grant": grant,
                "manifest": manifest.model_dump(mode="json"),
            },
            expect=(200, 202),
        )
        return JobStatus.model_validate(body)

    async def start_verify(
        self,
        node: str,
        *,
        job_id: uuid.UUID,
        slug: str,
        manifest: ChecksumManifest | None = None,
    ) -> JobStatus:
        payload: dict[str, Any] = {"job_id": str(job_id), "slug": slug}
        if manifest is not None:
            payload["manifest"] = manifest.model_dump(mode="json")
        _, body = await self._call(
            "POST", node, "/node/jobs/verify", json=payload, expect=(200, 202)
        )
        return JobStatus.model_validate(body)

    async def hold_reservation(self, node: str, request: ReservationRequest) -> Reservation:
        _, body = await self._call(
            "POST",
            node,
            "/node/jobs/reservations",
            json=request.model_dump(mode="json"),
            expect=(200, 201),
        )
        return Reservation.model_validate(body)

    async def release_reservation(self, node: str, reservation_id: uuid.UUID) -> None:
        await self._call(
            "DELETE",
            node,
            f"/node/jobs/reservations/{reservation_id}",
            expect=(204,),
        )

    async def start_convert(
        self,
        node: str,
        *,
        job_id: uuid.UUID,
        repo_id: str,
        revision: str,
        source_slug: str,
        target_slug: str,
        reservation_id: uuid.UUID,
        recipe: VariantRecipe,
        dequantize: bool = False,
        expected_total_bytes: int | None = None,
    ) -> JobStatus:
        payload: dict[str, Any] = {
            "job_id": str(job_id),
            "repo_id": repo_id,
            "revision": revision,
            "source_slug": source_slug,
            "target_slug": target_slug,
            "reservation_id": str(reservation_id),
            "recipe": recipe.model_dump(mode="json"),
            "dequantize": dequantize,
        }
        if expected_total_bytes is not None:
            payload["expected_total_bytes"] = expected_total_bytes
        _, body = await self._call(
            "POST", node, "/node/jobs/convert", json=payload, expect=(200, 202)
        )
        return JobStatus.model_validate(body)

    async def start_validate(
        self,
        node: str,
        *,
        job_id: uuid.UUID,
        slug: str,
        tolerance: float,
        validator_version: str,
        chat_template_present: bool,
        reference_perplexity: float | None = None,
        reference_variant_id: uuid.UUID | None = None,
    ) -> JobStatus:
        _, body = await self._call(
            "POST",
            node,
            "/node/jobs/validate",
            json={
                "job_id": str(job_id),
                "slug": slug,
                "tolerance": tolerance,
                "validator_version": validator_version,
                "chat_template_present": chat_template_present,
                "reference_perplexity": reference_perplexity,
                "reference_variant_id": str(reference_variant_id) if reference_variant_id else None,
            },
            expect=(200, 202),
        )
        return JobStatus.model_validate(body)

    async def start_cleanup(self, node: str, *, job_id: uuid.UUID, slug: str) -> JobStatus:
        _, body = await self._call(
            "POST",
            node,
            "/node/jobs/cleanup",
            json={"job_id": str(job_id), "slug": slug},
            expect=(200, 202),
        )
        return JobStatus.model_validate(body)

    async def get_job(self, node: str, job_id: uuid.UUID) -> JobStatus:
        _, body = await self._call("GET", node, f"/node/jobs/{job_id}", expect=(200,))
        return JobStatus.model_validate(body)

    async def cancel_job(self, node: str, job_id: uuid.UUID) -> None:
        await self._call("DELETE", node, f"/node/jobs/{job_id}", expect=(202, 404))

    # -- engines -----------------------------------------------------------
    async def list_engines(self, node: str) -> list[EngineStatus]:
        _, body = await self._call("GET", node, "/node/engines", expect=(200,))
        return [EngineStatus.model_validate(e) for e in body.get("items", [])]

    async def start_engine(
        self,
        node: str,
        *,
        engine_id: uuid.UUID,
        slug: str,
        estimate_bytes: int,
        chat_template: str | None = None,
    ) -> tuple[bool, EngineStatus]:
        """Returns `(already_running, status)`.

        A second load of the same model on the same node answers 200 with the existing engine
        rather than starting a second one (spec FR-019); the caller needs to know which
        happened so it does not create a duplicate row.
        """
        status, body = await self._call(
            "POST",
            node,
            "/node/engines",
            json={
                "engine_id": str(engine_id),
                "slug": slug,
                "estimate_bytes": estimate_bytes,
                "chat_template": chat_template,
            },
            expect=(200, 202),
        )
        return status == 200, EngineStatus.model_validate(body)

    async def get_engine(self, node: str, engine_id: uuid.UUID) -> EngineStatus:
        _, body = await self._call("GET", node, f"/node/engines/{engine_id}", expect=(200,))
        return EngineStatus.model_validate(body)

    async def stop_engine(self, node: str, engine_id: uuid.UUID) -> EngineStatus | None:
        status, body = await self._call(
            "DELETE", node, f"/node/engines/{engine_id}", expect=(202, 404)
        )
        return EngineStatus.model_validate(body) if status == 202 else None

    async def reconcile(self, node: str, request: ReconcileRequest) -> ReconcileResult:
        _, body = await self._call(
            "POST",
            node,
            "/node/engines/reconcile",
            json=request.model_dump(mode="json"),
            expect=(200,),
        )
        return ReconcileResult.model_validate(body)

    # -- ephemeral runs ----------------------------------------------------
    async def create_run(self, node: str, command: RunContainerCreate) -> RunContainerStatus:
        _, body = await self._call(
            "POST",
            node,
            "/node/runs",
            json=command.model_dump(mode="json"),
            expect=(200, 201),
        )
        return RunContainerStatus.model_validate(body)

    async def start_run(self, node: str, run_id: uuid.UUID) -> RunContainerStatus:
        _, body = await self._call("POST", node, f"/node/runs/{run_id}/start", expect=(200,))
        return RunContainerStatus.model_validate(body)

    async def run_logs(self, node: str, run_id: uuid.UUID, *, offset: int = 0) -> list[RunLogChunk]:
        _, body = await self._call(
            "GET", node, f"/node/runs/{run_id}/logs?offset={offset}", expect=(200,)
        )
        return [RunLogChunk.model_validate(item) for item in body.get("items", [])]

    async def wait_run(self, node: str, run_id: uuid.UUID) -> RunContainerStatus:
        _, body = await self._call("POST", node, f"/node/runs/{run_id}/wait", expect=(200,))
        return RunContainerStatus.model_validate(body)

    async def collect_run(self, node: str, run_id: uuid.UUID) -> RunCollectedResult:
        _, body = await self._call("GET", node, f"/node/runs/{run_id}/result", expect=(200,))
        return RunCollectedResult.model_validate(body)

    async def remove_run(self, node: str, run_id: uuid.UUID, *, kill: bool = False) -> None:
        suffix = "?kill=true" if kill else ""
        await self._call("DELETE", node, f"/node/runs/{run_id}{suffix}", expect=(204, 404))

    async def list_runs(self, node: str) -> list[RunContainerObservation]:
        _, body = await self._call("GET", node, "/node/runs", expect=(200,))
        return [RunContainerObservation.model_validate(item) for item in body.get("items", [])]

    async def reconcile_runs(self, node: str, request: RunReconcileRequest) -> RunReconcileResult:
        _, body = await self._call(
            "POST",
            node,
            "/node/runs/reconcile",
            json=request.model_dump(mode="json"),
            expect=(200,),
        )
        return RunReconcileResult.model_validate(body)
