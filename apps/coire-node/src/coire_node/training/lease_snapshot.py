"""Ephemeral authenticated core lease observations; admission never performs network I/O."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
import uuid
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime
from urllib.parse import urlsplit

import httpx
from opentelemetry import metrics, trace

from coire_core.errors import TrainingConflict
from coire_core.models.training_node import NodeTrainingLeaseSnapshot
from coire_core.net import shared_http_ssl_context
from coire_core.settings import Settings

logger = logging.getLogger(__name__)
tracer = trace.get_tracer("coire.node.training")
fetches = metrics.get_meter("coire.node.training").create_counter(
    "coire_training_lease_snapshot_fetches_total"
)
MAX_AGE_S = 5.0
MAX_BYTES = 64 * 1024


class TrainingLeaseSnapshotReader:
    def __init__(
        self,
        settings: Settings,
        *,
        node: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.settings = settings
        self.node = node or settings.node_name
        if self.node not in {"coire-edge-a", "coire-edge-b"}:
            raise TrainingConflict("Lease inventory requires a declared Studio")
        self.now, self.monotonic = now, monotonic
        self.lock = threading.RLock()
        self.snapshot: NodeTrainingLeaseSnapshot | None = None
        self.deadline = 0.0
        self.client = httpx.AsyncClient(
            timeout=2.0,
            follow_redirects=False,
            trust_env=False,
            transport=transport,
            verify=shared_http_ssl_context(trust_env=False),
        )
        self.stop = asyncio.Event()
        self.task: asyncio.Task[None] | None = None
        self.lifecycle = asyncio.Lock()

    def url(self) -> str:
        origin = self.settings.training_input_api_url
        parsed = urlsplit(origin)
        if (
            parsed.hostname != self.settings.core_control_host
            or parsed.scheme not in {"http", "https"}
            or parsed.username
            or parsed.password
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise TrainingConflict("Lease inventory origin differs from declared core")
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            raise TrainingConflict("Lease inventory port is invalid")
        return f"{origin.rstrip('/')}/api/v1/internal/training/nodes/{self.node}/leases"

    def accept(self, snapshot: NodeTrainingLeaseSnapshot) -> None:
        snapshot = NodeTrainingLeaseSnapshot.model_validate(snapshot.model_dump(), strict=True)
        if self.stop.is_set():
            raise TrainingConflict("Lease inventory reader is closed")
        now, observed = self.now(), self.monotonic()
        age = (now - snapshot.sampled_at).total_seconds()
        remaining = (snapshot.expires_at - now).total_seconds()
        validity = (snapshot.expires_at - snapshot.sampled_at).total_seconds()
        if (
            snapshot.node != self.node
            or not 0 <= age < MAX_AGE_S
            or not 0 < remaining <= MAX_AGE_S
            or validity > MAX_AGE_S
        ):
            raise TrainingConflict("Lease inventory scope or freshness is invalid")
        with self.lock:
            deadline = observed + min(remaining, MAX_AGE_S - age)
            if self.snapshot is not None:
                if snapshot.sampled_at < self.snapshot.sampled_at:
                    raise TrainingConflict("Lease inventory observation rewound")
                if snapshot.sampled_at == self.snapshot.sampled_at:
                    if snapshot != self.snapshot:
                        raise TrainingConflict("Lease inventory observation changed")
                    deadline = min(deadline, self.deadline)
            self.snapshot, self.deadline = snapshot, deadline

    def __call__(self, instances: set[uuid.UUID]) -> int:
        self.url()  # Pure scope validation; never contact core under the admission lock.
        with self.lock:
            snapshot = self.snapshot
            now = self.now()
            if (
                self.stop.is_set()
                or snapshot is None
                or snapshot.node != self.node
                or self.monotonic() >= self.deadline
                or not snapshot.sampled_at <= now < snapshot.expires_at
                or (now - snapshot.sampled_at).total_seconds() >= MAX_AGE_S
            ):
                raise TrainingConflict("Lease inventory is unavailable or stale")
            if not instances.issubset(snapshot.active_leases):
                raise TrainingConflict("Lease inventory omits an addressed instance")
            # Empty scope is the isolated memory probe: check ALL leases on this node.
            return (
                sum(snapshot.active_leases[identity] for identity in instances)
                if instances
                else sum(snapshot.active_leases.values())
            )

    async def refresh(self) -> None:
        url = self.url()  # Validate the complete origin before sending any credential.
        token = self.settings.node_token.get_secret_value()
        if not token:
            raise TrainingConflict("Lease inventory credential is unavailable")
        body = bytearray()
        with tracer.start_as_current_span("coire.node.training.lease_snapshot"):
            async with (
                asyncio.timeout(2.0),
                self.client.stream(
                    "GET",
                    url,
                    headers={
                        "Authorization": "Bearer " + token,
                        "X-Coire-Node": self.node,
                        "Accept-Encoding": "identity",
                    },
                ) as response,
            ):
                response.raise_for_status()
                if response.url != httpx.URL(url):
                    raise TrainingConflict("Lease inventory response path differs")
                if response.headers.get("content-encoding", "identity") != "identity":
                    raise TrainingConflict("Lease inventory encoding is unsupported")
                async for chunk in response.aiter_bytes(16 * 1024):
                    if len(body) + len(chunk) > MAX_BYTES:
                        raise TrainingConflict("Lease inventory exceeds its byte bound")
                    body.extend(chunk)
            self.accept(NodeTrainingLeaseSnapshot.model_validate_json(body, strict=True))
        fetches.add(1, {"outcome": "accepted"})

    async def observe(self) -> None:
        try:
            await self.refresh()
        except (TrainingConflict, ValueError, httpx.HTTPError, TimeoutError, OSError) as exc:
            fetches.add(1, {"outcome": "refused"})
            logger.warning(
                "lease inventory observation refused",
                extra={"node": self.node, "error_type": type(exc).__name__},
            )

    async def start(self) -> None:
        async with self.lifecycle:
            if self.stop.is_set():
                raise TrainingConflict("Lease inventory reader is closed")
            if self.task is None:
                await self.observe()
                self.task = asyncio.create_task(self.poll(), name="training-lease-snapshot")

    async def poll(self) -> None:
        while not self.stop.is_set():
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=1.0)
            except TimeoutError:
                await self.observe()

    async def aclose(self) -> None:
        async with self.lifecycle:
            self.stop.set()
            if self.task is not None:
                self.task.cancel()
                with suppress(asyncio.CancelledError):
                    await self.task
            await self.client.aclose()
            with self.lock:
                self.snapshot, self.deadline = None, 0.0
