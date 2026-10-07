"""Exact evidence multiplicity and live per-target latency boundaries."""

from datetime import UTC, datetime, timedelta

from coire_scheduler.training_guard import latency_reason, protective_reason


def test_latency_requires_thirty_fresh_trailing_window_samples() -> None:
    now = datetime.now(UTC)
    assert latency_reason([(now, 2.0)] * 29, observed_at=now, now=now) == "insufficient_samples"
    assert latency_reason([(now, 2.0)] * 30, observed_at=now, now=now) == "latency_breach"
    assert (
        latency_reason([(now, 1.5)] * 30, observed_at=now - timedelta(seconds=60), now=now) is None
    )
    assert (
        latency_reason(
            [(now, 1.0)] * 30, observed_at=now - timedelta(seconds=60, microseconds=1), now=now
        )
        == "insufficient_samples"
    )
    assert (
        latency_reason(
            [(now - timedelta(minutes=5, microseconds=1), 2.0)] * 30, observed_at=now, now=now
        )
        == "insufficient_samples"
    )


def test_missing_latency_is_not_a_fabricated_running_breach() -> None:
    assert (
        protective_reason(
            footprint_bytes=None,
            reservation_bytes=100,
            swap_growth_bytes=0,
            thermal_state=None,
            latency_reasons=["insufficient_samples"],
        )
        is None
    )
    assert (
        protective_reason(
            footprint_bytes=101,
            reservation_bytes=100,
            swap_growth_bytes=0,
            thermal_state="nominal",
            latency_reasons=[],
        )
        == "memory_breach"
    )
    assert (
        protective_reason(
            footprint_bytes=10,
            reservation_bytes=100,
            swap_growth_bytes=1,
            thermal_state="nominal",
            latency_reasons=[],
        )
        == "memory_breach"
    )
    assert (
        protective_reason(
            footprint_bytes=10,
            reservation_bytes=100,
            swap_growth_bytes=0,
            thermal_state="serious",
            latency_reasons=[],
        )
        == "thermal_breach"
    )
