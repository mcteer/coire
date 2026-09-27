# Quickstart: Agent Harness

## Local gates

```bash
uv sync --all-packages
uv run pytest -q -m "not integration and not engine"
uv run coire eval harness --help
```

Verify native, JSON, and delimited strategies; malformed-call retries; structured repair; reasoning
extraction; context summarisation fallback; append-only history; and unverified-write refusal.

## Composed gateway test

```bash
COIRE_INTEGRATION=1 uv run pytest -q tests/integration/test_agent_harness.py
```

The deterministic gateway double must complete read tasks through two strategies, reject every
unverified write before invocation, persist two evaluation reruns, and keep the user image free of
the admin client.

## Studio evaluation

Run `coire eval harness <variant-id>` for three ≤1 GB/open-weight variants spanning native and
text-only behavior. Record scorecard IDs and confirm only passing exact variants become verified.

## Rollback

Stop new runs, keep append-only evaluation rows, restore both harness images and API together, and
clear no verification flags manually. Previous scorecards remain readable.
