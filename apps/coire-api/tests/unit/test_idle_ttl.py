from __future__ import annotations

from datetime import UTC, datetime, timedelta

from coire_scheduler.placement import idle_eligible


def test_expired_unpinned_model_without_live_lease_is_eligible() -> None:
    now = datetime.now(UTC)
    assert idle_eligible(
        last_used_at=now - timedelta(seconds=61),
        ttl_seconds=60,
        pinned=False,
        in_flight=0,
        now=now,
    )


def test_active_or_stale_request_race_is_conservative() -> None:
    now = datetime.now(UTC)
    last_used = now - timedelta(seconds=61)
    assert not idle_eligible(
        last_used_at=last_used, ttl_seconds=60, pinned=False, in_flight=1, now=now
    )
    assert not idle_eligible(
        last_used_at=last_used, ttl_seconds=60, pinned=True, in_flight=0, now=now
    )


def test_each_model_uses_its_own_ttl() -> None:
    now = datetime.now(UTC)
    last_used = now - timedelta(seconds=90)
    assert idle_eligible(last_used_at=last_used, ttl_seconds=60, pinned=False, in_flight=0, now=now)
    assert not idle_eligible(
        last_used_at=last_used, ttl_seconds=120, pinned=False, in_flight=0, now=now
    )
