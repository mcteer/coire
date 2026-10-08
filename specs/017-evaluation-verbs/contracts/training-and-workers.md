# Training and Studio Worker Contracts

All transport types are Pydantic models in coire-core. The definitions here prescribe behavior; generated OpenAPI is an implementation artifact. Shared suite fixtures are immutable package data, never executable downloaded modules.

## Training intent version 2

Preserve schema version 1 exactly, including default serialization and resolved hash calculation. V2 keeps common training fields and adds declarations under evaluation:

```yaml
schema_version: 2
# model/data/parameterization/optim/output/placement/seed retain existing meanings
eval:
  loss_every_updates: 100
  at_end: true                 # held-out loss only, unchanged
  suites:
    - suite_id: task-coding-instructions
      suite_version: 1
      checkpoint_updates: [100, 200]
    - suite_id: judge-rubric
      suite_version: 1
      checkpoint_updates: [200]
```

This is a fragment, not a complete runnable training recipe. Every declared suite runs at successful final completion regardless of held-out-loss `at_end`. Updates must be unique, sorted, positive, below `optim.updates`, and divisible by `output.checkpoint_every_updates`. At most four suites and 32 distinct pre-final updates. Same suite/version appears once. Reject incompatible nodes, missing/retired suites, self-judge bindings, bad schedule, unavailable exact target metadata and impossible checkpoint/storage bounds during preflight. Capacity and runtime readiness are rechecked before execution. Failed/cancelled training does not invent a final adapter comparison. A full queue/evidence quota or disabled admission cannot roll back final training success: persist the trigger and its deadline, then admit child runs when permitted or record explicit per-suite failure on expiry. New v2 submissions require evaluation admission enabled.

`ResolvedTrainingSpecV2` freezes normalized v2 intent, original source/hash, existing base/data/runtime/resource identities, and each resolved suite/content/judge/settings fingerprint. Old `ResolvedTrainingSpec` embeds only v1 and remains byte-for-byte serializable as before. `TrainingJobDetail`, validation, measurement, node lease, prepare/resume/import/extraction and checkpoint paths must parse the correct document version. Frozen v1 fixtures prove no implicit conversion. Preflight negotiates node training/evaluation workload capability before sending v2; an old node never receives a speculative payload.

## Committed checkpoint pause decision

Extend the existing fenced checkpoint-commit response with a versioned v2 variant containing `checkpoint_id`, `manifest_sha256`, `committed_update`, `job_version`, `attempt_id`, `fence`, and optional `{trigger_id,pause_command_id,pause_origin:"evaluation"}`. It is returned only after both full checkpoint copies independently verify and trigger/pin/pause intent commit atomically. V1 acknowledgments stay unchanged.

The trainer consumes the decision at the acknowledged completed-update boundary. For two ranks, the existing common-rank control path broadcasts the same pause decision and proves every trainer stopped. Scheduler releases trainer holds only after stop evidence, extracts a mirrored private checkpoint adapter via 016 primitives, and starts evaluation. It never passes optimizer/full-state checkpoint directories as adapters to an engine.

On evaluation completion or failure, release only evaluation-owned work and pins after cleanup. Resume checks trigger pause ownership, latest job version/command, exact checkpoint identity, remaining training deadlines, active admin owner and fresh ordinary training admission/profile. A newer admin/protective pause/cancel prevents auto-resume. Boundary evidence persists through DBOS/node restarts. Retention uses the existing job lock/quotas, and cannot evict pinned input. An explicit forced retention exception is outside scope.

## Evaluation workspace and request dispatch

New authenticated node operation `POST /evaluations/workspaces` accepts `EvaluationWorkspacePrepare`: workload version 1, evaluation/attempt/agent-run IDs, fence, installed suite digest, exact phase target, read-only input manifest and bounded grant-backed prior-output/dataset references. Scheduler-authored inputs only; no arbitrary host path, argv, repository URL or caller code. Maximum manifest/body 256 KiB; content is transferred separately with size/digest checks. Response `EvaluationWorkspaceReceipt` binds opaque workspace/output refs, node, actual installed suite/agent version and accepted digest. Existing run APIs own launch, status, kill, collect and removal; new workspace prepare is idempotent under run+fence and reports conflicts.

Node launch still uses only `-m coire_agent`. `__main__.py` accepts the legacy untagged `HarnessRunRequest` or a strictly tagged evaluation request; dispatch is by validated type, never a shell string. New runs carry an internal `purpose=evaluation` link that public user-run requests cannot set. Matching workspace receipt/DB record/grant is required for private or unverified target admission. Workload permissions are inference-only READ with no ordinary user tools. Fixed local fixture assertions may simulate edit application only inside their disposable scratch data.

Each generating/judging phase receives a single exact target token with no admin scopes, finite spend and lifetime ≤remaining run deadline. Token role/owner/entitlements are checked by the gateway; no external provider fallback or network egress is enabled. The judge sees bounded delimited candidate text and no tools. Installed scorer verifies fixed allowed assertions and never executes generated code. Suite/runtime mismatch fails before generation.

## Collection, grading and evidence

`EvaluationWorkerResult`: version, evaluation/attempt/run IDs, fence, phase, suite/content/cases/scorer digests, actual exact target/runtime identities, case results, timing/usage and bounded evidence manifest. Result cap is 8 MiB total; individual output ≤16 KiB, ≤4096 generated tokens, ≤32 cases. On partial failure, content-free reason plus completed case metadata remains separate from aggregate. The scheduler validates requested identity, complete case cardinality and declared scoring rule consistency before committing the result. Only validated harness category evidence reaches the existing exact verification writer.

Base/candidate/judge phases persist independently and reuse their collected evidence after restart. A completed phase is not regenerated because a collection acknowledgment was lost. Missing or tampered evidence is infrastructure failure; explicit rerun is a new execution. For pairwise, two presentations per case swap A/B order using stable case/seed ordering; store both verdicts. Malformed output has at most two retries per presentation within the original execution deadline; exhausted validation yields no aggregate.

Raw evidence is copied to private core storage with reserved quota, digest verification and expiry metadata; node workspace cleanup follows successful durable collection or cancellation. Later phases stage only necessary bounded data using existing authenticated node/control transfers. Dataset input access reuses 016 grants and consumes no admin credentials. Unavailable data gives an explicit contamination status rather than a clean result.

## Serving coexistence and cancellation

`EvaluationMeasurementRequest` binds suite/version, exact subject/judge targets, selected node/hardware/runtime, resident instance/target set, token/arrival/concurrency bounds, fixed prompt-set digest and baseline/mixed phase durations. Qualification uses at least 100 valid requests in each phase per resident, records failures rather than dropping failed samples, and requires TTFT ≤1.5s, gateway overhead ≤20ms p95, zero swap growth/failures and healthy thermal/memory bounds. Gateway completion evidence is authenticated and instance-bound; an arbitrary client cannot self-report successful latency.

A current matching profile permits guarded mixed evaluation. Without one, use an unoccupied admissible node or visibly wait. Measurement itself is explicit controlled admin work with normal ledger limits and the same live abort guard. New workload/runtime/resident/hardware fingerprints invalidate prior profiles; no reuse of training-specific profiles.

Cancel/revocation kills the phase container through existing node APIs and revokes grants first; owned engine unload follows existing lease-aware lifecycle. Reachable kill target is five seconds. Unreachable resources stay reserved and cleanup unresolved. Disabling new evaluation admission never disables this recovery, kill, deadline, pin and evidence sweeper.
