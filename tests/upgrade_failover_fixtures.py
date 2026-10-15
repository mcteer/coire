"""Armed failover boundary for the isolated executor workflow matrix.

Physical replacement and SQL are still the workflow test's explicit seams. Every
probe traverses the production API middleware and verifies a signed peer reply.
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import SecretStr

from coire_api import auth
from coire_api.app import create_app
from coire_api.auth import Principal, PrincipalKind
from coire_api.failover.participant import CoreParticipant
from coire_api.routes import failover
from coire_api.upgrades import admission
from coire_core.failover_crypto import public_key_b64, verify_ed25519
from coire_core.failover_election import (
    ElectionParticipant,
    ElectionTimings,
    MemberHealth,
    MemberObservation,
)
from coire_core.models.failover import FailoverHeartbeat, FailoverMember, FailoverMembershipConfig
from coire_core.settings import Settings


class ArmedCoreProbe:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
        keys = {
            name: base64.b64encode(Ed25519PrivateKey.generate().private_bytes_raw()).decode()
            for name in ("coire-core", "coire-edge-a", "coire-edge-b")
        }
        membership = FailoverMembershipConfig(
            epoch=1,
            signing_key_id="workflow-test",
            members=[
                FailoverMember(name=name, priority=i, public_key=public_key_b64(key))
                for i, (name, key) in enumerate(keys.items())
            ],
        )
        timings = ElectionTimings(
            promotion=timedelta(seconds=15),
            demotion=timedelta(seconds=45),
            lease=timedelta(seconds=300),
            drain=timedelta(seconds=30),
        )
        self.core = CoreParticipant(
            name="coire-core",
            membership=membership,
            private_key_b64=keys["coire-core"],
            timings=timings,
        )
        self.peer = ElectionParticipant(
            name="coire-edge-a",
            membership=membership,
            private_key_b64=keys["coire-edge-a"],
            timings=timings,
        )
        now = datetime.now(UTC)
        view = [
            MemberObservation(name=name, health=MemberHealth.HEALTHY, since=now) for name in keys
        ]
        self.core.observe(view, now)
        self.peer.observe(view, now)
        vote = self.peer.vote(1, self.core.term, "coire-core", now)
        assert vote and self.core.receive_grant(vote, now)
        self.core.observe(view, now)
        assert self.core.serving()
        self.fenced = True
        self.reached = 0
        self.probes = 0

        async def scalar(_statement: object) -> str | None:
            return "core" if self.fenced else None

        @asynccontextmanager
        async def session() -> AsyncIterator[SimpleNamespace]:
            yield SimpleNamespace(scalar=scalar)

        monkeypatch.setattr(admission, "session_scope", session)
        monkeypatch.setattr(
            auth,
            "authenticate_request",
            AsyncMock(return_value=Principal(kind=PrincipalKind.USER, user_id=uuid4())),
        )
        self.app = create_app(
            Settings(  # type: ignore[call-arg]
                _secrets_dir="/nonexistent",
                upgrades_enabled=True,
                failover_member_name="coire-core",
                failover_peer_key=SecretStr(keys["coire-core"]),
            )
        )

        @self.app.get("/v1/qualification")
        async def user_work() -> dict[str, bool]:
            self.reached += 1
            return {"ok": True}

        failover.set_participant(self.core)
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://isolated-core"
        )

    async def probe(self, coordination: Path, *, expect_inflight: bool = False) -> None:
        def fingerprint() -> dict[str, bytes]:
            return {
                file.name: file.read_bytes() for file in coordination.iterdir() if file.is_file()
            }

        before = await asyncio.to_thread(fingerprint)
        beat = self.peer.issue_heartbeat(datetime.now(UTC))
        response = await self.client.post(
            "/api/v1/failover/heartbeat", json=beat.model_dump(mode="json")
        )
        assert response.status_code == 200
        reply = FailoverHeartbeat.model_validate(response.json())
        assert reply.challenge == beat.challenge
        assert verify_ed25519(
            reply.canonical_bytes(), reply.signature, self.core.member_public_key("coire-core")
        )
        assert (await self.client.get("/v1/qualification")).status_code == (
            503 if self.fenced else 200
        )
        if self.fenced:
            for route in ("snapshot", "events"):
                assert (
                    await self.client.post(
                        "/api/v1/failover/" + route, json=beat.model_dump(mode="json")
                    )
                ).status_code == 503
        assert before == await asyncio.to_thread(fingerprint), (
            "failover safety exchange changed the executor's physical owner"
        )
        inventory = await self.app.state.upgrade_admissions.inventory(("coire-api",))
        assert bool(inventory.entries) is expect_inflight
        self.probes += 1

    async def close(self) -> None:
        failover.set_participant(None)
        await self.client.aclose()
