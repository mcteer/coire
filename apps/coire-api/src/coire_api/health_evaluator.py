"""Pure node-health evaluation with freshness and asymmetric hysteresis."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from coire_core.models.node import NodeStatus, Reachability, ThermalState
from coire_core.settings import Settings


@dataclass(frozen=True, slots=True)
class HealthDecision:
    verdict: Reachability
    reason: str | None
    failures: int
    successes: int
    degraded: int


def observation_reason(status: NodeStatus, latency_ms: float, settings: Settings) -> str | None:
    used = status.memory_total_bytes - status.memory_free_bytes
    memory_pct = used / status.memory_total_bytes * 100 if status.memory_total_bytes else 100.0
    if status.thermal_state in {ThermalState.SERIOUS, ThermalState.CRITICAL}:
        return f"thermal_{status.thermal_state.value}"
    if status.cpu_percent >= settings.node_degraded_cpu_pct:
        return "cpu_saturated"
    if memory_pct >= settings.node_degraded_memory_pct:
        return "memory_saturated"
    if latency_ms >= settings.node_degraded_latency_ms:
        return "heartbeat_slow"
    return None


def evaluate_probe(
    *,
    current: Reachability,
    status: NodeStatus | None,
    latency_ms: float | None,
    failures: int,
    successes: int,
    degraded: int,
    settings: Settings,
) -> HealthDecision:
    """Evaluate one probe; recovery deliberately takes longer than failure."""
    if status is None or latency_ms is None:
        failures += 1
        verdict = (
            Reachability.UNREACHABLE
            if failures >= settings.node_probe_failures_before_unreachable
            else current
        )
        return HealthDecision(verdict, "heartbeat_missing", failures, 0, 0)

    reason = observation_reason(status, latency_ms, settings)
    if reason is not None:
        degraded += 1
        verdict = (
            Reachability.DEGRADED
            if degraded >= settings.node_probe_degraded_before_transition
            else current
        )
        return HealthDecision(verdict, reason, 0, 0, degraded)

    successes += 1
    recovery_needed = current in {Reachability.DEGRADED, Reachability.UNREACHABLE}
    threshold = settings.node_probe_successes_before_recovery if recovery_needed else 1
    verdict = Reachability.HEALTHY if successes >= threshold else current
    return HealthDecision(verdict, None, 0, successes, 0)


def is_fresh(observed_at: datetime | None, now: datetime, settings: Settings) -> bool:
    return (
        observed_at is not None
        and (now - observed_at).total_seconds() <= settings.node_health_freshness_s
    )
