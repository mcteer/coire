"""Independent processes and durable restart guards with accelerated isolated timings.

No native engine or Studio is contacted. Each process owns its own journal, keys, clock,
participant and authenticated production frontend; the parent only delivers signed messages.
"""

from __future__ import annotations

import base64
import multiprocessing
import time
from datetime import UTC, datetime, timedelta
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient
from pydantic import SecretStr

from coire_core.failover_crypto import public_key_b64, verify_ed25519
from coire_core.failover_election import ElectionParticipant, ElectionTimings, MemberObservation
from coire_core.models.failover import (
    ElectionSafetyRecord,
    ElectionVoteGrant,
    ElectionVoteRequest,
    FailoverAccessSubject,
    FailoverAccessVerifier,
    FailoverMember,
    FailoverMembershipConfig,
    FailoverSnapshot,
)
from coire_core.settings import Settings
from coire_failover.app import FailoverRuntime, create_app

pytestmark = pytest.mark.integration
NAMES = ("coire-core", "coire-edge-a", "coire-edge-b")


def _worker(channel: Connection, name: str, secret: str, membership_json: str, root: str) -> None:
    membership = FailoverMembershipConfig.model_validate_json(membership_json)
    member = ElectionParticipant(
        name=name,
        membership=membership,
        private_key_b64=secret,
        timings=ElectionTimings(
            promotion=timedelta(seconds=0.2),
            demotion=timedelta(seconds=0.4),
            lease=timedelta(seconds=1),
            drain=timedelta(seconds=0.3),
        ),
        safety_path=Path(root) / f"{name}.json",
    )
    now = datetime.now(UTC)
    snapshot = FailoverSnapshot(
        snapshot_id=uuid4(),
        issued_at=now,
        expires_at=now + timedelta(seconds=120),
        membership=membership,
        subjects=[
            FailoverAccessSubject(
                issuer="https://isolated.fixture",
                subject="operator",
                eligible=True,
                policy_version=1,
            )
        ],
        access_verifier=FailoverAccessVerifier(
            issuer="https://isolated.fixture",
            audience="origin-test",
            jwks_url="https://isolated.fixture/keys",
        ),
        signature="trusted-test-loader",
    )
    signing = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = jwt.algorithms.RSAAlgorithm.to_jwk(signing.public_key(), as_dict=True)
    jwk.update(kid="test", alg="RS256", use="sig")
    access = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"keys": [jwk]}))
    )
    assertion = jwt.encode(
        {
            "iss": "https://isolated.fixture",
            "aud": "origin-test",
            "sub": "operator",
            "email": "operator@example.test",
            "iat": int(now.timestamp()),
            "nbf": int(now.timestamp()) - 1,
            "exp": int(now.timestamp()) + 120,
        },
        signing,
        algorithm="RS256",
        headers={"kid": "test"},
    )
    settings = Settings(
        _secrets_dir="/nonexistent",
        failover_member_name=name,
        failover_monitor_token=SecretStr("isolated-monitor"),
    )  # type: ignore[call-arg]
    runtime = FailoverRuntime(
        settings=settings,
        load_snapshot=lambda: snapshot,
        load_lease=member.lease,
        access_client=access,
    )
    view: list[MemberObservation] = []
    with TestClient(create_app(runtime)) as http:
        channel.send("ready")
        while True:
            operation, value = channel.recv()
            now = datetime.now(UTC)
            if operation == "observe":
                view = [MemberObservation.model_validate(item) for item in value]
                member.observe(view, now)
                request = member.issue_vote_request(now)
                channel.send(request.model_dump(mode="json") if request else None)
            elif operation == "vote":
                request = ElectionVoteRequest.model_validate(value)
                grant = None
                if verify_ed25519(
                    request.canonical_bytes(),
                    request.signature,
                    member.member_public_key(request.candidate),
                ):
                    grant = member.vote(
                        request.epoch, request.term, request.candidate, now, request.request_id
                    )
                channel.send(grant.model_dump(mode="json") if grant else None)
            elif operation == "accept":
                for grant in value:
                    member.receive_grant(ElectionVoteGrant.model_validate(grant), now)
                member.observe(view, datetime.now(UTC))
                channel.send(True)
            elif operation == "rotate_snapshot_epoch":
                snapshot = snapshot.model_copy(
                    update={
                        "membership": membership.model_copy(update={"epoch": membership.epoch + 1})
                    }
                )
                channel.send(True)
            elif operation == "admit":
                response = http.get("/v1/models", headers={"Cf-Access-Jwt-Assertion": assertion})
                assert response.status_code in {200, 503}, response.text
                lease = member.lease()
                admitted = response.status_code == 200
                if admitted:
                    assert lease and lease.proof and lease.proof.is_valid_for(membership)
                channel.send(admitted)
            else:
                raise AssertionError(operation)


@pytest.mark.parametrize("isolated", NAMES)
def test_process_restart_and_each_partition_fence_stale_origins(
    tmp_path: Path, isolated: str
) -> None:
    private = {
        name: base64.b64encode(Ed25519PrivateKey.generate().private_bytes_raw()).decode()
        for name in NAMES
    }
    membership = FailoverMembershipConfig(
        epoch=1,
        signing_key_id="test",
        members=[
            FailoverMember(name=name, priority=i, public_key=public_key_b64(private[name]))
            for i, name in enumerate(NAMES)
        ],
    )
    context = multiprocessing.get_context("spawn")
    processes: dict[str, Any] = {}
    channels: dict[str, Connection] = {}

    def start(name: str) -> None:
        parent, child = context.Pipe()
        process = context.Process(
            target=_worker,
            args=(child, name, private[name], membership.model_dump_json(), str(tmp_path)),
        )
        process.start()
        child.close()
        processes[name], channels[name] = process, parent
        assert parent.poll(15), "participant startup timed out"
        assert parent.recv() == "ready"

    def rpc(name: str, operation: str, value: object = None) -> Any:
        channels[name].send((operation, value))
        assert channels[name].poll(5), f"{name} {operation} timed out"
        return channels[name].recv()

    def cycle(partition: bool) -> list[str]:
        for name in NAMES:
            view = [
                {
                    "name": peer,
                    "health": "unreachable"
                    if partition and name != peer and isolated in (name, peer)
                    else "healthy",
                    "since": (datetime.now(UTC) - timedelta(seconds=2)).isoformat(),
                }
                for peer in NAMES
            ]
            request = rpc(name, "observe", view)
            grants = []
            if request:
                for peer in NAMES:
                    if peer != name and not (partition and isolated in (name, peer)):
                        grant = rpc(peer, "vote", request)
                        if grant:
                            grants.append(grant)
            rpc(name, "accept", grants)
        admitted = [name for name in NAMES if rpc(name, "admit")]
        assert len(admitted) <= 1, admitted
        return admitted

    try:
        for name in NAMES:
            # A known empty initial record avoids the five-minute first-install quarantine;
            # the killed process's actual outstanding grants are preserved on restart below.
            (tmp_path / f"{name}.json").write_text(
                ElectionSafetyRecord(
                    member=name,
                    epoch=1,
                    highest_term=1,
                    prior_max_lifetime_s=1,
                ).model_dump_json()
            )
            start(name)
        deadline = time.monotonic() + 8
        while cycle(False) != ["coire-core"]:
            assert time.monotonic() < deadline
            time.sleep(0.03)
        processes[isolated].kill()
        processes[isolated].join(5)
        channels[isolated].close()
        start(isolated)
        expected = "coire-edge-a" if isolated == "coire-core" else "coire-core"
        deadline = time.monotonic() + 8
        while cycle(True) != [expected]:
            assert time.monotonic() < deadline
            time.sleep(0.03)
        # Continue past several authority renewals; a frozen stale route never reopens.
        end = time.monotonic() + 2
        while time.monotonic() < end:
            cycle(True)
            time.sleep(0.03)
        # An independently loaded newer trusted snapshot invalidates an otherwise live
        # old-epoch quorum proof at the actual frontend admission boundary.
        assert rpc(expected, "rotate_snapshot_epoch")
        assert not rpc(expected, "admit")
    finally:
        for process in processes.values():
            if process.is_alive():
                process.kill()
            process.join(5)
        for channel in channels.values():
            channel.close()
