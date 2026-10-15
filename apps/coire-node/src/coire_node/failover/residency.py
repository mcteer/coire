"""Native residency checks: immutable local process identities and fresh all-rank proof."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import uuid4

import httpx

from coire_core.failover_crypto import sign_ed25519
from coire_core.failover_residency import (
    eligible_model,
    process_generation,
    rank_observation_current,
    single_placement,
)
from coire_core.models.failover import (
    FailoverModel,
    FailoverRankObservation,
    FailoverRankProbe,
    FailoverShardedPlacement,
    FailoverSinglePlacement,
    FailoverSnapshot,
)
from coire_core.models.link import LinkState, RdmaState, StudioDataLinkStatus
from coire_core.models.sharding import ShardGroupState
from coire_core.settings import Settings
from coire_node.engines import EngineManager
from coire_node.sharding import ShardGroupManager
from coire_node.store import Store


class Residency:
    def __init__(
        self,
        settings: Settings,
        engines: EngineManager,
        store: Store,
        groups: ShardGroupManager | None,
        measure_link: Callable[[], StudioDataLinkStatus] | None,
    ) -> None:
        self.settings, self.engines, self.store = settings, engines, store
        self.groups, self.measure_link = groups, measure_link
        self._link_task: asyncio.Task[StudioDataLinkStatus] | None = None
        self._link_started = 0.0
        self._proofs: dict[str, float] = {}
        self._proof_lock = asyncio.Lock()

    def single_current(self, placement: FailoverSinglePlacement) -> bool:
        engine = self.engines.failover_status(placement.engine_id)
        if engine is None or not engine.slug:
            return False
        manifest = self.store.read_manifest(engine.slug)
        return (
            manifest is not None
            and manifest.sha256() == placement.target.base_manifest_sha256
            and single_placement(placement.instance_id, self.settings.node_name, engine)
            == placement
        )

    async def _link_current(self, mode: str) -> bool:
        if self.measure_link is None:
            return False
        # At most one native probe in flight. A slow probe never accumulates threads or
        # extends a prior observation's lifetime; all callers fail closed after one second.
        now = time.monotonic()
        if self._link_task is None or (self._link_task.done() and now - self._link_started > 1):
            self._link_started = now
            self._link_task = asyncio.create_task(asyncio.to_thread(self.measure_link))
        try:
            async with asyncio.timeout(1):
                link = await asyncio.shield(self._link_task)
        except (TimeoutError, OSError, ValueError):
            return False
        return (
            time.monotonic() - self._link_started <= 2
            and link.measured_at is not None
            and 0 <= (datetime.now(UTC) - link.measured_at).total_seconds() <= 2
            and link.ip_state is LinkState.UP
            and (mode == "pp" or link.rdma_state is RdmaState.UP)
        )

    async def observe(self, probe: FailoverRankProbe) -> FailoverRankObservation | None:
        groups = self.groups
        placement = probe.placement
        if groups is None:
            return None
        rank = await asyncio.to_thread(groups.local_rank, placement.group_id)
        status = groups.get(placement.group_id)
        expected = next((r for r in placement.ranks if r.node == self.settings.node_name), None)
        if (
            rank is None
            or rank.pid is None
            or rank.process_create_time is None
            or expected is None
            or status is None
            or status.state is not ShardGroupState.READY
            or status.instance_id != placement.instance_id
            or status.target != placement.target
            or status.mode != placement.mode
        ):
            return None
        generation = process_generation(
            instance_id=placement.instance_id,
            engine_id=placement.engine_id,
            group_id=placement.group_id,
            node=self.settings.node_name,
            pid=rank.pid,
            process_create_time=rank.process_create_time,
            target=placement.target,
            rank=rank.rank,
        )
        if generation != expected.generation or not await self._link_current(placement.mode):
            return None
        now = datetime.now(UTC)
        if not probe.issued_at <= now < probe.expires_at:
            return None
        result = FailoverRankObservation.model_validate(
            {
                "challenge": probe.challenge,
                "host": self.settings.node_name,
                "epoch": probe.epoch,
                "instance_id": placement.instance_id,
                "group_id": placement.group_id,
                "group_generation": placement.generation,
                "rank": expected,
                "target": placement.target,
                "healthy": True,
                "link_healthy": True,
                "issued_at": now,
                "expires_at": probe.expires_at,
                "signature": "unsigned",
            }
        )
        return result.model_copy(
            update={
                "signature": sign_ed25519(
                    result.canonical_bytes(), self.settings.failover_peer_key.get_secret_value()
                )
            }
        )

    async def current(
        self, model: FailoverModel, snapshot: FailoverSnapshot, client: httpx.AsyncClient
    ) -> bool:
        if not eligible_model(model):
            return False
        placement = model.placement
        if isinstance(placement, FailoverSinglePlacement):
            return self.single_current(placement)
        placement = cast(FailoverShardedPlacement, placement)
        # Only rank zero exposes the bare distributed server; rank one is proof-only.
        if self.settings.node_name != "coire-edge-a":
            return False
        key = snapshot.membership.model_dump_json() + placement.model_dump_json()
        async with self._proof_lock:
            if time.monotonic() < self._proofs.get(key, 0):
                return True
            now = datetime.now(UTC)
            probe = FailoverRankProbe.model_validate(
                {
                    "challenge": uuid4(),
                    "member": self.settings.node_name,
                    "epoch": snapshot.membership.epoch,
                    "placement": placement,
                    "issued_at": now,
                    "expires_at": now + timedelta(seconds=2),
                    "signature": "unsigned",
                }
            )
            probe = probe.model_copy(
                update={
                    "signature": sign_ed25519(
                        probe.canonical_bytes(), self.settings.failover_peer_key.get_secret_value()
                    )
                }
            )

            async def remote() -> FailoverRankObservation:
                host = self.settings.failover_control_hosts["coire-edge-b"]
                response = await client.post(
                    f"http://{host}:{self.settings.node_listen_port}/node/failover/rank",
                    json=probe.model_dump(mode="json"),
                    timeout=1,
                )
                response.raise_for_status()
                return FailoverRankObservation.model_validate_json(response.content)

            started = time.monotonic()
            try:
                async with asyncio.timeout(1.5):
                    observations = await asyncio.gather(self.observe(probe), remote())
            except (httpx.HTTPError, TimeoutError, ValueError, OSError):
                return False
            keys = {m.name: m.public_key for m in snapshot.membership.members}
            if any(
                obs is None or not rank_observation_current(probe, obs, keys[obs.host])
                for obs in observations
            ):
                return False
            self._proofs = {key: started + 1}
            return True
