"""Subscribe to core's signed snapshot and run the Studio election member."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path

import httpx

from coire_core.failover_crypto import public_key_b64, sign_ed25519
from coire_core.failover_election import ServiceRole, timings_from_settings
from coire_core.failover_poller import FailoverPoller, peer_url
from coire_core.failover_telemetry import snapshot_fresh, tracer
from coire_core.models.failover import (
    FailoverEventBatch,
    FailoverHeartbeat,
    FailoverSnapshot,
    FailoverStatus,
)
from coire_core.settings import Settings
from coire_node.docker_api import DockerAPI, DockerAPIError
from coire_node.failover.journal import ElectionJournal
from coire_node.failover.participant import StudioParticipant
from coire_node.routes.failover import set_participant

logger = logging.getLogger(__name__)


class StudioFailoverController:
    """A missing snapshot at startup is retried without restarting the node agent."""

    def __init__(
        self,
        settings: Settings,
        client: httpx.AsyncClient | None = None,
        docker: DockerAPI | None = None,
    ) -> None:
        self.settings = settings
        self._client = client or httpx.AsyncClient()
        self._owns_client = client is None
        self._poller: FailoverPoller | None = None
        self._task: asyncio.Task[None] | None = None
        self._lifecycle_task: asyncio.Task[None] | None = None
        self._docker = docker
        self._last_issued_at: datetime | None = None
        self._last_epoch = 0

    def _verified(self, snapshot: FailoverSnapshot, *, require_fresh: bool = True) -> bool:
        key = self.settings.failover_peer_key.get_secret_value().strip()
        mine = next(
            member
            for member in snapshot.membership.members
            if member.name == self.settings.failover_member_name
        )
        return (
            snapshot.signature_is_valid(self.settings.failover_core_public_key)
            and (not require_fresh or snapshot.is_fresh(self.settings.failover_snapshot_max_age_s))
            and mine.public_key == public_key_b64(key)
        )

    async def refresh_once(self) -> bool:
        """Fetch and atomically install a verified snapshot; keep the old one on failure."""
        now = datetime.now(UTC)
        key = self.settings.failover_peer_key.get_secret_value().strip()
        unsigned = FailoverHeartbeat(
            member=self.settings.failover_member_name, sent_at=now, signature="unsigned"
        )
        beat = unsigned.model_copy(
            update={"signature": sign_ed25519(unsigned.canonical_bytes(), key)}
        )
        try:
            response = await self._client.post(
                peer_url(self.settings, "coire-core", "/snapshot"),
                json=beat.model_dump(mode="json"),
                timeout=5.0,
            )
            response.raise_for_status()
            snapshot = FailoverSnapshot.model_validate(response.json())
            if not self._verified(snapshot):
                return False
            if (
                self._last_issued_at is not None and snapshot.issued_at < self._last_issued_at
            ) or snapshot.membership.epoch < self._last_epoch:
                return False
        except (httpx.HTTPError, ValueError, StopIteration):
            return False
        await asyncio.to_thread(self._write_snapshot, snapshot)
        self._last_issued_at = snapshot.issued_at
        self._last_epoch = snapshot.membership.epoch
        snapshot_fresh.set(1, {"host": self.settings.failover_member_name})
        logger.info(
            "failover snapshot installed",
            extra={
                "snapshot_id": str(snapshot.snapshot_id),
                "host": self.settings.failover_member_name,
            },
        )
        await self._arm(snapshot)
        await self._reconcile_once()
        return True

    async def _reconcile_once(self) -> None:
        path = Path(self.settings.failover_proof_path)
        journal = ElectionJournal(
            path.with_name(f"{self.settings.failover_member_name}.journal.json")
        )
        entries = await asyncio.to_thread(journal.entries)
        if not entries:
            return
        unsigned = FailoverEventBatch(
            host=self.settings.failover_member_name, events=entries, signature="unsigned"
        )
        batch = unsigned.model_copy(
            update={
                "signature": sign_ed25519(
                    unsigned.canonical_bytes(),
                    self.settings.failover_peer_key.get_secret_value(),
                )
            }
        )
        try:
            response = await self._client.post(
                peer_url(self.settings, "coire-core", "/events"),
                json=batch.model_dump(mode="json"),
                timeout=5.0,
            )
            response.raise_for_status()
        except httpx.HTTPError:
            logger.warning("failover event reconciliation deferred")

    def _write_snapshot(self, snapshot: FailoverSnapshot) -> None:
        path = Path(self.settings.failover_snapshot_path)
        path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        path.parent.chmod(0o755)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=".snapshot-", delete=False
        ) as handle:
            os.fchmod(handle.fileno(), 0o644)
            json.dump(snapshot.model_dump(mode="json"), handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
            temporary = Path(handle.name)
        os.replace(temporary, path)

    async def _arm(self, snapshot: FailoverSnapshot) -> None:
        if self._poller is not None:
            if self._poller.participant.membership == snapshot.membership:
                return
            await self._disarm()
        name = self.settings.failover_member_name
        proof = Path(self.settings.failover_proof_path)
        participant = StudioParticipant(
            name=name,
            membership=snapshot.membership,
            private_key_b64=self.settings.failover_peer_key.get_secret_value(),
            timings=timings_from_settings(self.settings),
            proof_path=proof,
            journal=ElectionJournal(proof.with_name(f"{name}.journal.json")),
            restart_hold=True,
        )
        set_participant(participant)
        self._poller = FailoverPoller(
            participant,
            self.settings,
            override=participant.active_override,
            in_flight=self._in_flight,
        )
        await self._poller.start()
        if self._docker is not None:
            self._lifecycle_task = asyncio.create_task(
                self._manage_frontend(), name="failover-frontend-lifecycle"
            )

    async def _disarm(self) -> None:
        lifecycle = self._lifecycle_task
        self._lifecycle_task = None
        if lifecycle is not None:
            lifecycle.cancel()
            with suppress(asyncio.CancelledError):
                await lifecycle
        poller = self._poller
        self._poller = None
        if poller is not None:
            await poller.stop()
            await asyncio.to_thread(Path(self.settings.failover_proof_path).unlink, missing_ok=True)
        if self._docker is not None:
            try:
                await self._docker.stop_container("coire-failover")
            except (DockerAPIError, httpx.HTTPError):
                logger.exception("could not stop failover frontend while fencing the member")
        set_participant(None)

    async def _manage_frontend(self) -> None:
        """Run only the precreated, hardened failover container while this host holds a lease."""
        while True:
            try:
                with tracer.start_as_current_span("coire.node.failover_frontend_lifecycle"):
                    await self._sync_frontend()
            except (DockerAPIError, httpx.HTTPError, ValueError):
                logger.exception("failover frontend lifecycle update failed")
            await asyncio.sleep(1)

    async def _sync_frontend(self) -> None:
        poller = self._poller
        docker = self._docker
        if poller is None or docker is None:
            return
        container = await docker.inspect_container("coire-failover")
        if container is None:
            logger.error("precreated coire-failover container is missing")
            return
        config = container.get("Config") or {}
        host = container.get("HostConfig") or {}
        state = container.get("State") or {}
        if (
            config.get("Image") != self.settings.failover_frontend_image
            or config.get("User") != "65532:65532"
            or host.get("ReadonlyRootfs") is not True
            or "ALL" not in host.get("CapDrop", [])
        ):
            raise ValueError(
                "precreated failover container does not match the approved image and policy"
            )
        running = state.get("Running") is True
        needed = poller.participant.role in (ServiceRole.ELECTED, ServiceRole.DRAINING)
        if needed and not running:
            await docker.start_container("coire-failover")
        elif running and not needed:
            await docker.stop_container("coire-failover")

    async def _in_flight(self) -> int:
        poller = self._poller
        if poller is None or poller.participant.role not in (
            ServiceRole.ELECTED,
            ServiceRole.DRAINING,
        ):
            return 0
        try:
            response = await self._client.get("http://127.0.0.1:8004/failover/tier", timeout=1.0)
            response.raise_for_status()
            return FailoverStatus.model_validate(response.json()).in_flight
        except (httpx.HTTPError, ValueError):
            return 1

    async def start(self) -> None:
        try:
            snapshot = FailoverSnapshot.model_validate_json(
                await asyncio.to_thread(
                    Path(self.settings.failover_snapshot_path).read_text, encoding="utf-8"
                )
            )
            if self._verified(snapshot, require_fresh=False):
                self._last_issued_at = snapshot.issued_at
                self._last_epoch = snapshot.membership.epoch
                await self._arm(snapshot)
        except (OSError, ValueError, StopIteration):
            pass
        self._task = asyncio.create_task(self._run(), name="failover-snapshot-subscriber")

    async def _run(self) -> None:
        interval = min(30.0, self.settings.failover_snapshot_max_age_s / 3)
        while True:
            try:
                with tracer.start_as_current_span("coire.node.failover_snapshot_refresh"):
                    await self.refresh_once()
            except Exception:
                logger.exception("failover snapshot refresh failed")
            await asyncio.sleep(interval)

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        await self._disarm()
        if self._owns_client:
            await self._client.aclose()


def build_controller(settings: Settings, docker: DockerAPI) -> StudioFailoverController | None:
    name = settings.failover_member_name.strip()
    key = settings.failover_peer_key.get_secret_value().strip()
    if name not in {"coire-edge-a", "coire-edge-b"} or not key:
        return None
    if not settings.failover_core_public_key.strip():
        logger.warning("failover controller not started; core verification key is missing")
        return None
    if not settings.failover_frontend_image:
        logger.warning("failover controller not started; pinned frontend image is missing")
        return None
    return StudioFailoverController(settings, docker=docker)
