from datetime import UTC, datetime, timedelta

from coire_api.health_evaluator import evaluate_probe, is_fresh
from coire_core.models.node import NodePath, NodeStatus, Reachability, ThermalState
from coire_core.settings import Settings


def sample(
    *, cpu: float = 10, free: int = 80, thermal: ThermalState = ThermalState.NOMINAL
) -> NodeStatus:
    return NodeStatus(
        name="coire-edge-a",
        agent_version="test",
        uptime_seconds=1,
        cpu_percent=cpu,
        thermal_state=thermal,
        memory_total_bytes=100,
        memory_free_bytes=free,
        disk_total_bytes=100,
        disk_free_bytes=90,
        agent_cpu_percent=1,
        agent_rss_bytes=1024,
        collection_budget_ok=True,
        path=NodePath.MESH,
        sampled_at=datetime.now(UTC),
    )


def test_saturated_alive_node_becomes_degraded_after_hysteresis() -> None:
    settings = Settings()
    state = Reachability.HEALTHY
    count = 0
    for _ in range(3):
        decision = evaluate_probe(
            current=state,
            status=sample(cpu=99),
            latency_ms=10,
            failures=0,
            successes=0,
            degraded=count,
            settings=settings,
        )
        state, count = decision.verdict, decision.degraded
    assert state is Reachability.DEGRADED
    assert decision.reason == "cpu_saturated"


def test_failure_is_faster_than_recovery() -> None:
    settings = Settings()
    state = Reachability.HEALTHY
    failures = 0
    for _ in range(3):
        decision = evaluate_probe(
            current=state,
            status=None,
            latency_ms=None,
            failures=failures,
            successes=0,
            degraded=0,
            settings=settings,
        )
        state, failures = decision.verdict, decision.failures
    assert state is Reachability.UNREACHABLE
    successes = 0
    for _ in range(4):
        decision = evaluate_probe(
            current=state,
            status=sample(),
            latency_ms=10,
            failures=0,
            successes=successes,
            degraded=0,
            settings=settings,
        )
        state, successes = decision.verdict, decision.successes
    assert state is Reachability.UNREACHABLE
    decision = evaluate_probe(
        current=state,
        status=sample(),
        latency_ms=10,
        failures=0,
        successes=successes,
        degraded=0,
        settings=settings,
    )
    assert decision.verdict is Reachability.HEALTHY


def test_freshness_uses_control_plane_time() -> None:
    settings = Settings(node_health_freshness_s=30)
    now = datetime.now(UTC)
    assert is_fresh(now - timedelta(seconds=29), now, settings)
    assert not is_fresh(now - timedelta(seconds=31), now, settings)


def test_flapping_below_threshold_never_changes_placement_verdict() -> None:
    settings = Settings()
    state = Reachability.HEALTHY
    failures = successes = degraded = 0
    for failed in [True, False] * 6:
        decision = evaluate_probe(
            current=state,
            status=None if failed else sample(),
            latency_ms=None if failed else 10,
            failures=failures,
            successes=successes,
            degraded=degraded,
            settings=settings,
        )
        state = decision.verdict
        failures = decision.failures
        successes = decision.successes
        degraded = decision.degraded
        assert state is Reachability.HEALTHY
