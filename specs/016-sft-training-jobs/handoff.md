# Feature 016 handoff — complete

## Spec and scope

- Spec: [spec.md](spec.md); implementation on `feat/016-sft-training-jobs`.
- [x] Plan and tasks reviewed; FR-001–FR-036 and SC-001–SC-012 reviewed against the mapped tasks.
- [x] User-visible behavior and rollback path documented in [the runbook](../../docs/runbooks/sft-training.md).

SFT supports single-Studio LoRA, QLoRA and DoRA, fixed two-Studio QLoRA, full-state
checkpoint recovery, mirrored artifacts, private exact-adapter inference and independent
write verification. Mixed chat/training requires an exact passing profile. Two-node dense
LoRA/DoRA remain unmeasured and cannot be admitted without their own evidence.

Final release: complete. Immutable image policy/scan/SBOM, fresh isolated metadata restore and temporary credential revocation passed.
Final proof: `~/.coire/projects/coire/releases/016-finish-20261006/evidence/qualified-release-final.json`. All final image gates, source equivalence, authenticated health and isolated metadata restore passed. Temporary acceptance credentials are revoked.

## Validation

- [x] Unit and contract tests: **2,601 passed**, 408 excluded integration/engine cases;
  the broad local selection has two skipped cases. Relevant real Studio gates below
  passed without skips. No failing check is accepted.
- [x] Isolated Postgres integration: **162 passed**, no skips, 193.25 seconds.
- [x] Real Studio native runtime: **23 passed**, no skips; separate SDK entrypoint tests passed.
- [x] Ruff format/check: **1,522 files**; strict mypy: **806 source files**.
- [x] OpenAPI freshness and byte-identical regenerated TypeScript schema.
- [x] Web: **171 tests passed**; lint and production build passed; updated runbook renderer: **2 passed**.
- [x] Training Prometheus rule tests passed in the hardened image with its required temporary filesystem.
- [x] Final production image policy/scans/SBOM and metadata preservation

The broad run initially exposed two SDK test-process isolation errors: API test setup's
`OTEL_SDK_DISABLED=true` propagated into export-test children. Explicit SDK activation
in those in-memory test children passed with the parent still disabled, and the complete
suite then passed. Four new test typing errors were also corrected without weakening
checks. Production telemetry settings were not loosened.

Live recipe/form equivalence, single-node parameterizations, held-out evaluation,
three exact full-state reboot comparisons, API/scheduler/node recovery, both rank faults,
physical link interruption and transfer expiry/refresh/interruption/corruption passed.
A verified 1.5B adapter passed all four actual harness evaluations and Studio-owned MCP
apply in a disposable workspace. Failed tiny-adapter quality evaluation remains failed;
its write gate was not bypassed. Dataset, publication, authority and exact-selector
negative cases passed.

Frozen qualification `fe31b6b3-a52d-4f9c-b9a8-8eaa30c1bbb1` completed **16,384 updates**,
zero swap growth and both full 15-minute windows with **180 completions per target per
phase, zero failures**. Base/adapter p95: baseline **0.533/0.538 seconds**; mixed
**0.473/0.487 seconds**. Profile `dcc68261-a9ce-46e9-b59d-cbd894bc8dff` retains exact
configuration/resident identities and seven-day expiry. Prior failed/inconclusive reports
remain unchanged. Ordinary mixed admission waited for fresh samples, then ran 25 updates;
cancellation took **1.653 seconds**.

Injected thermal/memory/latency observations in separate metadata copies drove real
scoped protective stops in **1.857/2.329/2.314 seconds**. Stale observation produced no
false confirmed breach. Every test job ended cancelled with no adapter and zero counted
holds. Synthetic conditions were never written into production telemetry or profiles.
Both image/train admission directions and newer-pin-aware reload passed.

## Dependencies and licences

| Dependency | Version | Reason | Licence |
| --- | --- | --- | --- |
| PyYAML | 6.0.3, existing lock | Direct bounded recipe parsing declaration | MIT |
| safetensors | 0.8.0 | Allocation-free checkpoint validation; synthetic CPU format tests | Apache-2.0 |
| NumPy, development only | 2.5.2, existing lock | Synthetic CPU checkpoint fixtures | BSD-3-Clause |
| @types/node, build only | 22.18.8 | Checked-in operational runbook embedding | MIT |
| undici-types, transitive build only | 6.21.0 | @types/node declarations | MIT |

No new telemetry SDK, inference wrapper or engine version was introduced by the final corrections.

## Constitution check

- **I. Bare engines:** node-owned bare MLX/mlx-lm execution; no inference wrapper.
- **II / II-a:** all model and user-harness work stays on Studios; Core services and
  Studio harness/relay images retain separate non-root hardened containers.
- **III. Contracts first:** shared Pydantic wire types; generated OpenAPI/TypeScript;
  exact target identities preserved across inference, evaluations and run tokens.
- **IV. Zero implicit trust:** scoped authenticated controls, audited mutations,
  process-birth/fence ownership and counted uncertainty. Temporary credentials stay
  in a private explicit Keychain. The bounded 4.538-second credential-recovery exception
  is closed and documented in [ADR 0013](../../docs/adr/0013-bounded-development-credential-recovery.md).
- **V. Registry assets:** all live models were already admin-acquired and verified;
  training never acquires weights or executes dataset code.
- **VI. Observability:** native worker/analysis/mixed stage traces verified in Tempo;
  durable state/audit and real stall/protection alerts passed with diagnostics off.
  Tempo's observed OOM loop was fixed with bounded memory; zero new restarts afterward.
- **VII. Spec-driven:** spec/plan/task coverage and independent runtime gates passed;
  final release gates passed and all feature tasks are complete.

## Operational evidence

- [x] Runbook and ADR updated.
- [x] Dashboard and alert updated and tested.
- [x] Real-cluster evidence recorded in [execution-record.md](execution-record.md).
- [x] Secrets, tensors, datasets and generated images excluded from Git.

Compatible API/Studio-A rollback and restoration passed in **15.091/14.488 seconds**
with schema retained, authenticated history, checkpoint verification and base/exact-adapter
chat. Disabling admission drained the owned trainer in **0.200 seconds** from durable
cancelling state, retained mirrored update 4 and released all holds. Complete Studio
stores restored with every file digest equal: A **146/146** and B **140/140** complete
manifests verified. Metadata/dataset restores and migration refusal/clean isolated round-trip gates passed; a fresh final metadata snapshot was restored in isolation.

Original Anthropic ops settings are restored. Temporary Studio-B residents were drained
through audited APIs, including removal of the temporary base pin. Both Studios run
`0.2.0-d6a27507f84e` with the tested digest-pinned user harness and relay images. Training
admission is enabled for development after all final gates passed. Configuration defaults remain off. No commit, push
or PR was created. Preserve the existing broad working tree.

Private evidence: `~/.coire/projects/coire/releases/016-finish-20261006/evidence/`.
