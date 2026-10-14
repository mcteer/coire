"""Persistent maintenance epochs and strict inventory; deadlines never imply safety."""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID

if TYPE_CHECKING:
    from coire_node.engines import EngineManager
    from coire_node.image_runtime.supervisor import ImageProcessSupervisor

from fastapi import Request
from opentelemetry import metrics, trace

from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrades import (
    NODE_UPGRADE_OWNERS,
    NodeMaintenanceAck,
    NodeMaintenanceCommand,
    NodeUpgradeInventory,
    StudioName,
    UpgradeMaintenance,
    UpgradeOwnerInventory,
)
from coire_core.upgrade_journal import PrivateUpgradeStore

logger = logging.getLogger(__name__)
tracer = trace.get_tracer("coire.node.upgrades")
meter = metrics.get_meter("coire.node.upgrades")
fence_state = meter.create_gauge(
    "coire_upgrade_node_maintenance", description="Persisted Studio maintenance fence"
)
admission_refusals = meter.create_counter("coire_upgrade_admission_refusals_total")
inventory_errors = meter.create_counter("coire_upgrade_inventory_errors_total")

REQUIRED_OWNERS = NODE_UPGRADE_OWNERS


class NodeMaintenance:
    def __init__(
        self, root: Path, node: StudioName, *, lock: threading.RLock | None = None
    ) -> None:
        self.store = PrivateUpgradeStore(root)
        self.node = node
        self.lock = lock or threading.RLock()
        self._admitting = 0
        self._owners: dict[str, Callable[[], UpgradeOwnerInventory]] = {}
        self._async_owners: dict[str, Callable[[], Awaitable[UpgradeOwnerInventory]]] = {}
        current = self.store.read(
            "maintenance.json", UpgradeMaintenance
        )  # corruption is never idle
        fence_state.set(
            int(current is not None and current.state != "released"), {"node": self.node}
        )

    def current(self) -> UpgradeMaintenance | None:
        return self.store.read("maintenance.json", UpgradeMaintenance)

    def blocked(self) -> bool:
        current = self.current()
        return current is not None and current.state != "released"

    def check(self) -> None:
        with self.lock:
            from coire_node.core_smoke import authorize_current_smoke

            if authorize_current_smoke(self):
                return
            if self.blocked():
                admission_refusals.add(1, {"node": self.node})
                raise UpgradeUnavailable("Node is draining for an upgrade; retry after maintenance")

    @contextmanager
    def admission(self) -> Iterator[None]:
        with self.lock:
            if self.blocked():
                admission_refusals.add(1, {"node": self.node})
                raise UpgradeUnavailable("Node is draining for an upgrade; retry after maintenance")
            self._admitting += 1
        try:
            yield
        finally:
            with self.lock:
                self._admitting -= 1

    def apply(
        self, command: NodeMaintenanceCommand, *, allow_existing_work: bool = False
    ) -> NodeMaintenanceAck:
        if allow_existing_work and not (
            command.action == "release"
            and command.source_unchanged
            and command.terminal_verified
            and command.source_runtime_sha256 is not None
        ):
            raise UpgradeConflict("Existing work requires verified unchanged-source closure")
        requested = command.maintenance
        if self.node not in requested.targets and "cluster" not in requested.targets:
            raise UpgradeConflict("Maintenance target differs")
        with (
            tracer.start_as_current_span("coire.node.upgrade_maintenance"),
            self.lock,
            self.store.ownership("maintenance.lock"),
        ):
            previous = self.current()
            if previous is not None:
                if requested.generation == previous.generation and (
                    requested.targets != previous.targets
                    or requested.drain_deadline_at != previous.drain_deadline_at
                    or requested.initiator_user_id != previous.initiator_user_id
                ):
                    raise UpgradeConflict("Maintenance epoch authority differs")
                if requested.generation < previous.generation:
                    raise UpgradeConflict("Maintenance owner is stale")
                if (
                    requested.generation == previous.generation
                    and requested.job_id != previous.job_id
                ):
                    raise UpgradeConflict("Maintenance generation has another operation")
                if previous.state != "released" and requested.job_id != previous.job_id:
                    raise UpgradeConflict("Previous maintenance retains ownership")
                if (
                    previous.state == "released"
                    and requested.generation == previous.generation
                    and command.action != "release"
                ):
                    raise UpgradeConflict("Released maintenance cannot be replayed as active")
            if command.action == "release":
                if previous is None or (previous.job_id, previous.generation) != (
                    requested.job_id,
                    requested.generation,
                ):
                    raise UpgradeConflict("Maintenance release has no matching owner")
                if previous.state == "released":
                    return NodeMaintenanceAck(
                        job_id=previous.job_id,
                        generation=previous.generation,
                        node=self.node,
                        applied_at=datetime.now(UTC),
                        released=True,
                    )
                if self._admitting and not allow_existing_work:
                    raise UpgradeConflict("Admitted requests still require reconciliation")
                value = requested.model_copy(
                    update={"state": "released", "updated_at": datetime.now(UTC)}
                )
            else:
                if requested.state == "released":
                    raise UpgradeConflict("Only terminal reconciliation can release maintenance")
                value = requested
            self.store.write("maintenance.json", value)
            fence_state.set(int(value.state != "released"), {"node": self.node})
            logger.info(
                "upgrade maintenance persisted",
                extra={
                    "job_id": value.job_id,
                    "generation": value.generation,
                    "node": self.node,
                    "state": value.state,
                },
            )
            return NodeMaintenanceAck(
                job_id=value.job_id,
                generation=value.generation,
                node=self.node,
                applied_at=datetime.now(UTC),
                released=value.state == "released",
            )

    @contextmanager
    def transfer_continuation(self, authorize: Callable[[], None]) -> Iterator[None]:
        """Continue a pre-issued transfer grant; its source inventory retains drain work."""
        with self.lock:
            authorize()
            self._admitting += 1
        try:
            yield
        finally:
            with self.lock:
                self._admitting -= 1

    def register(self, owner: str, collect: Callable[[], UpgradeOwnerInventory]) -> None:
        with self.lock:
            if (
                owner not in REQUIRED_OWNERS
                or owner == "requests"
                or owner in self._owners
                or owner in self._async_owners
            ):
                raise ValueError("Unknown or duplicate strict inventory owner")
            self._owners[owner] = collect

    def register_async(
        self, owner: str, collect: Callable[[], Awaitable[UpgradeOwnerInventory]]
    ) -> None:
        with self.lock:
            if (
                owner not in REQUIRED_OWNERS
                or owner == "requests"
                or owner in self._owners
                or owner in self._async_owners
            ):
                raise ValueError("Unknown or duplicate strict inventory owner")
            self._async_owners[owner] = collect

    async def inventory_async(
        self, job_id: str, generation: int, runtime_sha256: str
    ) -> NodeUpgradeInventory:
        snapshot = self.inventory(job_id, generation, runtime_sha256)
        owners = {item.owner: item for item in snapshot.owners}
        for owner, collect in self._async_owners.items():
            try:
                result = await collect()
                if result.owner != owner:
                    raise UpgradeUnavailable()
                owners[owner] = result
            except Exception:
                inventory_errors.add(1, {"node": self.node, "owner": owner})
                owners[owner] = UpgradeOwnerInventory(
                    owner=owner, status="unknown", reason="owner_inventory_unavailable"
                )
        # Revalidate authority after external I/O. A released/replaced fence invalidates
        # the entire observation, even if each individual collector succeeded.
        current = self.current()
        if (
            current is None
            or (current.job_id, current.generation) != (job_id, generation)
            or current.state == "released"
        ):
            raise UpgradeConflict("Inventory authority changed during collection")
        return snapshot.model_copy(
            update={
                "owners": tuple(owners[key] for key in sorted(owners)),
                "observed_at": datetime.now(UTC),
            }
        )

    async def drain_idle_engines(
        self,
        job_id: str,
        generation: int,
        runtime_sha256: str,
        engines: EngineManager,
        images: ImageProcessSupervisor | None = None,
    ) -> tuple[UUID, ...]:
        """Unload residents only after a complete, fenced source-owner observation."""
        observed = await self.inventory_async(job_id, generation, runtime_sha256)

        def drain() -> tuple[UUID, ...]:
            with self.lock:
                # Recollect synchronous owners while holding the same lock as admissions
                # and EngineManager. Async owners cannot admit fresh work under this fence.
                fresh = self.inventory(job_id, generation, runtime_sha256)
                asynchronous = {owner.owner: owner for owner in observed.owners}
                owners = tuple(
                    asynchronous[owner.owner] if owner.owner in self._async_owners else owner
                    for owner in fresh.owners
                )
                if any(
                    owner.status != "complete"
                    or (
                        owner.owner not in ({"engines", "images"} if images else {"engines"})
                        and any(e.state != "standing" for e in owner.entries)
                    )
                    or (
                        owner.owner == "images"
                        and images is not None
                        and any(
                            e.kind not in {"image-worker", "native-process"} for e in owner.entries
                        )
                    )
                    for owner in owners
                ):
                    return ()
                if engines._lock is not self.lock:
                    raise UpgradeConflict("Engine drain requires the shared admission lock")
                stopped = engines._idle_resident_identities()
                if stopped is None:
                    return ()
                image_identity = None
                if images is not None:
                    from coire_node.image_runtime.supervisor import ImageProcessUnavailable

                    if images._lock is not self.lock:
                        raise UpgradeConflict("Image drain requires the shared admission lock")
                    try:
                        image_identity = images._idle_resident_identity()
                    except ImageProcessUnavailable:
                        return ()
                with tracer.start_as_current_span("coire.node.upgrade_idle_engine_drain"):
                    for identity in stopped:
                        engines.stop(identity)
                    if image_identity is not None and images is not None:
                        from coire_core.models.image_worker import ImageWorkerUnloadRequest

                        images.stop(
                            ImageWorkerUnloadRequest(
                                instance_id=image_identity,
                                reason="shutdown",
                                requested_at=datetime.now(UTC),
                            )
                        )
                    return stopped

        return await asyncio.to_thread(drain)

    def inventory(self, job_id: str, generation: int, runtime_sha256: str) -> NodeUpgradeInventory:
        with self.lock:
            current = self.current()
            if (
                current is None
                or (current.job_id, current.generation) != (job_id, generation)
                or current.state == "released"
            ):
                raise UpgradeConflict("Strict inventory requires the current maintenance owner")
            requests = self._admitting
        from coire_core.models.upgrades import UpgradeInventoryEntry

        owners = [
            UpgradeOwnerInventory(
                owner="requests",
                status="complete",
                entries=()
                if not requests
                else (
                    UpgradeInventoryEntry(
                        owner="requests",
                        kind="admission",
                        work_id="inflight-requests",
                        state="active",
                    ),
                ),
            )
        ]
        for owner in sorted(REQUIRED_OWNERS - {"requests"}):
            if owner in self._async_owners:
                owners.append(
                    UpgradeOwnerInventory(
                        owner=owner, status="unknown", reason="async_inventory_required"
                    )
                )
                continue
            collect = self._owners.get(owner)
            try:
                if collect is None:
                    raise UpgradeUnavailable()
                result = collect()
                if result.owner != owner:
                    raise UpgradeUnavailable()
                owners.append(result)
            except Exception:
                inventory_errors.add(1, {"node": self.node, "owner": owner})
                owners.append(
                    UpgradeOwnerInventory(
                        owner=owner, status="unknown", reason="owner_inventory_unavailable"
                    )
                )
        # Collectors may perform I/O or reconciliation. Never publish an idle
        # observation under an owner that changed while those collectors ran.
        with self.lock:
            latest = self.current()
            if (
                latest is None
                or (latest.job_id, latest.generation) != (job_id, generation)
                or latest.state == "released"
            ):
                raise UpgradeConflict("Inventory authority changed during collection")
        return NodeUpgradeInventory(
            job_id=job_id,
            generation=generation,
            node=self.node,
            runtime_sha256=runtime_sha256,
            observed_at=datetime.now(UTC),
            owners=tuple(owners),
        )


async def require_admission(request: Request) -> AsyncIterator[None]:
    """Hold a request permit through response streaming, after node authentication."""
    from fastapi import HTTPException

    from coire_core.models.engine import EngineStartRequest
    from coire_core.models.gateway import EngineChatRequest
    from coire_node.core_smoke import core_smoke_admission

    gate: NodeMaintenance | None = getattr(request.app.state, "maintenance", None)
    body: EngineChatRequest | EngineStartRequest | None = None
    if request.method == "POST" and (
        request.url.path == "/node/engines"
        or (
            request.url.path.startswith("/node/engines/")
            and request.url.path.endswith("/proxy/v1/chat/completions")
        )
    ):
        document = await request.body()
        if b'"coire_upgrade_scope"' in document:
            try:
                body = (
                    EngineStartRequest.model_validate_json(document)
                    if request.url.path == "/node/engines"
                    else EngineChatRequest.model_validate_json(document)
                )
            except ValueError:
                raise HTTPException(422, "invalid engine serving scope") from None
    if gate is None:
        if body is not None and body.coire_upgrade_scope is not None:
            raise HTTPException(503, "Core serving smoke requires node maintenance ownership")
        yield
        return
    permit = (
        core_smoke_admission(
            body,
            request.url.path,
            request.app.state.settings,
            gate,
            request.app.state.engines,
            request.app.state.store,
        )
        if body is not None and body.coire_upgrade_scope is not None
        else gate.admission()
    )
    try:
        permit.__enter__()
    except (UpgradeUnavailable, UpgradeConflict) as error:
        raise HTTPException(
            error.status,
            error.to_problem().model_dump(mode="json"),
            headers={"Retry-After": "5"},
        ) from error
    try:
        yield
    finally:
        permit.__exit__(None, None, None)
