"""Lightweight election poller shared by core and the Studios.

Heartbeats reuse feature 009's rule: a slow beat is degraded, several missed beats are
unreachable, and recovery takes more than one success. The election timings then damp
promotion and hand-back. One task, two peers, no database.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Protocol

import httpx
from pydantic import ValidationError

from coire_core.failover_crypto import verify_ed25519
from coire_core.failover_election import (
    ElectionParticipant,
    MemberHealth,
    MemberObservation,
    ServiceRole,
)
from coire_core.models.failover import (
    ElectionVoteGrant,
    ElectionVoteRequest,
    FailoverHeartbeat,
    FailoverOverride,
)
from coire_core.settings import Settings

logger = logging.getLogger(__name__)


class PeerExchange(Protocol):
    async def heartbeat(self, peer: str, body: FailoverHeartbeat) -> tuple[bool, float]: ...

    async def request_vote(
        self, peer: str, body: ElectionVoteRequest
    ) -> ElectionVoteGrant | None: ...


class PeerLiveness:
    """Classify probe results without inventing a second health model."""

    def __init__(
        self, *, failure_limit: int, recovery_beats: int, latency_budget_ms: float
    ) -> None:
        if failure_limit < 1 or recovery_beats < 1:
            raise ValueError("heartbeat damping requires at least one beat")
        self._failure_limit = failure_limit
        self._recovery_beats = recovery_beats
        self._latency_budget_ms = latency_budget_ms
        self._failures: dict[str, int] = {}
        self._recoveries: dict[str, int] = {}
        self._health: dict[str, MemberHealth] = {}
        self._since: dict[str, datetime] = {}
        self._down_since: dict[str, datetime] = {}

    def note(self, name: str, *, ok: bool, latency_ms: float, now: datetime) -> None:
        if not ok:
            self._failures[name] = self._failures.get(name, 0) + 1
            self._recoveries[name] = 0
            self._down_since.setdefault(name, now)
            if self._failures[name] >= self._failure_limit:
                self._health[name] = MemberHealth.UNREACHABLE
                self._since[name] = self._down_since[name]
            return
        self._failures[name] = 0
        self._down_since.pop(name, None)
        current = self._health.get(name)
        if latency_ms > self._latency_budget_ms:
            self._recoveries[name] = 0
            if current is not MemberHealth.DEGRADED:
                self._since[name] = now
            self._health[name] = MemberHealth.DEGRADED
            return
        if current in (MemberHealth.UNREACHABLE, MemberHealth.DEGRADED):
            self._recoveries[name] = self._recoveries.get(name, 0) + 1
            if self._recoveries[name] >= self._recovery_beats:
                self._health[name] = MemberHealth.HEALTHY
                self._since[name] = now
                self._recoveries[name] = 0
            return
        self._health[name] = MemberHealth.HEALTHY
        self._since.setdefault(name, now)

    def observations(self, self_name: str, now: datetime) -> list[MemberObservation]:
        view = [
            MemberObservation(
                name=self_name, health=MemberHealth.HEALTHY, since=self._since.get(self_name, now)
            )
        ]
        self._since.setdefault(self_name, now)
        for name, health in self._health.items():
            view.append(MemberObservation(name=name, health=health, since=self._since[name]))
        return view


def peer_url(settings: Settings, peer: str, route: str) -> str:
    """Core is reached through the published control port. Studios use the node listener."""
    if peer == "coire-core":
        return f"http://coire-core:{settings.core_api_port}/api/v1/failover{route}"
    prefix = "/election" if route in {"/votes", "/handback", "/override"} else ""
    return f"http://{peer}:{settings.node_listen_port}/node/failover{prefix}{route}"


class HttpPeerExchange:
    """Signed heartbeat and vote exchange over the existing control listeners."""

    def __init__(
        self,
        participant: ElectionParticipant,
        settings: Settings,
        client: httpx.AsyncClient,
    ) -> None:
        self._participant = participant
        self._settings = settings
        self._client = client

    async def heartbeat(self, peer: str, body: FailoverHeartbeat) -> tuple[bool, float]:
        started = time.perf_counter()
        try:
            response = await self._client.post(
                peer_url(self._settings, peer, "/heartbeat"),
                json=body.model_dump(mode="json"),
                timeout=1.0,
            )
            reply = FailoverHeartbeat.model_validate(response.json())
        except (httpx.HTTPError, ValidationError, ValueError):
            return False, (time.perf_counter() - started) * 1000
        latency_ms = (time.perf_counter() - started) * 1000
        if response.status_code != 200 or reply.member != peer:
            return False, latency_ms
        ok = verify_ed25519(
            reply.canonical_bytes(), reply.signature, self._participant.member_public_key(peer)
        )
        return ok, latency_ms

    async def request_vote(self, peer: str, body: ElectionVoteRequest) -> ElectionVoteGrant | None:
        try:
            response = await self._client.post(
                peer_url(self._settings, peer, "/votes"),
                json=body.model_dump(mode="json"),
                timeout=1.0,
            )
            if response.status_code != 200:
                return None
            return ElectionVoteGrant.model_validate(response.json())
        except (httpx.HTTPError, ValidationError, ValueError):
            return None


class FailoverPoller:
    """One member's poll loop. Call `tick` directly in tests."""

    def __init__(
        self,
        participant: ElectionParticipant,
        settings: Settings,
        exchange: PeerExchange | None = None,
        *,
        in_flight: Callable[[], int | Awaitable[int]] | None = None,
        override: Callable[[], FailoverOverride | None] | None = None,
    ) -> None:
        self.participant = participant
        self._settings = settings
        self._in_flight = in_flight or (lambda: 0)
        self._override = override or (lambda: None)
        self._client = httpx.AsyncClient() if exchange is None else None
        self._exchange = exchange or HttpPeerExchange(
            participant, settings, self._client or httpx.AsyncClient()
        )
        limit = settings.node_probe_failures_before_unreachable
        self._liveness = PeerLiveness(
            failure_limit=limit,
            recovery_beats=limit,
            latency_budget_ms=settings.failover_heartbeat_latency_budget_ms,
        )
        self._stopping = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def tick(self, now: datetime | None = None) -> None:
        moment = now or datetime.now(UTC)
        peers = [
            member.name
            for member in self.participant.membership.members
            if member.name != self.participant.name
        ]
        beat = self.participant.issue_heartbeat(moment)
        probed = await asyncio.gather(
            *(self._exchange.heartbeat(peer, beat) for peer in peers),
            return_exceptions=True,
        )
        for peer, result in zip(peers, probed, strict=True):
            if isinstance(result, BaseException):
                self._liveness.note(peer, ok=False, latency_ms=0, now=moment)
                continue
            ok, latency_ms = result
            self._liveness.note(peer, ok=ok, latency_ms=latency_ms, now=moment)
        observations = self._liveness.observations(self.participant.name, moment)
        active = self._in_flight()
        if inspect.isawaitable(active):
            active = await active
        self.participant.observe(observations, moment, in_flight=active, override=self._override())
        request = self.participant.issue_vote_request(moment)
        if request is None:
            return
        grants = await self._collect_grants(peers, request, moment)
        if not grants and self.participant.role is ServiceRole.CANDIDATE:
            self.participant.adopt_term(self.participant.term + 1)
            request = self.participant.issue_vote_request(moment)
            if request is not None:
                grants = await self._collect_grants(peers, request, moment)
        for grant in grants:
            self.participant.receive_grant(grant, moment)
        active = self._in_flight()
        if inspect.isawaitable(active):
            active = await active
        self.participant.observe(observations, moment, in_flight=active, override=self._override())

    async def _collect_grants(
        self, peers: list[str], request: ElectionVoteRequest, moment: datetime
    ) -> list[ElectionVoteGrant]:
        del moment
        replies = await asyncio.gather(
            *(self._exchange.request_vote(peer, request) for peer in peers),
            return_exceptions=True,
        )
        return [grant for grant in replies if isinstance(grant, ElectionVoteGrant)]

    async def start(self) -> None:
        self._stopping.clear()
        self._task = asyncio.create_task(
            self._run(), name=f"failover-poller-{self.participant.name}"
        )

    async def stop(self) -> None:
        self._stopping.set()
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if self._client is not None:
            await self._client.aclose()

    async def _run(self) -> None:
        while not self._stopping.is_set():
            try:
                await self.tick()
            except Exception:
                logger.exception("failover poll failed for %s", self.participant.name)
            try:
                await asyncio.wait_for(
                    self._stopping.wait(), timeout=self._settings.failover_election_interval_s
                )
            except TimeoutError:
                continue
