# Quickstart: Placement Scheduler and Auto-Unload

## Local gates

```bash
uv run ruff format --check && uv run ruff check
uv run mypy apps/ packages/
uv run pytest -q -m "not integration and not engine"
uv run python -m coire_api.openapi --check
```

## Composed policy scenarios

```bash
COIRE_INTEGRATION=1 uv run pytest -q tests/integration/test_placement_scheduler.py
```

The suite uses artificial small budgets and fake engines to prove exact-fit admission, Studio A
preference/B fallback, LRU eviction, busy skipping and bounded refusal, pin immunity, concurrent
serialization, TTL unload, restart recovery, and the standing sandbox reservation.

## Real cluster

1. Confirm both nodes report fresh healthy samples and no swap.
2. Set a bounded test budget above the sandbox slice, load two ≤1 GB variants, and generate traffic
   to make their LRU order unambiguous.
3. Request a third variant and record the eviction, reservation-before-load ordering, and ledger.
4. Pin the survivor, expire its TTL, and confirm it remains; unpin and confirm the next loop unloads.
5. Compare reserved versus measured resident bytes and capture the dashboard/alert evidence.
6. Restore production budgets and verify both nodes still report no swap.

Rollback: stop new placements, let active workflows settle, restore the previous API/scheduler
images, and leave the additive ledger tables intact. Existing engines remain node-owned and usable.
