# Feature 016 gateway measurement evidence

Date: 2026-10-05. Branch: `feat/016-sft-training-jobs`.

## Implemented boundary

- `ResolvedModel.instance_id` is optional for compatibility and is populated from the actual
  selected single-node or sharded instance. Internal `resolve_model(..., instance_id=...)`
  filters both placement queries and refuses unavailable fixed instances instead of falling
  back to another instance, legacy engine, or external provider.
- Shared stream accounting observes the first complete SSE frame with non-empty string
  `delta.content`. Keepalives, role-only/empty deltas, usage-only events and terminators do not
  establish TTFT. Timing uses the request clock and the arrival time of the content-bearing
  chunk; the proxy's `first_chunk_at` is an upstream-line observation, not a token timestamp.
- Once-only usage stores actual instance, first-content timestamp, TTFT and reported token
  counts. Postgres `ON CONFLICT (request_id) DO NOTHING RETURNING id` gates both insertion and
  credential/provider settlement; shielded persistence survives caller cancellation.
- `coire_api.training.gateway_measurements.gateway_measurement_generate(settings)` supplies
  the production callback signature expected by `GatewayWorkloadDriver`. It checks current
  human-admin/key authority, owner, audited request digest, frozen prompt metadata and output
  bounds, complete resident identity set, healthy exact resident members and dedicated active
  measurement holds. Personal keys retain rate/quota checks. Generated content is discarded.
- Private task-local context reaches two small proxy hooks for admission and lease renewal.
  These reauthorize under the existing ordered node locks. Standard `acquire_lease` and its
  ordinary training guard still run; no unchecked public endpoint or mixed-guard bypass exists.
  Stream consumption rechecks authority and closes the upstream generator on refusal/error.

Caller integration, owned by the main executor workstream:

```python
from coire_api.training.gateway_measurements import gateway_measurement_generate

workload = GatewayWorkloadDriver(gateway_measurement_generate(settings))
```

No shared wire model was changed by this workstream. The callback consumes and returns existing
strict shared training contracts; persisted fields use the ORM/migration owned by main.

## Actual verification commands

```bash
uv run pytest -q apps/coire-api/tests/unit/test_gateway_execution.py apps/coire-api/tests/unit/test_gateway_usage.py apps/coire-api/tests/unit/test_chat_streaming.py apps/coire-api/tests/unit/test_training_guard.py apps/coire-api/tests/unit/test_training_measurements.py apps/coire-api/tests/contract/test_gateway_v1.py apps/coire-api/tests/contract/test_adapter_targets.py tests/integration/test_exact_adapter_resolution.py
```

Result: **102 passed**, 54 local missing-secret-directory warnings, 13.87 seconds.

```bash
COIRE_INTEGRATION=1 uv run pytest -q apps/coire-api/tests/integration/test_gateway_measurement_usage.py
```

Result after adding cancellation and nearest-rank SQL assertions: **1 passed**, one local
missing-secret-directory warning, 4.94 seconds. The fixture uses disposable loopback-only
Postgres 17 and authenticated HTTP MockTransport; it performs no inference or Studio traffic.
Initial fixture attempts correctly hit the reverse admission fence and rejected an unvalidated
variant; fixture construction was corrected without changing those production gates.

The Postgres test verifies:

1. Thirty full gateway streams bind the requested instance despite a newer same-model instance.
2. Exact per-instance Studio token metadata is checked against reported usage.
3. Twenty-nine samples are insufficient; thirty fresh samples qualify. The other resident
   instance remains undersampled, proving that model-level samples cannot leak across instances.
4. Exactly 60 seconds of freshness qualifies; 60 seconds plus one microsecond is insufficient.
5. Nearest-rank p95 at 1500 ms qualifies; two 1501 ms samples among thirty cause a breach.
6. Every recorded success has persisted instance/first-content timing and every lease releases.
7. An unknown fixed instance and an unfrozen prompt fail closed; a disabled administrator cannot
   initiate another upstream call.
8. Concurrent writes for one request insert one usage row; cancellation while the persistence
   task is waiting still leaves one committed row.

```bash
uv run mypy apps/coire-api/src/coire_api/training/gateway_measurements.py apps/coire-api/src/coire_api/gateway/{usage,execution,resolution,proxy}.py
uv run ruff format apps/coire-api/src/coire_api/gateway/{usage,execution,resolution,proxy}.py apps/coire-api/src/coire_api/training/gateway_measurements.py apps/coire-api/tests/unit/test_gateway_execution.py apps/coire-api/tests/integration/test_gateway_measurement_usage.py
uv run ruff check apps/coire-api/src/coire_api/gateway/{usage,execution,resolution,proxy}.py apps/coire-api/src/coire_api/training/gateway_measurements.py apps/coire-api/tests/unit/test_gateway_execution.py apps/coire-api/tests/integration/test_gateway_measurement_usage.py
git diff --check -- apps/coire-api/src/coire_api/gateway/usage.py apps/coire-api/src/coire_api/gateway/execution.py apps/coire-api/src/coire_api/gateway/resolution.py apps/coire-api/src/coire_api/gateway/proxy.py apps/coire-api/tests/unit/test_gateway_execution.py
```

Results: strict mypy passed for five source files; Ruff passed; whitespace check passed.

Final targeted rerun after aligning the callback's accounting start timestamp with its request
clock and refusing fixed-instance provider resolution:

```bash
COIRE_INTEGRATION=1 uv run pytest -q apps/coire-api/tests/unit/test_gateway_execution.py apps/coire-api/tests/unit/test_gateway_usage.py apps/coire-api/tests/integration/test_gateway_measurement_usage.py
```

Result: **20 passed**, one local missing-secret-directory warning, 5.52 seconds. The source Ruff
and strict mypy commands above also passed again.

## Acceptance scope

This establishes gateway transport, authorization and Postgres latency evidence primitives.
Synthetic prompt counts/transport timings are not Studio-tokenization or 15-minute coexistence
acceptance. Full baseline/mixed workload and hardware evidence remain the executor/Studio gates
in `tasks.md`. Constitution compliance: III (existing typed contracts), IV (live scoped authority),
V (registry-only fixed-instance routing), VI (existing gateway/training telemetry), VII (real
transaction and boundary tests); no model, tokenizer or Metal execution on core (II).
