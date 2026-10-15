"""Residency never treats a ready label, reused PID or old rank proof as live authority."""

import asyncio
import base64
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import httpx
import psutil
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.responses import StreamingResponse
from pydantic import SecretStr

from coire_core.failover_crypto import public_key_b64
from coire_core.failover_lifetime import FailoverAuthorityLost, guarded_await
from coire_core.failover_residency import sharded_placement
from coire_core.models.adapters import InferenceTarget
from coire_core.models.failover import (
    FailoverAccessVerifier,
    FailoverMember,
    FailoverMembershipConfig,
    FailoverModel,
    FailoverRankProbe,
    FailoverSnapshot,
)
from coire_core.models.link import LinkState, RdmaState, StudioDataLinkStatus
from coire_core.models.sharding import ShardGroupState, ShardGroupStatus, ShardingMode, ShardRank
from coire_core.settings import Settings
from coire_node.engines import EngineManager
from coire_node.failover.lifetime import bounded_response
from coire_node.failover.residency import Residency
from coire_node.sharding import ShardGroupManager
from coire_node.store import Store


@pytest.mark.parametrize(
    "fault", [None, "pid_reuse", "wrong_command", "ambiguous", "dead", "legacy"]
)
def test_sharded_worker_identity_is_owned_and_unambiguous(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str | None,
) -> None:
    settings = Settings(
        _secrets_dir="/nonexistent",
        node_name="coire-edge-b",
        node_state_dir=str(tmp_path / "state"),
    )  # type: ignore[call-arg]
    store = Store(str(tmp_path / "models"))
    manager = ShardGroupManager(settings, store)
    now = datetime.now(UTC)
    group = ShardGroupStatus(
        group_id=uuid4(),
        instance_id=uuid4(),
        command_id=uuid4(),
        mode=ShardingMode.TENSOR_PARALLEL,
        state=ShardGroupState.READY,
        started_at=now - timedelta(seconds=1),
        target=InferenceTarget(model_id=uuid4(), variant_id=uuid4(), base_manifest_sha256="a" * 64),
        ranks=[ShardRank(rank=1, node_name="coire-edge-b", host="coire-edge-b.fabric", port=9600)],
    )
    manager._groups[group.group_id] = group
    manager._model_paths[group.group_id] = "/models/trusted"
    proc = SimpleNamespace(
        pid=123,
        cmdline=lambda: ["python", "-m", "mlx_lm.server", "--model", "/models/trusted"],
        environ=lambda: {
            "COIRE_SHARD_GROUP_ID": str(group.group_id),
            "COIRE_SHARD_COMMAND_ID": str(group.command_id),
        },
        create_time=lambda: now.timestamp(),
        status=lambda: psutil.STATUS_SLEEPING,
    )
    monkeypatch.setattr("coire_node.sharding.psutil.process_iter", lambda: iter([proc]))
    monkeypatch.setattr("coire_node.sharding.psutil.Process", lambda _pid: proc)
    if fault == "ambiguous":
        monkeypatch.setattr("coire_node.sharding.psutil.process_iter", lambda: iter([proc, proc]))
    elif fault == "wrong_command":
        proc.environ = lambda: {
            "COIRE_SHARD_GROUP_ID": str(group.group_id),
            "COIRE_SHARD_COMMAND_ID": str(uuid4()),
        }
    elif fault == "dead":
        proc.status = lambda: psutil.STATUS_ZOMBIE
    elif fault == "legacy":
        group.target = None
    elif fault == "pid_reuse":
        assert manager.local_rank(group.group_id) is not None
        proc.create_time = lambda: now.timestamp() + 0.001
    rank = manager.local_rank(group.group_id)
    assert (rank is not None) is (fault is None)
    if rank is not None:
        assert rank.pid == 123 and rank.process_create_time == now.timestamp()


async def test_async_admission_finishes_before_any_engine_work() -> None:
    engine = Mock()

    async def work() -> None:
        engine()

    async def denied() -> bool:
        await asyncio.sleep(0.01)
        return False

    with pytest.raises(FailoverAuthorityLost):
        await guarded_await(work(), denied)
    engine.assert_not_called()


@pytest.mark.parametrize(
    "state,expected",
    [
        (b"enabled\n", RdmaState.UP),
        (b"disabled\n", RdmaState.DOWN),
        (b"uncertain\n", RdmaState.UNKNOWN),
    ],
)
def test_native_rdma_status_is_not_inferred_from_profiler_text(
    monkeypatch: pytest.MonkeyPatch,
    state: bytes,
    expected: RdmaState,
) -> None:
    import subprocess
    from contextlib import nullcontext

    from coire_node.metrics import MetricsCollector

    monkeypatch.setattr("coire_node.metrics.shutil.which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(
        "coire_node.metrics.socket.create_connection", lambda *_a, **_k: nullcontext()
    )

    def probe(argv: list[str], *, timeout: float) -> subprocess.CompletedProcess[bytes]:
        assert argv == ["/usr/bin/rdma_ctl", "status"] and timeout == 0.5
        return subprocess.CompletedProcess(argv, 0, state, b"")

    monkeypatch.setattr("coire_node.metrics.run_probe", probe)
    collector = MetricsCollector(
        node_name="coire-edge-a",
        agent_version="test",
        interval_s=2,
        budget_cpu_pct=1,
        budget_rss_bytes=32 * 1024**2,
    )
    result = collector.data_link_status()
    assert result.ip_state is LinkState.UP
    assert result.rdma_state is expected


@pytest.mark.parametrize(
    "fault", ["rank_loss", "replacement", "link_loss", "wrong_challenge", "peer_timeout"]
)
async def test_all_rank_proof_loss_interrupts_stream_within_three_seconds(
    tmp_path: Path,
    fault: str,
) -> None:
    names = ("coire-core", "coire-edge-a", "coire-edge-b")
    private = {
        name: base64.b64encode(Ed25519PrivateKey.generate().private_bytes_raw()).decode()
        for name in names
    }
    target = InferenceTarget(model_id=uuid4(), variant_id=uuid4(), base_manifest_sha256="a" * 64)
    now = datetime.now(UTC)
    group = ShardGroupStatus(
        group_id=uuid4(),
        instance_id=uuid4(),
        command_id=uuid4(),
        target=target,
        mode=ShardingMode.TENSOR_PARALLEL,
        state=ShardGroupState.READY,
        started_at=now,
        ranks=[
            ShardRank(
                rank=i,
                node_name=name,
                host=f"{name}.fabric",
                port=9600,
                pid=100 + i,
                process_create_time=1000 + i,
            )
            for i, name in enumerate(names[1:])
        ],
    )
    placement = sharded_placement(dict.fromkeys(names[1:], group))
    assert placement is not None
    model = FailoverModel(
        id=target.model_id,
        slug="tiny-group",
        display_name="Tiny",
        context_window=2048,
        capabilities=frozenset({"text_chat"}),
        placement=placement,
    )
    snapshot = FailoverSnapshot(
        snapshot_id=uuid4(),
        issued_at=now,
        expires_at=now + timedelta(seconds=120),
        models=[model],
        membership=FailoverMembershipConfig(
            epoch=1,
            signing_key_id="test",
            members=[
                FailoverMember(name=name, priority=i, public_key=public_key_b64(private[name]))
                for i, name in enumerate(names)
            ],
        ),
        access_verifier=FailoverAccessVerifier(
            issuer="https://access.fixture", audience="test", jwks_url="https://access.fixture/keys"
        ),
        signature="isolated-residency-test",
    )
    broken = False

    def measure() -> StudioDataLinkStatus:
        return StudioDataLinkStatus(
            node_a=names[1],
            node_b=names[2],
            measured_at=datetime.now(UTC),
            ip_state=LinkState.DOWN if broken and fault == "link_loss" else LinkState.UP,
            rdma_state=RdmaState.UP,
        )

    native: dict[str, Residency] = {}
    for i, name in enumerate(names[1:]):
        settings = Settings(
            _secrets_dir="/nonexistent",
            node_name=name,
            failover_member_name=name,
            failover_peer_key=SecretStr(private[name]),
        )  # type: ignore[call-arg]
        store = Store(str(tmp_path / name))
        groups = Mock(spec=ShardGroupManager)
        groups.get.return_value = group

        def local_rank(_group: object, rank: ShardRank = group.ranks[i]) -> ShardRank | None:
            if broken and fault == "rank_loss":
                return None
            return (
                rank.model_copy(update={"process_create_time": 2000})
                if broken and fault == "replacement"
                else rank
            )

        groups.local_rank.side_effect = local_rank
        native[name] = Residency(settings, Mock(spec=EngineManager), store, groups, measure)

    observed_epochs: list[int] = []

    async def peer(request: httpx.Request) -> httpx.Response:
        if broken and fault == "peer_timeout":
            raise httpx.ReadTimeout("isolated partition")
        probe = FailoverRankProbe.model_validate_json(request.content)
        observed_epochs.append(probe.epoch)
        if broken and fault == "wrong_challenge":
            probe = probe.model_copy(update={"challenge": uuid4()})
        result = await native[names[2]].observe(probe)
        return (
            httpx.Response(503)
            if result is None
            else httpx.Response(200, json=result.model_dump(mode="json"))
        )

    released = Mock()
    cancelled = asyncio.Event()
    async with httpx.AsyncClient(transport=httpx.MockTransport(peer)) as client:

        async def allowed() -> bool:
            return await native[names[1]].current(model, snapshot, client)

        assert await allowed()
        rotated = snapshot.model_copy(
            update={"membership": snapshot.membership.model_copy(update={"epoch": 2})}
        )
        assert await native[names[1]].current(model, rotated, client)
        assert observed_epochs == [1, 2], "membership changes must invalidate cached rank proofs"

        async def chunks() -> AsyncIterator[bytes]:
            nonlocal broken
            try:
                yield b"data: first\n\n"
                broken = True
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        async def work() -> StreamingResponse:
            return StreamingResponse(chunks())

        response = await bounded_response(work(), allowed, released)
        assert isinstance(response, StreamingResponse)
        started = time.monotonic()
        async with asyncio.timeout(3):
            output = [chunk async for chunk in response.body_iterator]
        assert time.monotonic() - started < 3
        assert b"coire_interrupted" in output[-1]
        assert cancelled.is_set()
        released.assert_called_once()
