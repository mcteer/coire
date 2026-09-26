"""Publish a short-lived, signed snapshot from core's authoritative registry."""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select

from coire_api.db import ModelRow, session_scope
from coire_api.failover.snapshot import SnapshotPublisher
from coire_core.failover_crypto import public_key_b64
from coire_core.failover_telemetry import snapshot_fresh, tracer
from coire_core.models.failover import (
    FailoverAccessVerifier,
    FailoverMember,
    FailoverMembershipConfig,
    FailoverModel,
    FailoverSnapshot,
)
from coire_core.models.registry import ModelState, Visibility
from coire_core.settings import Settings

logger = logging.getLogger(__name__)


def configured_membership(settings: Settings) -> FailoverMembershipConfig | None:
    """Only a complete, pinned three-member configuration may arm failover."""
    key = settings.failover_peer_key.get_secret_value().strip()
    public = settings.failover_core_public_key.strip()
    edge_a = settings.failover_edge_a_public_key.strip()
    edge_b = settings.failover_edge_b_public_key.strip()
    if not (key and public and edge_a and edge_b):
        return None
    if public_key_b64(key) != public:
        raise ValueError("core failover signing key does not match its pinned public key")
    return FailoverMembershipConfig(
        epoch=settings.failover_membership_epoch,
        signing_key_id=settings.failover_signing_key_id,
        members=[
            FailoverMember(name="coire-core", priority=0, public_key=public),
            FailoverMember(name="coire-edge-a", priority=1, public_key=edge_a),
            FailoverMember(name="coire-edge-b", priority=2, public_key=edge_b),
        ],
    )


class CoreSnapshotService:
    """Refresh the non-authoritative Studio snapshot before its bounded age expires."""

    def __init__(self, settings: Settings, membership: FailoverMembershipConfig) -> None:
        self.settings = settings
        self.membership = membership
        self.publisher = SnapshotPublisher(
            Path(settings.failover_snapshot_path),
            settings.failover_peer_key.get_secret_value(),
        )
        self._task: asyncio.Task[None] | None = None

    async def publish_once(self, now: datetime | None = None) -> FailoverSnapshot:
        moment = now or datetime.now(UTC)
        issuer = self.settings.cloudflare_access_issuer.rstrip("/")
        audience = self.settings.cloudflare_access_audience
        if not issuer or not audience:
            raise ValueError("Cloudflare Access verification is not configured")
        with tracer.start_as_current_span("coire.api.failover_snapshot_publish") as span:
            async with session_scope() as session:
                rows = (
                    await session.scalars(
                        select(ModelRow).where(
                            ModelRow.state == ModelState.READY,
                            ModelRow.visibility == Visibility.PUBLISHED,
                        )
                    )
                ).all()
            snapshot = FailoverSnapshot(
                snapshot_id=uuid4(),
                issued_at=moment,
                expires_at=moment + timedelta(seconds=self.settings.failover_snapshot_max_age_s),
                membership=self.membership,
                models=[
                    FailoverModel(
                        id=row.id,
                        slug=row.slug,
                        display_name=row.display_name,
                        entitlement=frozenset(row.entitlement or []),
                        context_window=row.context_window or 4096,
                    )
                    for row in rows
                ],
                access_verifier=FailoverAccessVerifier(
                    issuer=issuer,
                    audience=audience,
                    jwks_url=f"{issuer}/cdn-cgi/access/certs",
                ),
                signature="unsigned",
            )
            signed = await asyncio.to_thread(self.publisher.publish, snapshot)
            span.set_attribute("coire.failover.snapshot_id", str(signed.snapshot_id))
            span.set_attribute("coire.failover.model_count", len(signed.models))
            snapshot_fresh.set(1, {"host": "coire-core"})
            logger.info(
                "failover snapshot published", extra={"snapshot_id": str(signed.snapshot_id)}
            )
            return signed

    async def start(self) -> None:
        await self.publish_once()
        self._task = asyncio.create_task(self._run(), name="failover-snapshot-publisher")

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    async def _run(self) -> None:
        interval = min(30.0, self.settings.failover_snapshot_max_age_s / 3)
        while True:
            await asyncio.sleep(interval)
            try:
                await self.publish_once()
            except Exception:
                logger.exception("failover snapshot publication failed")
