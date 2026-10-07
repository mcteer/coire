# Feature 016 — private adapter smoke routing evidence

Scope: `coire_api/gateway/targets.py`, `coire_scheduler/placement.py`,
`coire_api/instance/service.py`, focused tests. Constitution I, IV, V and VII.

## Exact caller integration

1. `TrainingRuntime.prepare_adapter(adapter_id)` in `training/runtime.py` mirrors
   independently verified artifact copies, rechecks current authority, and persists
   `ModelInstanceRow(id=uuid.uuid5(adapter_id, "validation-smoke"),
   model_id=adapter.model_id, variant_id=adapter.base_variant_id,
   adapter_id=adapter.id, policy="single:auto")`. It records the same ID as
   `adapter.metadata_record["smoke_instance_id"]` and appends the initial transition.
2. Existing `coire_scheduler.instances.execute_instance_launch` creates the persisted
   placement decision. Both `_candidate_nodes` and load-command construction in
   `coire_scheduler.placement` now call `resolve_instance_target(session, instance)`.
   For a `validating`/`replicating` candidate, that delegates to the separate
   `resolve_validation_target(session, instance.id)`. It accepts no public selector,
   principal override, or generic permission flag.
3. The validation resolver requires training enabled (configuration remains default
   off), the deterministic instance and metadata binding, private/unverified candidate,
   current human-admin/personal-key authority, retained committed checkpoint,
   canonical checkpoint/adapter manifests, frozen resolved spec/base/runtime lineage,
   and two verified complete copies of each artifact. Automatic finalization also
   requires the current finalizing fence, final update/output slug, and stopped attempt.
   Candidate state/authority are refreshed under the job/base/adapter locks.
4. Placement filters exact verified base/adapter copies, uses the instance-scoped
   counted reservation, and refuses private smoke alongside counted training/image
   holds. Load payload contains the exact `InferenceTarget` with both manifests.
   No base/smaller-target fallback is introduced. Missing/mismatched node target
   acknowledgement leaves the load uncertain and its hold counted; dispatched adapter
   loads are retained for reconciliation on placement errors.
5. Existing `placement/executor.py` starts the node-owned bare engine and polls until
   node `EngineState.READY`, checking `status.target == target`. Node readiness is the
   existing actual-generation probe, not merely `/health`. Adapter instance transition
   to READY additionally requires one exact engine/member and a HELD reservation whose
   holder is that instance, never a parent-model reservation.
6. `TrainingRuntime.prepare_adapter` observes the READY member via authenticated
   `client.get_engine(node_name, engine_id)` and returns `(instance_id, EngineStatus)`.
   Existing `finalize_serving_adapter` checks current authority/fence, both adapter
   copies, exact target digests, actual process identity, fresh health and HELD reservation
   before changing the adapter to `ready`, `admin_only`, `verified=False`.
   `resolve_target` stays ready-only for ordinary public/user/admin/run inference.

## Verification boundary

`test_private_adapter_smoke.py` uses disposable PostgreSQL and actual runtime,
stage/finalization reducers, resolver, placement policy/command construction, and
instance transitions/projection. Node verification/load/readiness receipts are simulated;
this is control-plane integration evidence, not a measured Studio/bare-engine acceptance.
The broader feature's measured admission and hardware release gates remain in its
execution record. No production configuration or model/Metal workload was changed.

Coverage includes both validating and replicating candidates; exact manifests in load
commands and final receipts; public/admin/run-token refusal before finalization;
forged IDs, changed metadata, revoked admin, changed base digest, wrong artifact byte
count, purged checkpoint, cancellation, and default-off refusal; instance-only holds;
and retained counted holds when a node acknowledges a different adapter manifest.

Two existing synthetic runtime tests were corrected to mark their trainer stopped and
release its training reservation before inserting a smoke reservation, matching the
existing reverse-admission guard rather than bypassing it.

## Commands/results

Executed 2026-10-05 on the development workspace:

```sh
COIRE_INTEGRATION=1 uv run pytest -q \
  apps/coire-api/tests/integration/test_private_adapter_smoke.py \
  apps/coire-api/tests/integration/test_training_runtime.py \
  tests/integration/test_exact_adapter_resolution.py \
  tests/integration/test_training_targets.py \
  apps/coire-api/tests/unit/test_placement_policy.py \
  apps/coire-api/tests/unit/test_instance_lifecycle.py \
  apps/coire-api/tests/unit/test_gateway_resolution.py \
  apps/coire-api/tests/unit/test_gateway_loading.py \
  apps/coire-api/tests/unit/test_gateway_execution.py \
  apps/coire-api/tests/contract/test_adapter_targets.py
```

**92 passed, zero failed/skipped, 93.61 s.** The new private smoke file contributes
12 cases. The 32 warnings concern absent development secrets directories.

```sh
uv run ruff format \
  apps/coire-api/src/coire_api/gateway/targets.py \
  apps/coire-api/src/coire_api/instance/service.py \
  apps/coire-api/src/coire_scheduler/placement.py \
  apps/coire-api/tests/integration/test_private_adapter_smoke.py \
  apps/coire-api/tests/integration/test_training_runtime.py
uv run ruff check \
  apps/coire-api/src/coire_api/gateway/targets.py \
  apps/coire-api/src/coire_api/instance/service.py \
  apps/coire-api/src/coire_scheduler/placement.py \
  apps/coire-api/tests/integration/test_private_adapter_smoke.py \
  apps/coire-api/tests/integration/test_training_runtime.py
uv run mypy \
  apps/coire-api/src/coire_api/gateway/targets.py \
  apps/coire-api/src/coire_api/instance/service.py \
  apps/coire-api/src/coire_scheduler/placement.py
git diff --check
```

Ruff: all checks passed. Mypy: no issues in all three owned source files.
Whitespace check passed. No commit was created.
