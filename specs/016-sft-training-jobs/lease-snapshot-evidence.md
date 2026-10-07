# Feature 016 internal node lease snapshot evidence

Date: 2026-10-05. Branch: `feat/016-sft-training-jobs`.

## Production boundary

Implemented `GET /api/v1/internal/training/nodes/{node}/leases` in the already-mounted
`routes/internal_training.py`, backed by `training/lease_snapshots.py`. The existing dataset source
route and its runtime/measurement grant fallback remain intact.

The route requires a declared node bearer plus matching `X-Coire-Node` and path. It uses the existing
internal node-authentication middleware and repeats the scoped check at the route; no human/session
principal is required or accepted in place of the node credential. Responses use the existing shared
`NodeTrainingLeaseSnapshot` and `Cache-Control: no-store`.

The helper takes the node's shared transaction-scoped admission lock, then obtains `sampled_at` from
Postgres `clock_timestamp()`. Validity is five seconds; an observation already expired before helper
publication is refused. Counts cover every active node lease, including leases on reservations whose
state is no longer held. Modern instance ownership yields exact counts and explicit zeroes; current
members, sharded ranks, actual engines and retained counted model holds populate the inventory.
Unowned live engines and legacy/malformed model holders retain conservative nonzero UUID markers.
Other unbound active leases use their reservation UUID, so isolated probes' all-entry sum cannot
silently omit them. Ambiguous ownership and bounds overflow refuse instead of truncating.

No database or shared model source changes were needed. OpenAPI and generated TypeScript types were
regenerated. Operational behavior is documented in `docs/runbooks/sft-training.md`.

## Actual commands and results

```bash
COIRE_INTEGRATION=1 uv run pytest -q apps/coire-api/tests/integration/test_training_lease_snapshots.py
```

Final result after adding the actual native-reader round-trip, retained protected-instance zero and
historical-terminal membership exclusion:
**5 passed**, two local missing-secret-directory warnings, **6.46 seconds**. Tests use disposable
loopback-only Postgres 17 and in-process ASGI transport, with no Studio workloads.

Verified assertions:

- Missing, malformed, wrong-node, path/header-mismatched and human bearer credentials fail with 401.
  Current-user authentication is replaced by a test failure sentinel and is never invoked.
- Node A's active request does not appear in node B's snapshot. Sharded membership on both nodes
  appears with explicit zeroes; a resident instance represented only by its engine is also included.
- Expired/released leases are excluded. Two active leases yield two, not an instance-level boolean.
  Legacy model, orphan engine, malformed model holder and unbound non-model leases remain conservative;
  orphan engine plus legacy hold does not double-count actual leases.
- A counted hold retains its exact instance with zero requests after serving stops.
- The real `TrainingLeaseSnapshotReader.refresh()` consumes the production route response and reads
  per-instance and empty-scope counts. It refuses at the response's expiry boundary.
- A new-lease transaction winning the node lock blocks the snapshot until commit; the database sample
  timestamp is taken afterward and sees that lease. A snapshot transaction winning the lock blocks new
  lease insertion until snapshot commit; the following observation sees the new request.
- Exactly 256 current identities are supported; unprotected stopped/failed historical members do not
  consume that bound. A 257th current identity and ambiguous reservation ownership refuse.
- A caller's fake future timestamp header has no effect on the database observation.

```bash
uv run pytest -q apps/coire-node/tests/unit/test_training_lease_snapshot.py apps/coire-api/tests/contract/test_training_api.py apps/coire-api/tests/unit/test_training_authorization.py
```

Result: **30 passed**, 22 local missing-secret-directory warnings, **1.82 seconds**. This includes
native stale/future/wrong-node/long-validity/replay checks and existing training authorization contracts.

```bash
uv run ruff format apps/coire-api/src/coire_api/training/lease_snapshots.py apps/coire-api/src/coire_api/routes/internal_training.py apps/coire-api/tests/integration/test_training_lease_snapshots.py
uv run ruff check apps/coire-api/src/coire_api/training/lease_snapshots.py apps/coire-api/src/coire_api/routes/internal_training.py apps/coire-api/tests/integration/test_training_lease_snapshots.py
uv run mypy apps/coire-api/src/coire_api/training/lease_snapshots.py apps/coire-api/src/coire_api/routes/internal_training.py
uv run python -m coire_api.openapi
pnpm -C apps/coire-web exec openapi-typescript ../coire-api/openapi.json -o src/api/schema.d.ts
uv run python -m coire_api.openapi --check
git diff --check -- apps/coire-api/src/coire_api/routes/internal_training.py apps/coire-api/openapi.json apps/coire-web/src/api/schema.d.ts
```

Results: Ruff passed, strict mypy passed for both source files, API and TypeScript generation succeeded,
OpenAPI freshness passed, whitespace check passed. Constitution III/IV/VI/VII: existing typed contract,
scoped node authentication, structured observations and real transaction/transport tests.
