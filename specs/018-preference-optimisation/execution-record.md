# Execution Record — Preference Optimisation and Feedback Capture

Branch: `feat/018-preference-optimisation`. Baseline: `d2e4bbf` (016/017/PR94 merged).
Status: implementation code and automated checks complete; release withheld at T074. Eight exclusive native train/serve cases, live recovery and drained rollback pass. Three sustained shared profiles pass; Studio A dense ORPO fails the gateway-overhead gate.

## Current release evidence

The table records the current admission-timing release checkpoint. T076 passes; T074 fails acceptance and remains unchecked. Final reconciliation and the draft PR record that blocker. The full 517-case integration checkpoint precedes the final content-free timing events; all nine affected real-Postgres cases were rerun on the current checked images.

| Gate | Result |
| --- | --- |
| CPU unit/contract suite | 3,114 passed; two existing optional skips |
| Disposable integration suite | 517 passed at the principal-identity checkpoint; nine affected cases pass again after content-free timing events; 39 existing optional skips, no required 018 skips |
| Static and generated contracts | Ruff, formatting (1,776 files), strict mypy (1,019 files), OpenAPI and dependency pins pass |
| Web | 228 tests, TypeScript and lint pass |
| Production images | All 16 ARM64 builds, policy checks, zero-CRITICAL scans and SPDX SBOMs pass |
| Observability | 14 alert files plus feedback/training alert tests pass |
| Exclusive native preference training/serving | All eight objective × parameterization × initialization cases pass |
| Recovery, cancellation and checkpoints | Actual restart/resume, ancestor retirement, cancellation, corruption, lease expiry and keep-three evidence pass |
| Feedback privacy and export | Actual captured feedback trains and serves; immediate withdrawal, physical purge and preservation of published data pass |
| Prior-release rollback | Drained old binaries serve the verified base on the additive schema; tested node/runtime restoration passes |
| Training/image exclusion | Valid image job remains queued without allocation while both native trainers run; cancellation passes |
| Shared chat/training | Three exact profiles accepted; Studio A dense ORPO **failed T074** and remains unqualified |
| Final reconciliation/PR | Reconciled for draft review; release remains blocked by T074 |

Raw transcripts, datasets, credentials, native checkpoint data and image/SBOM receipts remain outside Git in the private acceptance directory. The entries below distinguish required acceptance from synthetic fixtures and historical checkpoints.

### Resumed T074 investigation — 2026-10-10 UTC

The user's direction to continue authorizes further debugging. A failed acceptance task withholds release; it does not end this investigation. T074 remains unchecked.

- Live, deliberately nonqualifying admission profile `20a2e950-b6c1-450a-a767-02d56806870f` collected 60 seconds of Studio A baseline traffic. Its resident reached stopped, its owned key was revoked, and normal checked entrypoints and disabled training flags were restored. Python profiler totals across async/greenlet boundaries are unsuitable for attributing coroutine CPU time; they are not acceptance measurements.
- A metadata-only probe in the checked API container compared the same fresh authority/document-digest query, canonical node locks, fresh inventory query and commit against the live database. With 100 ms pauses, 80 warmed observations had total median **13.097 ms**, p95 **17.124 ms** and median thread CPU **4.336 ms**. Without those pauses, the corresponding figures were **2.051 ms**, **2.363 ms** and **0.484 ms**. Both inventories returned zero rows; these probes do not qualify chat or training. The timing difference warrants a streaming-cadence experiment, but does not establish a particular power-management cause. Host power mode remained automatic; no power, resource, network or durability settings changed.
- An isolated `pg_test_fsync` comparison found no faster durable replacement for the existing Linux default: one 8 KiB write measured approximately **397 µs** with `fdatasync` versus **904 µs** with `open_datasync`. The owned benchmark container and volume were removed. No PostgreSQL setting changed.
- A disposable projection experiment retained all inventory checks while replacing ORM metadata objects with tuple snapshots. Both existing user/API-key gateway cases passed. Across 120 observations per path, original/core median wall times were **3.025/2.954 ms**, and p95 **4.660/4.933 ms**. This was not a clear improvement and was not applied.
- The earlier authority/inventory batching prototype was also tested inside the checked API image, on disposable PostgreSQL. Across 120 observations per path, separate/combined median times were **1.616/1.856 ms** in a tight loop and **9.788/11.803 ms** with 100 ms pauses. Both gateway cases passed, but batching was slower and remains unapplied. No prototype function was installed in the live database.
- Short, nonqualifying streaming experiment `5575ad8c-57f1-414e-8e55-4dfee1fd70a8` used **500 ms arrival / four concurrent slots** on unchanged checked images. It completed 175 successful streams, with first-token p95 **440.149 ms**, successful duration p95 **464.998 ms**, and seven of 175 first-token observations above 20 ms. This short diagnostic does not satisfy T074. Two requests failed before the deliberate actor withdrawal, and a third finished after withdrawal. Queue traces identify both earlier failures as saturation; metadata windows show a previous stream still active at each refusal. The configured gateway limit is **one** in-flight request per engine, so the four-slot workload did not fit that limit. The limit remains unchanged. The resident reached stopped, the owned key was revoked, and normal disabled admission flags were restored.
- Dense ORPO qualification V57 was attempted on **Studio B**, beginning with its own 5,632-update memory measurement and then the full **900-second baseline + 900-second mixed** protocol at **2,000 ms arrival / concurrency one**. FR-006 requires single-Studio objective/parameterization support, and FR-007 requires exact measured configuration admission; neither assigns dense ORPO sharing to Studio A. Any accepted result will be bound to Studio B. Studio A's failed profiles remain unqualified. T074 stays unchecked until all sustained performance, native coverage, memory and stopped-proof evidence passes.

Private receipts: `admission-live-profile-rows.json`, `production-metadata-benchmark*.json`, `core-inventory-benchmark.jsonl`, `container-authority-benchmark*.jsonl`, `cadence-diagnostic-*.json`, `cadence-queue-*.json`, and `v57-qualification-controller.log` in the existing private acceptance directory.

## Prerequisites and intended matrix

This workspace is `coire-core.lab` (Apple Silicon). Core must not load models or execute a user harness. Native tests run through the isolated Mac CI environment and authorized Studio node workflows. Existing uv/Docker/pnpm tools are available. Credentials remain Keychain-backed; model and raw test artifacts stay outside Git.

Initial requested matrix: DPO and ORPO, dense LoRA and affine 4-bit/group-64 QLoRA, single Studio, bare and compatible SFT-adapter starts. Both-copy checkpoints, fresh objective profiles, numerical parity, cancellation, exact serving, evaluation obligations and coexistence require measured qualification. Unsupported or unmeasured configurations remain refused.

## Historical evidence ledger

The entries below preserve successive implementation checkpoints. Later dated
receipts supersede their pending statements; they do not erase failed attempts.

- Setup: six pre-018 v1/v2 intent/resolved/checkpoint/worker-state fixtures frozen; digest regression tests added. Existing profile-normalization tests retained.
- Contracts: strict feedback/provenance/export/lineage types; separate preference/v3 intent/resolved/resource/full-state/ack types. All 419 coire-core tests passed at the latest full core checkpoint, including frozen v1/v2 documents. Further gates remain pending.
- Database: frozen 0033 migration passed disposable-Postgres upgrade, seeded SFT/checkpoint/chat/evaluation history preservation, owner/active-pair uniqueness, generation check and drained downgrade/refusal. Immutable published-member and lineage triggers added.
- Privacy foundations: owner settings/origin/non-human credentials, live inactive/key refusal, immediate generation fencing, writer-versus-opt-out serialization, content-free fresh replay and physical source purge tests passed (4 targeted Postgres cases; 5 settings boundary contracts). Existing chat-delete contracts passed (4 cases) after extending their persistence fakes for the new locks. Expiry/cleanup and complete replay/export races still need further proof.
- Data foundations: preference upload/grouping/private API contracts passed (4 cases), paired rendering tests passed (2), paired analysis plus existing tokenizer/rendering regressions passed (26). Analysis uses bounded two-side/response summaries plus the token-row digest; per-row masks remain node-owned. Complete native token-cache/runtime evidence is pending.
- Static checkpoint: `uv run mypy apps/ packages/` passed all 953 checked source files after intentional legacy-runtime guards. New v3 work is not yet advertised by nodes. `uv run mypy` alone is not a valid repository invocation; CI uses the explicit targets.
- Feedback/comparison/export and user control: pending.
- Native preference numerical/runtime/restore/serve matrix: pending.
- Real Studio profiles/recovery/coexistence/rollback: pending.
- Complete local/CI/image/schema checks: pending.

No missing credential, asset or license blocker has been established. Determine prerequisites through the authorized workflow when the runtime reaches validation, rather than inventing a passing result or treating core's role as a Studio-testing prohibition.

### Foundation checkpoint

- `uv run pytest -q -m 'not integration and not engine'`: 2,904 passed, 1 failed, 2 existing optional skips, 547 deselected. The failure was the pre-018 assertion that ack v3 was unsupported. It now rejects version 4 and explicit v3 update/version tests were added; the six targeted ack tests pass. The complete suite must run again at release after remaining implementation.
- Added a scheduler-owned independent feedback cleanup worker, preserving cleanup while both admission flags are false. Seven cleanup/worker ownership tests pass. Five withdrawal/expiry/label erasure Postgres tests pass.
- Quota plus migration targeted tests: 2 passed; source bodies and releasing export staging remain charged until physical erasure/release proof. Source registration retains old private staging/orphan cleanup ownership and UUID paths.
- No native objective capability is advertised and no preference training execution has run. Feedback/comparison/export and training stories are still pending.

### Native provenance and thumb checkpoint

- Current thumbs support owner-only completed assistants, bounded current-state pages, clearing, optimistic versions, atomic content-free audit/command receipts and fresh privacy-safe replay. Four disposable-Postgres thumb contracts pass, including old-command refusal after disable/re-enable and replacement feedback on the same message. Thumbs create no comparison pair.
- Bare node serving reports verified inert tokenizer/template/package identities without loading weights or encoding tokens on core. Five identity tests cover acquired Jinja priority, registry override, checksum/link/unacquired/ambiguous refusals. Node engine/fake engine/exact adapter contracts plus migration/thumb checks passed (42 cases).
- Native cold load pins the exact resolved target and re-resolves current authority/artifact identity; three exact resolution tests and all native streaming tests pass. A mismatched artifact or missing adapter cannot fall back to the bare model.
- Native admission snapshots capture generation under owner-first locks and records a separate context revision. Prospective capture keeps bounded prompt bytes, exact serving target and tokenizer/template/runtime identity before generation; opt-out/re-enable cannot capture an old admission. Source seed remains explicitly null; regeneration will allocate its own recorded seed. Native non-reasoning text explicitly freezes `enable_thinking=false`; compatible `/v1` payload defaults remain unchanged.
- Updated one additive migration 0033 with nullable engine rendering metadata, separate chat context revision and required provenance runtime identity; seeded history upgrade/refusal/drained downgrade still passes.
- Latest focused provenance/chat/gateway/migration checks: 49 passed. Latest core checkpoint: 422 passed. Latest strict mypy checkpoint: all 961 files passed. Full release checks and all native preference training/real Studio qualification remain pending; no preference capability or training acceptance claimed.

### Explicit comparison checkpoint (partial US1)

- Explicit create/read/select/dismiss contracts are implemented separately from failed-turn retry. Admission checks live owner/key/role/entitlements, exact registry target availability, current capture generation, whole-prompt provenance, source/context freshness, one pending pair and counted storage. Retired exact sources return 503 before creating a pair.
- API-owned regeneration uses the existing authenticated gateway/node proxy and serving lease; freezes source sampling/template settings and a fresh seed; checks live generation and exact serving authority before every candidate write/terminal transition. Duplicate workers cannot claim another generation; one usage request identity is used. Full crash-accounting settlement proof is still outstanding.
- First selection installs a separate active-answer mapping; later judgement edits leave that choice and context revision unchanged. New sends and model changes refuse a pending comparison, while rename does not change context revision. Tests prove only the selected answer enters the next prompt.
- Persisted comparison SSE cursors contain metadata and candidate offsets, never copied answer text. Replay reconstructs a delta only from a currently eligible private body; after opt-out it emits a content-free tombstone. Reset snapshots and bounded feedback reads include pair metadata.
- Queued/running API execution carries a ten-second lease. Independent privacy cleanup reaps an expired lease even for inactive owners and with admissions disabled; source bodies and labels purge through the existing counted cleanup path.
- Latest feedback/provenance/comparison/withdrawal group: 20 passed. Earlier context/chat/feedback group: 28 passed. Replay/history group: 11 passed. Latest strict mypy: 964 files passed. New tests use fake proxy/registry outputs and inert asset fixtures; they are not native engine or Studio acceptance.
- Broad non-engine/non-integration checkpoint: 2,932 passed, 2 failed (OpenAPI freshness), 2 existing optional skips, 550 deselected. Regenerated OpenAPI/TypeScript, then all three targeted schema freshness checks passed. A later comparison quota test found that the global settings dependency ignored `create_app(settings)`; new feedback routes now use the API instance's settings and that contract passes. Full release checks remain pending after the remaining stories.

### Comparison accounting and export admission checkpoint

- Added a strict content-free accounting snapshot retained after copied feedback erasure. Terminal generation commits token counts with its lifecycle transition; recovery inserts usage under the existing unique request identity and then marks settlement. A disposable-Postgres crash test suppresses normal usage completion, erases feedback bodies and proves one recovered charge, with repeated settlement adding none.
- Distinct and byte-identical fake candidate generations both pass one-charge checks. Identical candidates immediately become non-exportable and their copied bodies are erased. Capture and first selection now require plain chat mode; whitespace-only answers cannot become eligible sources.
- Latest provenance/comparison/quota group: 14 passed, including coding-context capture refusal. Export effective-choice tests: 4 passed, covering owner precedence before tag/date filtering, half-open dates, withdrawn generations and thumb exclusion.
- Private export admission/history/detail/cancellation routes are implemented. Two disposable-Postgres contracts pass for immutable replay, mismatch conflict, default-off admission, same-origin enforcement and history/cancellation with both training flags disabled. Export dataset identity now derives from the authenticated immutable registration command, matching the existing dataset pipeline.
- Export selection streaming, publication revalidation, DBOS/restart recovery, UI, preference runtime and real-Studio acceptance remain outstanding. No export publication or native-training capability is claimed at this checkpoint.

### Private export publication checkpoint (partial US1)

- Selection counts metadata in PostgreSQL before reading bodies; above 10,000 matches returns the full match count and no truncated snapshot. Export details expose matched/selected/excluded counts and fixed pair/byte limits. The same additive 0033 migration now includes `matched_count`; seeded upgrade/drained downgrade passes.
- Staging reads one strict preference row at a time and records content-free member identities/hashes. Final registration rechecks live admin/key authority and deterministic selection under sorted owner, conversation, pair and judgement locks. The original five-minute deadline applies across at most three rebuilds.
- Disposable-Postgres publication tests prove one prompt group fails with `insufficient_groups`, two groups register an immutable private dataset with the small-sample warning, later opt-out preserves registered bytes/membership, withdrawal after staging prevents registration, and a lost final acknowledgement recovers committed success without deleting the source. These are inert database fixtures, not native generation acceptance.
- Scheduler dispatch and durable DBOS steps pass IDs and boolean status only. Durable exceptions suppress original source/database exception text; a fault test confirms content-free logged and raised recovery failures. Cleanup resolves registered dataset identity before deleting private files or releasing storage ownership; unresolved cleanup remains counted.
- Latest export selection/admission/publication/workflow group: 14 passed. New CLI export/exports/export-show/export-cancel commands use authenticated typed API requests. New and existing CLI tests: 99 passed. Generated feedback transport uses generated TypeScript shapes; `pnpm -C apps/coire-web exec tsc --noEmit` passes. Latest strict mypy: 974 files pass.
- Fake/custom engine command overrides now explicitly omit bare rendering provenance; the targeted fake-engine contract passes. Feedback and dataset-stage spans suppress exception text/status descriptions that could contain copied SQL parameters.
- Marked T021, T024, T025, T028, T029 and T030 complete based on the native dispatch/context/route/CLI/generated-transport evidence above. Export restart/concurrency/limits and full US1 acceptance remain under unchecked T020/T026/T027/T034. UI stories, v3 runtime and all required real-Studio/release gates remain outstanding.

### Native feedback UI and sampling checkpoint (partial US1 / US3)

Implemented persistent disclosure/settings, thumbs, explicit comparison controls, bounded export form/history, preference dataset warnings, and shared-observer privacy replacement. The full web suite passed 216 tests before the final sampling addition; lint and TypeScript checks passed. Server withdrawal/current-feedback checks passed 18 tests; strict workspace mypy passed for 975 files before the sampling addition.

Native `mlx_lm.server` defaults to greedy decoding. A fresh comparison seed alone therefore does not usually yield a distinct candidate. Added an explicit optional “Varied answers” control for new local text chat answers (temperature 0.7, top-p 0.95); default chat requests retain the existing engine defaults and idempotency serialization. Provenance freezes the exact admitted sampler; comparison generation preserves it and changes only its recorded seed. Native sampling schema/forwarding checks passed 27 tests, chat/provenance contracts passed 26 tests, and Chat UI passed 29 tests. These are local unit/contract checks, not real Studio or manual keyboard qualification.

Export recovery follow-up: Postgres publication checks passed 10 tests covering accepted admission disablement, file commit before database rollback, cancellation after staging, unknown publication reads retaining holds, concurrent one-active admission, fixed queue expiry, and at most three source-version rebuilds. Admission checks passed 5 tests including 100 queued exports and authorized replay while full. Changed upload reservation factoring retains new-request gate checks while permitting accepted durable exports to complete with live authority/quota enforcement. Export-history component checks passed 3 tests, including preserving manually loaded older history on refresh. Marked implementation tasks T032/T033/T059 complete; browser/manual privacy acceptance and native qualification remain unchecked.

### Export and paired-runtime foundation follow-up

The 20-pair real-Postgres acceptance fixture passed: 20 explicit oriented pairs, 20 immutable members, one private dataset registration on replay, exact unchanged source bytes, and whole-group split coverage. Native Studio analysis/readiness is still a separate required gate. Publication now writes a content-free `feedback.export.publish` audit atomically, and failed staged execution writes a service audit without contributor text. The 10 publication/recovery tests passed again after that change. Marked T018/T023/T026/T027 implementation complete based on route/usage/recovery evidence; remaining US1 acceptance and privacy gates stay open.

Started US2 math/data work. Independent scalar FP64 objective/analytic-gradient checks passed 6 tests, including finite differences, extreme negative log-probabilities and ORPO zero-weight behavior. Implemented pure tensor loss hooks with FP32 logits/reductions, shifted mask selection, detached DPO reference, stable ORPO log1mexp and pair aggregation; 14 native cases are written but not executed on core. Added deterministic complete-pair mixture sampling using the existing platform-independent ordering primitive; 4 sampler tests passed exact restarted batches/references/state, seed/content mismatch refusal, overlength atomic refusal, padding masks and global RNG preservation. Strict mypy passed 980 source files. T035/T036/T037 remain open pending complete runtime/native evidence and source compilation.

### Studio A numerical objective gate

14/14 native preference-loss cases passed, zero skips, 1.83 seconds, on `coire-edge-a.lab` using `/opt/coire/envs/current` with actual installed MLX 0.32.2 / mlx-lm 0.31.3. The isolated test source bundle SHA256 is `d7f1e8a077bbace330d02b52d6b548e0c3dcc45f498ab441d46178c7737cd902`; staged privately at `~/coire-stage/018-native-loss`. JUnit proof: `numerical.xml` there. The fixture verified the existing acquired ≤1 GiB Qwen2.5-Coder-0.5B 4-bit acceptance copy and adjacent checksum manifest offline; the numerical cases used synthetic tensor inputs rather than loading/training a model. Production node binaries, engine processes and model assets were unchanged.

Passed declared FP32-vs-independent-FP64 loss/logp tolerances (rtol1e-5/atol1e-5) and analytic-gradient tolerances (rtol1e-4/atol1e-5), extreme logps, response-only shifted masks with half-precision overflowing excluded positions, detached reference gradients, zero-weight ORPO, sum-vs-mean unequal lengths, pair aggregation and equal-sized microbatch equivalence. Marked T035/T036 complete. Native actual model initialization, resume, reference integrity, callbacks, end-to-end placement/serving and qualified resource profiles remain unproved and unadvertised. The core-only scalar/factory/sampler checks passed 11 tests; strict mypy passed 980 files.

### Initial-policy runtime and v3 checkpoint foundation (partial US2)

Current initialization source bundle SHA256: `51a8481fd3be42095d0069e801cfc82a50f94ea6b1b7ee371b0d7bddbe63594e`. Studio A passed 4 bare/parent × DPO/ORPO cases on affine QLoRA (6.88 seconds) and the same 4 on dense LoRA (14.52 seconds), zero skips. Existing acquired mirrored fixture paths were verified offline; artifacts created by the isolated tests stayed in private temporary directories. JUnit receipts: `~/coire-stage/018-native-loss/runtime-qlora.xml` and `runtime-lora.xml`. Initialization checks verify matching policy/reference outputs, independent model identity, zero flattened trainable reference tensors, preserved post-policy RNG, exact complete parent tensor/configuration loading and no ORPO reference. An initial 2/4 run failed a test's raw nested-container assertion; corrected it to check flattened tensor absence and made reference metadata reflect its frozen state. No failed run is counted as qualification.

Added version-aware worker-state parsing and single-node full-state checkpoint storage/restore for v3 alongside unchanged v1. New synthetic CPU v3 roundtrip plus old checkpoint/mixture/frozen serialization checks passed 21 tests; strict mypy passed 984 source files. Preference restore validates objective/options/exact initial/reference before restoring policy-only tensors, full optimizer, pair cursor and RNG. Real full-process trajectory checks and worker/probe/lifecycle wiring are still pending; T038/T039 remain open.


### Paired compilation and full-process resume qualification

T037–T039 are complete at the component boundary. Frozen preference compilation now validates source/split/analysis/tokenizer/template/runtime identity before streaming bounded selected rows, retaining whole prompt-group separation, both answer masks and explicit token-buffer accounting. Deterministic complete-pair sampling preserves the exact next-consumed cursor across epoch tails and refuses changed identities. Focused compilation/rendering/sampler/checkpoint tests: 12 passed on core using inert fixtures and a fake tokenizer.

Studio A ran the fresh-process resume matrix using acquired tiny dense and affine 4-bit fixtures with MLX 0.32.2 / mlx-lm 0.31.3: dense LoRA 4 passed in 71.41 s (`resume-lora.xml`), QLoRA 4 passed in 50.95 s (`resume-qlora.xml`), zero skips. Both objectives × bare/parent initialization pause at update 8 and resume in a fresh process; every update 9–16 compares loss, all policy adapter tensors, full optimizer tensors, exact sampler/RNG state and original frozen-reference fingerprint. Parent fixtures receive two actual SFT optimizer updates. Final source archive SHA256: `cefad2ecd4a36e6423b36aa1089bf6fdd7ef70b01c2eaf0a298f2c9b9b8f6b1d`. Receipts/source are private under `/Users/mcteer/coire-stage/018-native-loss` on Studio A, outside Git.

The first dense run had two DPO failures because NumPy cannot represent the BF16 reference probe buffer. The fingerprint now converts that probe to FP32 before NumPy; BF16 values are preserved and comparison tolerances were unchanged. Only the successful final matrix is counted. Isolated component testing does not establish scheduler placement, peer mirroring, serving or end-to-end training qualification.

### Preference metrics and bounded probe checkpoint

Distinct preference progress wire shapes carry objective, fence, pair counts and actual response-token counts; durable storage and replay refuse wrong objectives/fences and immutable conflicts. Two real-Postgres metric tests passed, including unchanged SFT replay. Strict mypy passed 988 files after the metric/probe additions.

Post-update probes evaluate the first at most eight frozen held-out pairs one at a time and restore MLX RNG/model modes even on failure, without advancing the sampler. Studio A: QLoRA 4 passed in 6.86 s (`probe-qlora.xml`), dense LoRA 4 passed in 11.91 s (`probe-lora.xml`), zero skips. Both objectives include an injected forward exception; checks cover exact RNG/next draw, model modes, parameters, sampler state and references. Source archive SHA256: `afbee88cc409082aaec67889afb6255aba23bd4095b0d69753be68fcdcef0eb5`. Worker objective dispatch is being connected; T040 remains open until its callback/checkpoint path is qualified.


### Native worker callback and versioned staging checkpoint

The production worker now dispatches frozen v3 inputs to the paired compiler/runtime/loss hook, builds full v3 checkpoint state, validates exact parent artifacts, restores policy-only checkpoint state, and handles v3 evaluation acknowledgements with kill/cancel precedence. SFT still uses its unchanged compiler/runtime/loss. Native callback tests on Studio A: QLoRA 6 passed in 7.49 s, dense LoRA 6 passed in 15.59 s (four preference objective/pause cases plus two existing SFT evaluation/pause cases each), zero skips. Receipts `worker-qlora.xml` / `worker-lora.xml`; source SHA256 `8c903ca33793b0cb71e61f11b5cb8ffcd71e5537af4134ccb4567b5dad23740f`. Each preference update counts two complete pairs and six response tokens across accumulation, emits the post-update probe, and evaluates held-out loss; checkpoints roundtrip complete real adapter/optimizer/v3 worker state. The first worker run failed at its test acknowledgement because the fixture omitted required committed-update/job-version fields; the corrected final runs are the evidence.

Version-aware private input staging preserves hashes, exact source coverage, counted storage holds and immutable replay for v3 while refusing SFT/preference mixing. Six focused staging/ack tests passed with inert fixtures and a fake tokenizer; old native lifecycle/evaluation suite had 71 passes before correcting two new fake-tokenizer fixture setup failures. Latest old native-registration/control/mixture group: 27 passed. Latest web full suite: 218 passed in 58 files; TypeScript passed after widening the generated metric-page union. Capability advertisement, native supervised bootstrap/transfer, resource measurements and end-to-end scheduler qualification remain outstanding and are not inferred from callback tests.


### Objective-specific measured resource evidence (partial T043/T045)

Added distinct v3 frozen measurement bindings, native observations and memory evidence with exact initial/reference targets, objective/options, immutable datasets and implementation/sampler/probe versions. Old SFT shapes and profile hashes remain unchanged; the new preference config/intent hashes use explicit namespaces. Native observations require a post-update probe for every completed update and count the complete DPO reference; bare references count every frozen tensor, including zero-initialized module tensors, as frozen reference weights. Parent-backed references split parent adapter tensors into their separate allocation. ORPO reports zero reference allocation.

The node-owned production measurement process now compiles paired sources, runs the preference loss/probe hooks and full evaluated checkpoint serializer, and produces v3 observations with guarded footprint/swap/thermal telemetry. Probe artifacts are deleted locally and never published as durable checkpoints. Studio A acquired tiny fixtures: QLoRA 2 passed in 10.75 s (`measurement-qlora.xml`), dense LoRA 2 passed in 15.67 s (`measurement-lora.xml`), both objectives, zero skips. Source archive SHA256 `1b4141be08e3e4ccabfb7f8279e59c55e94c2d87f2961f07fe046a3148367927`; private source/JUnit receipts are under `/Users/mcteer/coire-stage/018-native-loss`. Native source qualification uses the actual `MeasurementSupervisor`/owned process group, frozen native tokenizer analysis and verified acquired asset; it does not activate or replace the installed production node.

API freezing/resolution now distinguishes paired splits and whole prompt-group leakage, resolves exact ready local parents/configuration/depth/mirrors, and requires objective-specific measured evidence. Native measured profiles cannot inherit SFT evidence; core release admission is still being completed. Initial-parent unit matrix: 11 passed. New memory-model/preflight/frozen-byte group: 33 passed; later model/staging/spec group: 18 passed. New synthetic CPU report tests: 6 passed and explicitly do not constitute native qualification. Old real-Postgres measurement transaction group: 7 passed. Node measurement/control/new-report group: 18 passed. Strict mypy passed 997 files including the shared synthetic preference measurement fixture. Generated schema refresh and full release checks remain pending after the remaining changes.


### V3 control-plane continuation: measured admission, evaluation and lineage

- Disposable Postgres preference measurement submission/admission tests: 2 passed (DPO/ORPO), covering default-off new admission, accepted replay while disabled, old-node capability refusal, frozen paired data and exact initial/reference identities, memory holds and resource reports. Synthetic observations test reducers; actual native measurements are recorded separately above.
- Evaluation transaction regression: 48 tests passed across the final-trigger and checkpoint-boundary files after v3 empty suites, declared final obligations and v3 atomic pause/acknowledgement replay tests were added. Later admin pause ownership and single committed retention pin remain intact. All explicit v2-only authorization/input/trigger guards now include evaluated v3.
- Standalone extraction: 50 CPU contract tests passed, including DPO/ORPO × LoRA/QLoRA full tensors, immutable replay and malformed/corrupt/digest/base/shape/dtype/deadline/quota refusals. These are inert synthetic safetensors, not a native serving claim.
- Lineage: 2 pure contract and 2 disposable Postgres tests passed. The immutable child document includes exact parent manifest, job and datasets; cycle/cross-base/depth overflow are refused; parent retirement preserves child snapshot and does not inherit verification. Serving extraction now persists objective and lineage in the same transaction. Added the authenticated generated GET lineage surface; full route authorization/serving matrix remains pending.
- Capability negotiation requires both Studio health documents to advertise v3 before new preference job resolution; native node health advertises [1,2,3]. Existing SFT lifecycle + preference preflight unit tests: 68 passed. Strict mypy: 1001 files passed at this checkpoint.
- DPO/ORPO shipped registry-binding templates parse as numeric v3 and preserve optional bare initialization. CLI validate/submit carries original YAML verbatim and adapter lineage is a typed authenticated GET. CLI plus training API contracts: 108 passed. T055 complete.
- Web form covers objective/options, paired-dataset filtering, zero dropout, single Studio and optional registry parent; declared/empty suites preserve v3. Generated API types include v3. Native preference history separates loss from held-out post-update probes and pair/token counts; adapter ancestry is lazily retrieved. Focused form tests (8) and ancestry/probe tests (4) passed; final full frontend checks remain pending after these changes.
- Broad Python default-environment regression: 3051 passed, 75 skipped, 520 deselected in 170.06 s. This command accidentally included engine-marked tests (using training_engine instead of engine); 73 native tests were deliberately skipped on core plus 2 existing optional tests. It is not the mandatory native release gate. Actual Studio receipts remain authoritative; rerun the proper non-engine selection at final qualification.

Incomplete work remains tracked by unchecked tasks; no end-to-end serving, production profile approval, coexistence, admin review, or full release completion is claimed.

## Admin review and baseline operations continuation

The US4 implementation uses `feedback/review.py` rather than extending the owner
service: the queue and judgements share its live owner-generation eligibility,
sorted owner/admin/conversation locks and audited mutation scope. Shared admin
labels carry optimistic versions; skips are reviewer-specific. Review does not
alter chat selection, active-answer mapping or conversation revision. Idempotent
retry receipts are metadata only and recheck current privacy before replay.

Validation on core used disposable Postgres and inert source records, not models:
`COIRE_INTEGRATION=1 uv run pytest -q
apps/coire-api/tests/contract/test_feedback_review.py
apps/coire-api/tests/contract/test_feedback_export_sources.py
apps/coire-api/tests/unit/test_feedback_export_selection.py`: **16 passed in
17.51 seconds**, no skips. This includes concurrent separate-admin transactions
(one saved, one conflict), opposing owner/admin choices, all three source modes,
owner-first tag/date filtering without fallback, skip/revisit, opt-out refusal,
live role/activity refusal and idempotent replay/changed-input rejection. The
contract suite is the actual database-backed acceptance location for T067; no
redundant top-level wrapper was added.

`ReviewQueue.test.tsx`: **4 passed**, including native keyboard button activation,
skip/revisit, stale conflict refresh without automatic resubmission, withdrawal
while deciding (copied answers removed) and unchanged-transport retry UUID reuse.
This is DOM/component evidence, not a claim of a real-browser screen-reader audit.
Generated review OpenAPI/TS and Training Feedback integration are complete.
T062–T067 are complete for their specified review behavior and source acceptance.
Broader privacy/browser gates remain T057–T061.

Preference measurement transactions now prove profile approval waits for complete
stop proof and public v3 resolution uses the measured objective envelope:
`test_preference_training.py`: **2 passed**, DPO and ORPO. Parent retention-pin
queries cover seven live/terminal job states in actual Postgres;
`test_preference_lineage.py`: **9 passed**. V3 public node lifecycle added two
objectives through authenticated prepare/replay, start, renewal, stale fencing,
status, stop and old-ack refusal: `test_preference_lifecycle.py`: **6 passed**.
These remain inert protocol evidence; actual native execution receipts are above.

T068 adds bounded state/overdue/physical-cleanup gauges and a committed-maintenance
heartbeat. Oldest withdrawal age and six explicit state zeroes remain observable
while new training/preference admission and diagnostics are disabled. The real
OTLP-to-hardened-collector gate
`COIRE_INTEGRATION=1 uv run pytest -q
apps/coire-api/tests/integration/test_feedback_metric_export.py`: **1 passed in
0.99 seconds**. Ten series (six fixed states, four scalars) retain the expected
Prometheus names, with no user/job/pair/dataset/content/tag labels. Four focused
baseline/disabled-cleanup unit tests also passed. Failed polling never refreshes
the heartbeat. Dashboard datasource references use the existing `prometheus` UID.

The Prometheus image now copies feedback rules into its normal rule directory.
`docker build -f deploy/compose/prometheus.Dockerfile -t coire-prometheus:dev .`
succeeded (config image sha256
`1b87d4c4ae6eb7e0c70c7fa88cdf335bc8c44beb2c7da1b89df4a8a2094091db`). Its actual
`promtool check config /etc/prometheus/prometheus.yml` passed with **14 rule files**,
including four feedback rules. `promtool test rules
 deploy/observability/tests/feedback.test.yaml` passed: purge/export/cleanup firing
and clearing, absent baseline firing and healthy idle baseline. Existing training
fault rules continue to cover preference trainers; their regression gate remains
part of final validation. Image CVE/SBOM policy qualification remains T076.

T069 runbooks now document authenticated see/kill/export/purge/recovery/rollback,
publication retention, current v3 support and incomplete deployment qualification.
Architecture, roadmap, SFT runbook and compose setting guidance agree on the
accepted retention policy and unchanged bare-engine/core boundary. No dependency
was added. The Jobs activity view additionally exposes v3 objective and durable
post-update probe independently of loss; legacy fields remain unchanged and new
fields are omitted for SFT. Its focused component tests: **3 passed**.

V3 implementation gates T041/T042/T043/T048/T049/T050 are now checked from the
recorded native measurement and protocol/extraction/lineage evidence, separate
from outstanding full-serving/recovery qualification. The refreshed focused
protocol/model/extraction/recipe/measurement/checkpoint suite: **82 passed in
1.20 seconds**, no skips. The actual Postgres evaluation-boundary/trigger and
preference measurement/resolution suite: **50 passed in 53.51 seconds**, no skips.
Jobs activity metadata/probe tests: **5 passed in 6.57 seconds**, preserving old
SFT history and covering DPO/ORPO probes separately from loss. Strict mypy passed
**1006 files** at that revision. Full web: **228 tests, 60 files passed in 7.62
seconds**, ESLint passed and generated OpenAPI freshness passed. Both existing
training and feedback promtool alert regression files passed from the rebuilt
hardened image. These are intermediate checks; final release T076 is still open.

CI now defines a required `preference-engine` isolated macos-15 job and release
publish dependency, with acquired <=1GiB quantized and node-owned dense conversion
fixtures. It explicitly enables training-native tests for both bases, including
numerical, runtime, restart, probe, worker and owned measurement tests. It uses no
real Studio address. The new builder is not yet runtime-qualified; T071 remains
open pending builder validation. No passing default-off skip substitutes for the
explicit native gate.

## Bounded feedback performance (T070)

`COIRE_INTEGRATION=1 uv run pytest -q -s --tb=short
apps/coire-api/tests/contract/test_feedback_performance.py`: **1 passed in 27.95
seconds**, no skips, on coire-core.lab with Python 3.13.15, SQLAlchemy/asyncpg,
disposable local Postgres, ASGI HTTP transport and a synthetic private corpus.
10,000 chosen explicit pair records and 10,000 owner labels remain eligible
throughout ten concurrent review readers. Twenty actual thumb mutations and
20 actual settings mutations for a separate live owner run concurrently. The
latter uses its own capture generation so it does not withdraw the measured
10,000-pair corpus.

132 review samples: **p95 200.4 ms** (limit 1000 ms).
20 feedback mutations: **p95 111.6 ms** (limit 500 ms).
20 settings mutations: **p95 23.1 ms** (limit 500 ms).
Actual local publication: **21.595 seconds**, `succeeded`, matched and selected
count **10,000** (limit 300 seconds), excluding Studio token analysis.
Receipts/output include only counts/timings/terminal state; synthetic copied
bodies and staged dataset files remain in the disposable private fixture.
Queue/deadline/quota/crash tests are separately recorded in US1 evidence above.

The first valid load run exposed **9.58-second review p95**. The fix adds the
`kind=pair` predicate required by the existing partial pair-source index and
batches page owner-generation/conversation checks and label projection. Sorted
live owner locks, current generation, live admin/key authorization, conversation
locks and bounded pair revalidation still precede every copied-body read.
No index, migration, authorization or performance limit was weakened. Ten review
contracts, including withdrawal/conflict/concurrent reviewer behavior, passed
alongside the first passing load run (**11 passed in 39.44 seconds**).
T070 uses the contract-test directory to reuse the real API/Postgres fixture,
without starting the separate root integration compose topology.

## Keyboard and streaming withdrawal continuation

Actual **Chromium 141.0.7390.37** headless keyboard acceptance passed on core via
its existing installed binary and CDP, with no new dependency. Command:
`COIRE_BROWSER_BIN=<installed Chromium> uv run python tests/browser/feedback_keyboard.py`.
The temporary ignored Vite harness imports the actual contribution/settings/review
components and generated API transport; synthetic fetch receipts are scoped to
this browser fixture. All controls were reached by native **Tab** and activated
by native **Space/Enter**, with select typeahead for the skipped queue. No pointer
clicks or programmatic element focus were used. It proves disclosure before
capture, disabled initial contributions, toggle, thumbs, comparison creation,
selected alternative, administrator skip/revisit/judgement, withdrawal body removal
and continued ordinary-chat messaging. **14 typed transport calls**, all checks
passed. Private browser profile and ignored harness files were cleaned afterwards.
This browser gate is distinct from the real-Postgres authorization/privacy gates;
it does not claim a production backend or screen-reader measurement.

`COIRE_INTEGRATION=1 uv run pytest -q --tb=short
apps/coire-api/tests/contract/test_chat_comparisons.py -k withdrawal_between`:
**2 passed in 4.64 seconds**, disable and delete between actual API-owned stream
frames. Exactly one metadata-only delta commits; the late body is never copied,
review refuses immediately, bounded physical purge clears prompt/both answers and
quota count, and delta replay exposes no withdrawn content. The configured
24-hour deadline remains unchanged; test cleanup completed immediately.

Latest complete CPU selection:
`uv run pytest -q -m 'not integration and not engine'`: **3073 passed, 2 optional
legacy skips, 604 deselected, 182.56 seconds**. No native gate is counted from
these results. Existing independently enabled Studio receipts remain above.
Ruff formatting and image pin checks passed at this revision. Final integration,
image scan/SBOM and full deployment qualification remain open.

The publication/withdrawal gate now also physically purges unpublished source
copies while proving the **registered dataset remains byte-identical**, both
immutable published memberships remain, and its queued Studio analysis remains
queued (dataset `analyzing`). `test_feedback_export_publication.py -k
preserves_published`: **2 passed in 4.61 seconds**. The actual Postgres withdrawal,
provenance and replay suite: **16 passed in 11.32 seconds**, including live revoked
owner/API-key refusal, fresh generation on re-enable, writer/opt-out locking,
source expiry, dead API reaping, provenance refusal and same-cursor replacement.
T058 is complete from its focused component/page tests plus actual Chromium
keyboard evidence; broader full privacy acceptance is still tracked separately.

## Staged-image and admissions-disabled deployment receipt

API, scheduler, migration and web ARM64 images built successfully, passed all seven image-policy checks and produced SPDX SBOMs. Trivy CRITICAL findings: zero for each. Private scan/SBOM files: `/private/tmp/coire-018-{api,scheduler,migrate,web}-scan.json` and corresponding `.spdx.json`. Image config digests respectively `091378324aeb3f58d9078a86aff536e9d8cb239ff95f2670b00ca75e44acb77d`, `ab22bbcdc47c224d2b69ddc5c30d28c0521f99a3715586600db2f36d0141122c`, `e7ef36cbb52f4b43e47bf5799c3fdefffbbca6a84818fee7ca0c3d08a80194cd`, `023269e322924759d7f0a5f89553be5679b7bdfcd96bf0cacaec9146bd325337`.

Both Studios staged/import-verified immutable environment `/opt/coire/envs/0.2.0-61924e5b9c2e` using the locked wheelhouse. Idle node/workload inventory showed no live engines, acquisition jobs or training processes; listed training history was terminal. Atomically selected this environment and sent SIGTERM to the existing service-account-owned node PID; unchanged launchd configuration restarted it through the existing `current` symlink. Prior runtime `/opt/coire/envs/0.2.0-a68cf8319860` remains available. No root service configuration, network or permission changed. Studio A reported v3 capabilities and healthy status after restart; Studio B confirmation follows.

Applied additive `0033_preference_feedback`, then deployed API/scheduler/web with training and preference admissions disabled. `/ready` returned 200. Existing project, secrets, network definitions and volumes retained. Private rollback config: `/private/tmp/coire-018-acceptance/rollback-compose.json`; candidate `/private/tmp/coire-018-acceptance/core-018-drained.json`. The attempted pre-migration in-container dump could not be copied; it is **not** a verified pre-migration backup. A consistent post-additive-migration custom-format backup was instead captured directly from `pg_dump` stdout, verified `PGDMP` signature, 5,118,345 bytes, mode-private `/private/tmp/coire-018-acceptance/coire-after-additive.dump`. No data-loss downgrade was attempted. Full deployment/qualification gates remain open.

## Additional privacy, parent-pin and deployed native receipts

Streaming withdrawal contract cases (disable/delete between emitted frames): 2 passed, 4.64s. Publication-preservation cases: 2 passed, 4.61s; source purge retained the analyzing registered dataset, queued analysis, immutable published membership and byte-identical source snapshot. Withdrawal/provenance/replay selection: 16 passed, 11.32s. Actual browser keyboard evidence remains recorded above. Tests live in the contract directory to reuse the authenticated real-Postgres fixture rather than duplicate top-level acceptance wrappers. This maps T019/T020/T031 and T057/T060 to their actual contract/component test locations; deployed 20-pair export acceptance remains T034.

Parent lineage/pin file now passes 13 tests, 14.25s: queued/running measurement intent pins the parent in addition to queued/running/paused/recovering jobs; real retirement calls refuse pinned parents. Terminal measurement/job intent releases those initialization pins; child immutable ancestry survives parent retirement. Generated-type training form, activity objective/probe views and lineage tests are included in the 228-test web pass. T045 resolution has now also executed against the deployed exact registered SFT parent; T046 v3 checkpoints and acknowledgements executed during complete mirrored DPO training.

Both Studios confirmed healthy and spec_versions [1,2,3] after activation. Controlled training/preference admissions enabled in private `core-018-qualification.json`; evaluations remain disabled and all submitted v3 recipes declare empty suites. Project-authored 40-row/40-group preference upload `bde0ca11-3d35-5a08-99c0-e012c41c5924` analyzed natively with zero invalid rows; source SHA256 `eaeb7d7a46fbe5ffca886542265db2d78c9f15d3e0175126e1a93826f1821d9a`, split `f1f27559ac63fbb55a4303d3f232a1b8f38dc1b182645dba2ca3d33e73019985`, both-answer token rows `1c3589dc74f2ec2e9567ee4bbb34478565d9bd7f09ad4d84771a1707ab0be818`. Separate dense-bound source `e6606c63-24ae-5614-bc08-419c6996b3da` also ready.

Deployed DPO/QLoRA bare measurement `a09fe731-db98-446c-92e4-411cc374cf4a` succeeded: 16 updates, 3,462,350,840-byte peak, zero swap, safe thermal, profile `b0b7cfdb-07e0-4b91-8adf-806201973ddc`. First attempt `ad41ea4a-9646-4dcd-9136-360f9a353e60` was inconclusive before spawn and released holds only on scoped stop proof. The successful diagnostic retry used the same scheduler executor in a one-off scheduler container with background scheduler stopped, then restored normal scheduling. Training `01M4FXTB7V38BE8MT9BKNJGBNR` succeeded at 16, standalone private/unverified DPO adapter `a97e3eb2-208f-5bcd-b92c-b7ecf97db326`, authenticated exact-selector gateway response 200. SFT-initialized DPO/QLoRA `01M4FY57H7A90WPTD7KBKWANGX` also succeeded at 16 and served adapter `c3f47a93-3e3a-56b4-b190-6cbcf566017a` with 200. Both acceptance-owned instances drained to stopped afterward. Raw samples/recipes/reports under private `/private/tmp/coire-018-acceptance`. Remaining matrix is not yet qualified.

Qualification exposed nested five-second preparation timeouts: both measurement client and v3 training client now have an explicit 30-second prepare bound; controller separately gives only v3 prepare that bound. Stop/pause and legacy control budgets remain five seconds. TDD transport regression selection: 27 passed; controller regression file: 17 passed, 19.90s. Initial ORPO attempts exhausted recovery before any committed update under the old outer timeout; no successful training or quality claim is made for them. Fresh qualification retries follow the corrected controller.

All 16 release images passed native ARM64 build, container policy, CRITICAL scan and SPDX generation. Full machine-readable receipts `/private/tmp/coire-018-acceptance/image-gates.json`; per-image logs and SBOMs there. Subsequent scheduler/controller changes require refreshed affected-image gates. First broad integration run: 479 passed, 9 failed, 4 errors, 39 legacy optional skips, 1262.47s. Three failures were strict earlier-binding error-message expectations (same no-publication/no-release assertions), corrected and all five cases passed in 8.76s. Composed harness failures used stale CI node/image tags; rebuilt node-test and tagged current 16 validated CI images, obtained two run-image content digests through the workflow's loopback-only pinned CI registry, and started a new isolated integration run. No failing/skipped required gate is counted as accepted.

## Current integration and preparation qualification

Current-image isolated integration completed: **496 passed, 34 optional legacy skips**, 1550.59s; private log `full-integration-current-images.log`. Required feature-native gates run separately on Studio fixtures. CPU selection completed **3079 passed, two optional legacy skips**, 185.57s; subsequent preparation changes require focused regression checks and refreshed final gates.

Native worker now also extracts the final full v3 checkpoint into a standalone adapter and serves it through the authenticated node proxy. Both objective × pause cases: **QLoRA four passed in 11.79s; dense LoRA four passed in 19.31s**, zero skips. Private JUnit receipts `worker-serving-qlora.xml` / `worker-serving-lora.xml` on Studio A. The first run served successfully but failed the immediate-stop assertion; the corrected test awaits asynchronous stopped proof within five seconds and always closes the test agent. No fixture engine remained in the process inventory afterward. Logical CI node scope uses `coire-edge-a`; physical core/asset guards remain mandatory.

Deployed ORPO/QLoRA bare and SFT-parent jobs both completed 16 updates and served their exact private/unverified adapters: `c4abf042-4923-53f0-ac60-a3aad84c6e79` and `0903cb88-ffb2-5a5c-a869-f66aae5a73b2`. Acceptance-owned serving instances drained to stopped. Dense DPO measurement succeeded (5,227,957,512-byte peak, zero swap, safe thermal), but early dense training attempts exhausted recovery before update zero; these are **not qualified**.

Investigation added successful exact-intent v3 preparation validation reuse for polling only; start still freshly checks every asset and a mutated staged source fails before spawn. Node lifecycle/measurement selection: 69 passed. Direct validation timing on the staged dense source was 2.25s, so repeated checks alone did not explain the expired leases. The scheduler renews reserving attempt leases, but node renewal previously refused prepared owners. Corrected renewal retains all fencing, expiry, released/stopped refusal and the five-second active control bound. V3 preparation replay and refreshed input grants also need current renewed-attempt authority while retaining the immutable original preparation payload; new real-Postgres regression passed. Full focused controller/transport checks and fresh deployed retries are in progress. Runtime candidates `0.2.0-de6659d663b1` and `0.2.0-8d44fc63f7af` were staged and activated through the unchanged service-account launchd path; prior environments retained.

Created a separate `acceptance-018@coire.invalid` user and chat-scoped API key for deployed privacy qualification; the credential is in Keychain, never an artifact. Existing owners' capture settings and contributions are not used for withdrawal testing.

### Deployed exclusive matrix and owner privacy acceptance

All eight DPO/ORPO × affine QLoRA/dense LoRA × bare/SFT-parent jobs completed 16 updates on Studio A and produced private, admin-only, unverified standalone adapters. Each exact adapter selector served an actual bare-engine response through the authenticated gateway, then its acceptance instance reached stopped. The private `matrix-v12.log` ends `MATRIX_COMPLETE`; per-case job, measurement, lineage, adapter, serving and drain receipts retain exact identities. Dense bare peak was 5,227,957,512 bytes, with zero swap growth. Earlier preparation/lease failures remain in the audit history and are not counted as successful qualification.

Real native chat and comparison acceptance used a separate chat-scoped owner/key, not the existing administrator's capture setting or contributions. Twenty-one explicitly generated, distinct comparisons were selected; replay retained the same selection. Export registered dataset `11d70037-2c0c-5216-9bf5-868ef97a03d5`, with 21 rows/21 groups and native analysis ready. Source SHA256: `52426559bf6cbec8c97851ff7a93556ebb226c00d73a981b72dcb02fd1d43fbf`. Opt-out withdrew all 21 pairs and removed copied answer bodies from responses immediately (0.666 seconds for mutation and all checks), while preserving published bytes. The independent cleanup worker physically purged all generation-3 provenance copies by 10:17:06 UTC on 2026-10-09.

Re-enabling created a fresh capture generation; old pairs stayed withdrawn. A newly selected, unexported comparison became inaccessible together with its conversation after deletion (0.121 seconds), and its generation-5 provenance was physically purged by 10:22:07 UTC. A subsequent actual follow-up on the published source conversation captured the chosen previous answer and excluded the rejected answer; the proof verified published row membership/content SHA against the captured prompt inside the API service, without copying prompt text into this record. The account was disabled again after this generation-7 check. Private receipts: `privacy-withdrawal-timing.json`, `privacy-generation-deletion.json`, `privacy-chosen-context.json`; raw bodies remain outside Git.

Actual native feedback exposed a strict internal node-contract omission: controlled top-k, min-p and `enable_thinking=false` template options are now explicitly typed, with unknown template keys refused. Core/gateway/node checks passed 51 cases. Updated native worker extraction/serving tests passed four QLoRA cases (15.64 seconds) and four dense cases (22.93 seconds), using that exact internal request shape. Structured logging now uses shared JSON encoding with UTC timestamps and a closed set of content-free identity fields; its security test refuses arbitrary prompt/secret extras.

Latest CPU suite: 3,082 passed, two optional legacy skips, 617 deselected (191.28 seconds). Web: 228 passed across 60 files; lint and TypeScript checks passed. Latest complete image gate built/scanned/policy-checked/SBOM-generated all 16 ARM64 release images with zero critical findings; later scheduler logging and cosmetic source changes require a final artifact refresh. These are checkpoints, not a claim that all final gates are complete. Timed coexistence is running on Studio B; recovery uses a separately promoted disposable SFT ancestor.

### Restart, lineage, cancellation and completed privacy gates

Disposable SFT ancestor `817ffa5f-f2d6-53cb-9c57-dda8810afd4e` was promoted from an existing retained SFT checkpoint through the authenticated admin path. Its separate DPO child job `01M4G3S3K0YNRKXDYWW9TRVP31` used fresh exclusive memory evidence and completed 512 updates. It paused at committed update 8 (checkpoint `c16fe84a-8f93-42cf-82f0-5cd9c517e646`, 84,118 bytes, SHA256 `169e1680f439ecd211788c418d9dc10f42dd74f10e7a4359420a7dd60a285781`, verified on both Studios), resumed after the owned Studio A node process restarted, then paused at 504. The rebuilt scheduler restored that exact paused state after its container restart; final resume reached 512, including ordinary preparation/recovery retries during node activation. No scratch restart or manual hold release was used.

Child adapter `2ad34263-1a79-50f8-93f1-7146168a26c5` served through its exact selector. The disposable ancestor was then retired through the audited admin API. The child's full immutable lineage document remained byte-equivalent as a parsed document and the exact child served again, still admin-only and unverified; both owned serving instances reached stopped. The preexisting SFT ancestor was not retired. Private receipts retain container IDs, node PIDs/environment, pause/resume versions and lineage. A script initially looked for the wrong lineage field (`initial_target`); correcting the read-only check to the actual contract's `parent` verified the result.

Independent live cancellation job reached confirmed cancelled state in **1.310536 seconds**, measured from the API cancellation request after a committed update. Core terminal cancellation requires scoped positive stop proof; raw control/job/timing receipts remain private. Other restart/corruption/unknown-liveness and rollback qualification remains under T073/T075.

Final deployed privacy proof counted **24 purged provenance records, 22 purged comparison records and 22 purged judgement/tag records**, all copied content null and counted bytes zero. All **21 published export members** remained published and source SHA stayed unchanged. Generation-7 follow-up content was physically purged by **10:32:05 UTC**, as well as all earlier acceptance generations. Capture is disabled for the acceptance account. Together with the existing real-Postgres credential/ownership/streaming withdrawal/crash tests and native Chromium keyboard component acceptance recorded above, this completes T034 and T061 at their stated boundaries; it does not claim a screen-reader or production browser-backend audit.

Private measured coexistence attempt `21d29233-182d-4f31-af0f-55d59401dfdc` is **inconclusive**, not qualified: 179 persisted requests had overall p95 527.9 ms and max 3,204.7 ms, while the trailing-window gate refused. Recovery traffic shared its administrator lock. Subsequent runs use separate acceptance principals per Studio and a release barrier after full integration, with sustained 512-update probes and two-second arrivals. A separate read-only authorization trace found 22 SQL queries per original full measurement check. Fresh batched registry reads now preserve all exact engine/artifact/model-hold refusals in at most 12 queries, and initial authorization/request-lease acquisition share one locked transaction; stream checks remain fresh. Postgres gateway/measurement tests passed 8 cases and ordinary proxy tests passed 13.

An additional v3 input-poll regression failed with three validations instead of one, then passed after complete identical unstarted inputs reuse owned validation. Start still freshly rejects a mutated staged source before any PID. Preference measurement input preparation also gets its own 30-second client budget, while legacy input/control and the independent five-second stop lane remain bounded as before. Focused measurement/staging/worker-control/supervisor tests passed **43 cases**. Final native wheel/image refresh and remaining coexistence/release evidence are still required.

### Final recovery and drained rollback receipts

The actual lease-guard fault froze only the service-account-owned Studio A node
while its native worker ran. The worker stopped **0.592578 seconds after lease
expiry**. Core observations retained the counted hold through stopping and
unknown liveness; cancellation completed only after the node returned and scoped
positive stop proof arrived. A reversible one-byte change to an owned checkpoint
was refused (409) on Studio A while the unchanged Studio B mirror verified (200).
Restoring the exact original SHA restored verification; no published artifact was
changed. Private proofs: `lease-guard-proof.json`,
`lease-guard-counted-hold-observations.json`, `checkpoint-corruption-proof.json`.

Separate job `01M4G7CQDGWKF025PXDTXFJ8HG` paused at update 16. An immediate
read saw four committed copies before asynchronous cleanup; the eventual bounded
read proved exactly three committed checkpoints, each verified on both Studios,
and one purged older checkpoint. The job then cancelled cleanly. Receipt:
`retention-proof-converged-checkpoints.json`; no manual cleanup was used.
Together with native fresh-process tolerance checks, frozen v1/v2 regressions and
the eight exclusive train/serve cases, these close T056 and T073.

The drained rehearsal selected prior 017 API/scheduler/web image identities and
node environment `0.2.0-a68cf8319860`. Studio A initially stopped because the old
parser rejected v3 adapter-extraction recovery metadata. The complete drained
attempt, measurement and extraction journals were preserved offline; with those
records quarantined, both old nodes answered authenticated capabilities and the
old gateway served real inference. Its owned instance drained to stopped. Original
journals were restored before activating `0.2.0-9b778b3c4c48` and the final 018
API/scheduler/web images; both restored nodes advertised v3. The acceptance
feedback owner alone was temporarily quarantined through audited admin changes
and restored active afterward, with its opted-out capture unchanged. Additive
schema and all published datasets/adapters remained in place. Private receipts:
`rollback-smoke.log`, `rollback-extractions-{a,b}.json`,
`rollback-restored-node-{a,b}.json`, `rollback-privacy-owner-restored.json`.

Final CPU suite: **3,083 passed**, two existing optional legacy skips, 617
deselected, 195.68 seconds. Full current-image integration: **505 passed**,
34 existing optional legacy skips, 3,162 deselected, 1,569.38 seconds. The latest
input-poll control change subsequently passed 43 focused cases; its refreshed
node-test image plus real-Postgres gateway/measurement/evaluation selection passed
**10 cases, zero skips**, 12.03 seconds. Ruff, formatting (1,766 files), mypy
(1,010 files), OpenAPI freshness and digest pin checks passed. All 16 final ARM64
release images built, passed seven policy checks, had zero CRITICAL Trivy findings
and generated SPDX SBOMs. Latest machine-readable image receipts remain private
in `image-gates.json`. Required native feature gates have no skipped cases; the
optional legacy suite skips are not counted as completed native qualification.

### Gateway admission latency follow-up

The first sustained v4 baselines were stopped and marked inconclusive: early
gateway-overhead p95 exceeded 20 ms. Withdrawing only the two acceptance owners'
authority through audited mutations stopped their measurements and normal scoped
cleanup released holds; both owned chat residents drained. No passing shared
profile is claimed. A separate bounded native callback diagnostic identified
repeated lease reads after initial exact-resident validation. Its private
`gateway-profile.jsonl` contains timings and SQL operation kinds, no SQL values.

A new real-Postgres regression failed when initial lease acquisition exceeded
12 queries. The private task-local measurement now retains validated mapped rows
only through the original live admission transaction. Lease insertion uses those
rows under the same owner/node locks and checks transaction identity; ordinary
requests, renewed leases and each stream recheck still validate freshly. Command
reads and verified-copy digest checks are batched without weakening inventory,
identity, exact engine/port, artifact or counted-hold refusals. The regression
passed; combined gateway/measurement/retention/seeded migration selection passed
**15 tests, zero skips** (17.64 seconds), affected gateway/measurement units
passed **62** (3.63 seconds), and mypy passed all 1,010 source files. Refreshed
images and new sustained measurements are required before T074/T076 closure.
The rollback's restored identities and isolated seeded downgrade/evaluation
history preservation, together with native evaluation acknowledgement/pause
regressions, complete T075 within its drained, schema-preserving boundary.

The v5 attempt at one request in flight still exceeded the overhead threshold
and was stopped as inconclusive; it is not shared-profile acceptance evidence.
A second regression first failed because identical immutable workloads were
parsed 180 times across 30 requests. A bounded eight-entry parsing memo now
compares the complete current request document, command payload and digest on
every check before reusing parsed values. Current owner/key locks, running state,
actor, dispatch deadline, node/registry/engine/artifact reads and counted holds
remain fresh. Changes during an existing stream fail closed rather than replacing
its frozen intent. The real-Postgres midstream mutation test passed, including
unchanged per-frame live-check counts and exact private lease accounting. Latest
gateway/measurement/evaluation selection: **10 passed, zero skips**, 15.64 seconds;
strict mypy passed 1,010 source files. The prior full CPU checkpoint was **3,083
passed**, two existing optional skips, 617 deselected, 203.80 seconds. Final
post-memo CPU/images and measured performance remain open.

Post-memo full CPU gate: **3,083 passed**, two existing optional legacy skips,
617 deselected, **206.42 seconds**. Final strict mypy/Ruff/format checks passed.
All 16 refreshed release images passed the complete ARM64 build/policy/CRITICAL
scan/SPDX gates. Controlled core deployment uses API image config digest
`468c329499ddd702e1266e005161bbb39f32148e09264268a836014e7c4db269`; node
environment remains `0.2.0-9b778b3c4c48`, with unchanged native objective/runtime
identity. New v6 measurements retain 4,000-token input, 500 ms arrivals and
**one request in flight per resident**. Any successful profile is limited to that
exact bound; two-request concurrency remains unqualified.

### Actual gateway service measurement boundary

V6 scheduler-local proxy traffic remains diagnostic/inconclusive. A bounded
callback clone usually recorded 8–12 ms overhead, while the scheduler-hosted
initial authorization/limits stage had 29.4 ms p95 in trace evidence. The measured
workload now calls the actual API gateway through the existing configured training
API URL and node-token map. New strict core contract:
`TrainingMeasurementGenerateRequest`; node membership and frozen principal digest
are checked before fresh owner/key, target/prompt, registry/artifact/engine and
counted-hold validation. No raw owner credential is copied, generated content is
never returned, and gateway route processing plus normal disconnect/credential
checks are included in its timing path. Scheduler arrival/orchestration and the
independent stop lane remain unchanged. ADR-0014 and the training contract/runbook
record this correction; no new service, dependency or network permission exists.

The endpoint test failed before implementation, then passed real Postgres tests
for missing/bad node authority, foreign-node and principal-digest refusal, unknown
fields, changed prompt, live inactive owner, exact metadata routing and transport
result mismatch. Gateway/measurement/component/lifecycle integration selection:
**25 passed, zero skips** (29.55 seconds). Affected runtime/gateway units:
**63 passed** (3.85 seconds). New OpenAPI security/schema contract passed; generated
OpenAPI/TypeScript refreshed together. Strict mypy: **1,012 files passed**. Native
objective code and the measured Studio environment remain unchanged.

### Refreshed gateway API release checkpoint

The final API transport build passes **3,084 CPU unit/contract tests** in 214.06
seconds; two existing optional legacy skips and 617 integration/engine deselections
are not native qualification. Web: **228 tests in 60 files**, 4.95 seconds;
TypeScript and lint pass. Strict mypy passes **1,012 source files**, Ruff and
format checks pass, generated OpenAPI/TypeScript are fresh together, and image
pin verification passes. All **16 ARM64 production images** pass build, the seven
image-policy assertions, zero CRITICAL Trivy findings and SPDX SBOM generation.
The complete isolated integration rerun is still pending at this checkpoint.
Raw logs, machine-readable image receipts and SBOMs remain private outside Git.

Actual image-exclusion attempt 1 reached committed update four on both native
trainers, then the image API correctly refused the existing admin key without the
`images` scope. Both owned trainers were cancelled with scoped cleanup. This
refusal is not image-exclusion acceptance. A separate acceptance-only key with
only `images` scope was issued through the audited admin API and kept in Keychain
for the repeat; existing administrator/key scopes were not expanded.

### Actual training/image exclusion

The corrected acceptance request matches the already registered model's exact
512×512/four-step/zero-guidance bounds. DPO QLoRA on Studio A and ORPO QLoRA on
Studio B both reached committed update four. Image job
`01M4GD3NJDMGX84EF5ABEBARHK` stayed **queued** across ten observations over five
seconds while both trainers remained running. The actual scheduler `_image_busy`
guard returned true for both nodes; the job had no selected node, instance,
reservation, resolved execution or output (fence zero). The image was cancelled
before either trainer's hold was released. Both acceptance trainers then reached
confirmed `cancelled` through expected-version controls. No image engine or Metal
worker was started. Private receipts: `image-exclusion-v3.log`, ten image
observations and `image-exclusion-core-guard-proof.json`.

The second attempt was refused before admission because 64×64/one-step was
outside the registered model's bounds, despite satisfying the generic wire shape;
it was corrected using the authenticated image catalog. Neither earlier refusal
counts as exclusion evidence. Sustained chat coexistence still remains open under
T074.

### Final full isolated integration gate

Current gateway-API images: **505 passed**, **39 skipped**, 3,159 deselected,
219 existing warnings, **1,574.23 seconds**. The skips comprise the 34 existing
optional live/manual/paid acceptance cases plus five existing offline native
evaluation cases selected by `-m integration`; these require their separate
isolated-Mac `COIRE_EVALUATION_ENGINE=1` job and cannot run on core. The prior
record's 34-skip count used an engine-excluding selection. They are not preference
runtime qualification: the required 018 native/component/resume/extraction/serving
gates were executed on Studios with zero required skips. The five evaluation
cases remain wired to the unchanged isolated-Mac evaluation-engine CI job, and
real-Postgres feature-017 evaluation ownership/acknowledgement regressions passed.
No failed or skipped required 018 gate is counted as passed. T076's local release
checks are complete; sustained performance is separately pending under T074.

V7 actual-API measurements remained inconclusive: B exceeded 20 ms overhead, and
A's short rolling observation also exceeded the limit. Both owners were withdrawn
through audited active-state mutations, both measurements ended inconclusive and
the owned residents drained. A bounded 30-request diagnostic (SQL operation kinds
and timings only) recorded 11.46 ms median / 23.29 ms p95 callback overhead, with
fresh authorization p95 16.72 ms. It is diagnostic traffic, not qualification.
The next measured attempt runs one Studio at a time and lowers acceptance-status
polling from every two seconds to every 30 seconds; the frozen chat workload
itself remains one in flight, 500 ms arrivals, 4,000 tokens and one output token.

V8 single-Studio/30-second status polling also exceeded the overhead limit; it
is not qualification. Diagnostic comparisons through the callback, handler and
full ASGI stack preserved fresh checks and isolated HTTP/middleware behavior.
A short standard-library cProfile session temporarily changes only the API
container command on this unqualified run, preserving its exact image, user,
read-only root, mounts, secrets and networks. Profile statistics are private,
contain function names/counts/timings only and are removed from the existing
volume after retrieval. The normal container command is restored afterward.

### Gateway event-loop stall correction

The private live API profile recorded **18 synchronous Argon2 verifications**,
about **20 ms each**, during 99.6 seconds. Their event-loop blocking can stall
unrelated gateway requests even when the measurement's node credentials do not
invoke Argon2. This is distinct from model time. The normal API command was
restored, the profile retrieved privately and its temporary volume file removed;
V8 ended inconclusive with owned-process cleanup and resident drain.

API-key verification now uses the existing AnyIO worker pool with an explicit
**one-verification capacity limiter**, preserving the previous approximately
19 MiB single-hash memory envelope and unchanged Argon2id parameters. No secret,
hash result or authority is cached. After the await, the key is read freshly and
its hash, version, owner, prefix and revocation state compared with the verified
snapshot; current user activity is also read freshly. No new dependency exists.

New loop-responsiveness, single-hash bound and mutation tests failed before the
fix (**six failed, one passed**) then passed with existing key/limit/contracts:
**15 passed** (2.36 seconds). Real Postgres concurrent revoke/rotate/deactivate
plus actual gateway transport: **four passed**, zero skips (8.47 seconds).
Ruff/format/OpenAPI freshness pass; strict mypy passes **1,014 files**. T076 is
reopened only to refresh the full release gates after this substantive fix;
native objective/runtime code and both Studio environment identities are unchanged.

### Captured feedback through SFT-initialized native training

Published 21-pair dataset `11d70037-2c0c-5216-9bf5-868ef97a03d5` remained
usable after the contributing owner opted out and copied feedback was purged.
A fresh exact DPO/QLoRA memory measurement succeeded on Studio A. Job
`01M4GFY250N0FTEAV4V9E602W0` used this dataset for grouped train/validation,
started from compatible SFT adapter `33efba4f-4277-56a0-a26a-c7665b90aeaf`,
and completed **16 committed updates**. Result
`bff27aeb-9437-55e2-9e73-b1a082418d56` stayed **private/admin-only and
unverified**, recorded its lineage, and served successfully through its exact
selector before the owned instance drained. Private log:
`captured-feedback-training.log`. This completes an actual captured-feedback →
published dataset → SFT-initialized preference adapter → serving loop; it makes
no model-quality or harness-verification claim.

Post-verification full CPU rerun: **3,091 passed**, two existing optional skips,
620 integration/engine deselections, **214.45 seconds**. All 16 refreshed ARM64
images pass build/policy/zero-CRITICAL-scan/SPDX gates, with final receipts in
`image-gates.json`. Current API image config digest:
`52386bbecc00df42fc4c97be982afa1ebbc06a520d7ad8c1f628cbd9ba4d0bbd`.
Native node environment remains `0.2.0-9b778b3c4c48`. Final isolated integration
and v9 sustained coexistence are pending; the latter waits on the former.

Final post-Argon2-fix isolated integration run: **508 passed**, 39 existing
optional/isolated-native skips as classified above, 3,166 deselected,
219 existing warnings, **1,593.72 seconds**. Process exit zero. Together with
3,091 CPU tests, 1,014-file mypy, Ruff/format/OpenAPI freshness, unchanged
228-case web/type/lint gates, image pins and all 16 refreshed image gates,
T076 is complete again. The v9 timed Studio workloads were released only after
this run ended; no heavy Core builds or full tests overlap their windows.


### Shared admission timing and query structure follow-up

The v9 two-Studio run and v10 single-Studio run did not meet the unchanged
20 ms p95 gateway-overhead limit (observed histogram estimates roughly
43–48 ms). Both were ended as inconclusive through audited withdrawal of the
owned acceptance principal; their residents were positively drained. No shared
profile from these attempts is qualified. The v10 temporary diagnostic API
entrypoint also invalidates it as a qualification run.

A private content-free stage trace measured initial authorization around
12–24 ms, route binding around 3–7 ms, quota around 2 ms, lease creation around
2–3 ms and current credential checks around 3–5 ms. SQL structures for the
measurement/command, node inventory, training holds and joined resident
inventory are now constructed once with bound parameters. Query results,
authority and inventory remain fresh on every execution; revocation, engine,
artifact and counted-hold checks are retained. The real Postgres gateway test
and strict wire contract pass: two tests, zero skips, 5.52 seconds. Mypy passes
all 1,014 source files and Ruff passes. The private timing trace was copied out
and removed from the service volume; the normal API command was restored.
T076 is reopened for final image/full-test reconciliation after this change.
V11 starts with one Studio and one request per second before extending to both.


V11 also remained above the gateway-overhead limit and was withdrawn and
positively drained before publishing a qualified shared profile. The next
optimization combines live owner/key authorization with the measurement and
command read (locking the owner and API key together), and counts both model
and training reservations in one fresh joined inventory query under the node
admission lock. The query-budget test failed at six reads before the change and
passes at four afterward. The resident query locks the current reservations;
missing/extra/legacy holds, engine/artifact drift and ended dedicated training
holds still refuse. Browser-admin and API-key variants each run 30 generations
and the existing mutation/stream checks. Additional real Postgres assertions
verify API-key revocation, credential-version rotation and removal of admin
scope refuse before any upstream generation. Ten relevant transaction/contract
tests passed without skips (16.99 s); the added credential assertions also pass
both parameterized cases (7.59 s). Full strict mypy remains 1,014 files.
V12 uses the refreshed API image and the same one-Studio 1 Hz frozen workload.


V12 was also withdrawn as inconclusive and its resident positively drained.
Its content-free trace showed initial authorization around 9–13 ms on ordinary
requests, quota around 2–6 ms and three-write lease creation around 2–4 ms;
a rare generation-two garbage collection took about 70 ms. Diagnostic command
and file were removed before V13. The lease insert, reservation last-used
update and instance in-flight increment now execute in one PostgreSQL statement
using data-modifying CTEs, under the same current transaction and node/row
locks. ORM values are marked committed so a later flush cannot double-increment.
API-key admission uses the fresh key already mutation-locked by authorization
in that same transaction, retaining the atomic rate insert and current monthly
quota read. The tightened initial lease budgets failed before this change
(seven browser-admin / ten API-key queries) and pass afterward (five / seven).
Thirty generations leave zero instance in-flight count and exactly thirty
released leases; API-key settlement records thirty requests, 120,000 input and
60 output tokens, with thirty rate admissions. Ten relevant checks pass with
zero skips (17.38 s); explicit current-rate/current-monthly-limit refusal checks
pass in both parameterized gateway cases (7.57 s). Strict mypy passes 1,014
files; Ruff passes. V13 begins with the same single-Studio frozen 1 Hz workload.


V13 likewise remained above the limit and was withdrawn and drained. The
internal generation route now keeps its initial transaction open for callback
admission, rather than committing it and acquiring a second pooled connection.
Authorization still rereads current owner, key, measurement and command rows;
the existing request-lease boundary commits before upstream I/O. A real gateway
assertion failed before the change because initial admission used a different
session; it passes with the active route transaction reused. All subsequent
stream guards and renewals open fresh sessions as before. Ten relevant tests
pass without skips (14.38 s). The joined queries additionally select only
fields consumed by the guards and resolution, with ORM raiseload preventing
unexpected deferred access. No validation field was removed. Ten tests pass
without skips (14.82 s), strict mypy passes 1,014 files, and the refreshed API
image builds and passes container policy. V14 begins with the same frozen
single-Studio 1 Hz workload; all performance thresholds remain unchanged.


V14 (1 Hz) and V15 (500 ms arrivals) remained above the unchanged overhead
limit; both were ended inconclusive through audited owner withdrawal and their
residents positively drained. No successful shared profile is claimed.

The current fresh, locked API-key row now admits rate and monthly quota in one
atomic PostgreSQL upsert. The current monthly counter gates both insertion and
conflict update; failures retain rate-first precedence when both limits are
exhausted. A tightened six-query initial API-key lease budget failed before and
passes after this change. Thirty-request counting, revoked/rotated/unscoped
keys, rate exhaustion, quota exhaustion and simultaneous exhaustion all pass.
The test fixes its minute window so crossing a wall-clock minute cannot mask a
rate refusal. Ten transaction/contract cases pass (14.09 s); both expanded
gateway variants pass (7.29 s), with no required skips.

The parsed immutable workload additionally binds node UUID/name pairs after
its first successful current registry validation. These are lock/identity
bindings, not cached authority. Every subsequent joined inventory checks all
current node rows against those exact pairs, alongside current owner/key,
measurement/command, resident, engine, artifact and counting holds. A node
rename is refused before engine I/O. The reduced-query test fails at four
repeated reads before and passes at three afterward; the first validation still
performs its fresh node lookup. Ten relevant cases pass without skips (13.60 s).


V16 improved the short-window fraction at or below 20 ms to about 80%, still
below the required 95%; it was withdrawn and drained as inconclusive. A private
content-free diagnostic trace measured 148 requests: median initial admission
17.748 ms, route binding 3.360 ms, current authorization 5.917 ms, atomic
limits 2.578 ms, lease creation 2.092 ms, and remaining setup/commit 3.255 ms.
The private command and trace file were removed and the normal command restored.

Quota admission now uses fixed, typed, bound SQL to avoid reconstructing and
compiling the PostgreSQL upsert for every request. Its current-counter checks,
lock requirements and refusal precedence are unchanged. Lease DML uses a fixed
bound statement too. The instance increment returns its actual database counter
value for ORM state, rather than deriving it from an earlier observed value.
All three lease writes remain one atomic statement under the same admission
locks. Ten relevant Postgres/contract cases pass with zero skips (15.61 s).
Final release reconciliation remains open after these performance changes.


V17 improved again but remained below the required 95% at the 20 ms histogram
edge; it was ended inconclusive and its owned resident positively drained.
The internal generation route now owns a bounded (two retained/two overflow)
database pool using the configured database identity and credentials. Its first
readonly lookup checks the connection instead of a redundant pre-ping query.
Only an invalidated connection on that lookup can retry once, after explicitly
rolling back the failed transaction. Non-disconnect statement errors, a second
failed read, generation errors and commit errors are never replayed. Current
owner/key, registry, artifact, node and hold checks are unchanged and fresh;
the ordinary shared database pool keeps its existing pre-ping behavior. The
API lifespan disposes the new pool on shutdown.

The readonly retry test failed before implementation and passes afterward.
Five unit boundaries pass without skips (0.57 s). Ten real Postgres/contract
cases pass without skips (14.27 s), including an actually terminated idle
metadata-pool connection followed by a complete gateway request. The live API
also recovered an explicitly identified idle connection belonging only to
`coire-api.training-measurements`; before and after lookup returned opaque 404,
with zero engine requests. The live collector exports
`coire_training_measurement_database_reconnects_total = 1` with service/scope
labels and no user/source labels. Its dashboard, reconnect-storm alert and
runbook are added; promtool's full training alert suite passes. Strict mypy
passes 1,016 source files, Ruff passes and the new API image builds/passes
container policy. V18 begins with the frozen 500 ms single-Studio workload.
Full release reconciliation remains open.


V18 remained below the required 95% at the 20 ms histogram edge and was ended
inconclusive; its owned resident was positively drained. The next optimization
uses PostgreSQL FOR SHARE for readonly owner/key checks in the private gateway
and measurement watcher. These locks permit concurrent readers while still
blocking deactivation, revocation, rotation and scope removal until their
transactions finish. Mutation paths retain their default exclusive locks.
Four real-Postgres concurrency tests failed before the change and pass after it.
Each holds two readers, verifies the identity mutation waits for both, then
verifies fresh authorization refuses the changed identity.

The coexistence watcher now resolves all frozen residents and counted model/
training holds in one fresh joined inventory query under canonical node locks.
The gateway integration test enforces one inventory query after the node lock
queries, preserving exact engine/artifact/node identity checks. The focused
regression suite passes 26 tests with zero skips (20.44 s); strict mypy passes
1,017 source files. Both ARM64 API/scheduler images build and pass container
policy. V19 begins with the unchanged frozen 500 ms workload. T076 is reopened
for final release checks after these changes; T074 remains incomplete.


V19's steady diagnostic window had 218 requests, about 90% at or below 20 ms;
it was ended inconclusive and its owned resident drained. V20 was explicitly a
content-free timing diagnostic, ended inconclusive and drained; the temporary
API entrypoint was removed and its service-volume timing file deleted. Among
154 diagnostic requests, median/p95 gateway overhead was 15.40/22.65 ms and
admission was 14.63/21.97 ms. Full current identity/inventory checks remained
enabled. These short windows are diagnostic evidence, not qualified profiles.

Repeated authorization now fetches PostgreSQL-computed SHA-256 digests over both
complete fresh stored request and command documents, instead of retransferring
and decoding unchanged documents. The first check loads and validates the full
documents alongside their database digests; a changed digest reloads/revalidates
them. A midstream digest change refuses generation immediately. Only immutable
parsing remains cached; current owner/key, measurement state, command actor,
registry, artifacts, nodes and holds are still freshly checked under locks.
The bounded-JSON-selection regression failed twice before implementation, then
passed. Both request-document and command-document midstream mutation tests
pass. Focused Postgres/contract/unit regression: 27 passed, zero skips in
18.17 s. Strict typing passes 1,017 source files; the ARM64 API builds/passes
container policy. V21 starts with the unchanged 500 ms workload and limits.


V21's steady diagnostic window had 267 requests, 92.13% at or below 20 ms;
it was ended inconclusive and its owned resident drained. Private stream-frame
and credential rechecks now use independent fresh sessions on the route's
bounded measurement database pool. They do not share the admission session and
do not retry any stream/admission operation. The pool-identity regression failed
before the change and passes afterward. Ordinary gateway requests continue to
use the existing database pool.

Credential liveness now uses one fresh joined scalar query matching key ID,
owner ID, active owner, non-revoked key and exact credential version. It cannot
reuse stale ORM rows. Four real-Postgres stale-session cases failed before the
change and pass afterward for revocation, rotation, deactivation and owner
reassignment. The focused integration/unit suite passes 19 tests with zero
skips in 19.99 s; existing gateway revocation/parallel-first-check behavior
passes. Strict typing passes 1,017 source files; the ARM64 API builds/passes
container policy. V22 starts with the same frozen 500 ms workload and limits.


V22's initial diagnostic window had 144 requests, 81.94% at or below 20 ms;
it was ended inconclusive and its owned resident drained. The API now explicitly
selects Uvicorn's supported uvloop event loop, with `uvloop==0.22.1` pinned
exactly in the API manifest and lockfile (distribution SHA-256 pins included).
The dependency is dual MIT / Apache-2.0; it provides I/O scheduling on Linux
and macOS and does not perform model work. Official references:
https://www.uvicorn.org/settings/ and https://pypi.org/project/uvloop/0.22.1/.
The lock adds only uvloop; no existing package is upgraded. The ARM64 API
builds/passes image policy, and its packaged Uvicorn loop factory creates
`uvloop.Loop` in the hardened runtime. The focused regression suite passes
19 tests with zero skips (see private uvloop-regression.log). V23 starts with
the unchanged frozen workload and acceptance limits. The metric observer was
restarted before the timed workload to extend its bounded observation window.


V23's steady diagnostic window had 208 requests, 83.17% at or below 20 ms;
it was ended inconclusive and its owned resident drained. V24 was a content-free
commit/stage diagnostic, ended inconclusive and drained; its temporary API
entrypoint and service-volume timing file were removed. Among 79 diagnostic
requests, median/p95 overhead was 15.89/22.13 ms and admission 14.97/21.05 ms.
The initial route lookup remained duplicate work before fresh authorization.

The bounded parsed-workload cache now exposes only the immutable execution
principal for warm routing. The callback still freshly queries current owner/key
under mutation locks, measurement state and complete request/command SHA-256
before any admission write, then freshly checks nodes, artifacts and counted
residents. No live authority result is reused. Cold/unknown routing retains its
readonly lookup. On a warm route the first fresh authorization read is the
liveness check; only its invalidated connection may retry once after rollback.
No later read, admission write, commit or stream is retried. Parsed entries are
stored only after all document and resident checks succeed.

The warm-route first-query regression failed before implementation and passes
afterward, including an actually terminated idle PostgreSQL connection. A
changed command principal is refused before engine IO despite a matching old
submitted routing digest. The focused suite passes 32 tests with zero skips
in 27.94 s, and strict typing passes 1,017 source files. The ARM64 API builds
and passes container policy. V25 starts with the unchanged frozen workload
and acceptance limits; full shared qualification and release gates remain open.


V25's steady diagnostic window had 220 requests, 90.91% at or below 20 ms;
it was ended inconclusive and its owned resident drained. The private generate
route now observes its already-parsed ASGI receive channel with one background
watcher. Every stream frame still checks a disconnect event; this removes
repeated middleware receive polling. A receive failure fails closed, and normal
completion/cancellation cancels and awaits the pending watcher. Ordinary
gateway requests retain their existing request/disconnect handling.

Three watcher tests failed before implementation and pass afterward: a
disconnect while upstream waits prevents content delivery, request completion
cancels a pending receive, and a broken receive fails closed. Full focused
regression passes 35 tests, zero skips, in 27.44 s. Strict typing passes
1,018 source files; Ruff/formatting and the ARM64 API build/container policy
pass. V26 starts with the same frozen workload and acceptance limits. Owned
acceptance key usage was 51,860,962 of 100,000,000 tokens before this attempt,
with sufficient headroom for both remaining Studio A profiles; no quota or
entitlement was widened.


V26's private-route disconnect watcher allowed a steady 216-request diagnostic
window with 98.61% at or below the 20 ms gateway histogram edge. The longer
821-request cumulative diagnostic had 96.35% at or below that edge. These
windows establish progress, not completed baseline/mixed qualification.
A setup error was found before attempting to publish a profile: 512 updates
finish before the full 900-second mixed phase, which the existing coverage
guard correctly refuses. V26 was ended inconclusive and its resident positively
drained; no qualified profile was published.

V27 collects fresh matching exclusive evidence for DPO QLoRA / 4,096 updates
on Studio A and ORPO QLoRA / 5,120 updates on Studio B. The lengths derive from
the measured 512-update durations (132.44 s and 104.76 s respectively), with
margin over the full 900-second mixed phase. Actual duration and full-phase
coverage still must be verified, rather than inferred. Timed gateway phases
wait behind a private barrier while final automated release checks run. Dense
profile run lengths will likewise be based on measured native durations.


### Final QLoRA memory evidence and automated gates

- `018-coex-dpo-qlora-coire-edge-a`: 4,096 actual native updates in 1079.325403 s, peak 3,504,506,968 bytes, zero swap growth, thermal safe; trainer positively stopped. Matching shared qualification remains pending.
- `018-coex-orpo-qlora-coire-edge-b`: 5,120 actual native updates in 1076.106342 s, peak 2,376,763,168 bytes, zero swap growth, thermal safe; trainer positively stopped. Matching shared qualification remains pending.

Final source passes 3,099 CPU tests (two existing optional skips; 629 deselected), 228 web tests, Ruff format/check, strict mypy across 1,018 files, TypeScript, web lint, OpenAPI freshness, dependency pins, all 16 ARM64 production image builds/policy checks/critical vulnerability scans/SPDX SBOMs, and all 14 Prometheus alert files plus feedback/training alert tests. The full integration suite is still running; T076 remains unchecked until its required gates pass.


Full integration release suite passed: **517 passed, 39 skipped, 3,174 deselected**, 220 warnings, 1,589.83 seconds. The 39 skips comprise 34 existing optional live/manual/paid gates and five existing native evaluation opt-in cases; required 018 native acceptance ran on Studios with no required skips. T076 automated release checks are complete. Logs remain in the private acceptance directory.


Dense native pilots completed, positively stopped: ORPO dense LoRA / Studio A / 512 updates took **98.826494 s**, peak **3,331,508,696 bytes**; DPO dense LoRA / Studio B / 512 updates took **176.576120 s**, peak **5,225,975,024 bytes**. Both had zero swap growth and safe thermal state. Next matching exclusive runs use 5,632 and 3,072 updates respectively, subject to verifying actual native duration before mixed qualification.

After all final release checks, the checked API `cd1517c4ab8e`, scheduler `7a7a20c41276` and web `22cfb7f08144` ARM64 images replaced the same owned services with unchanged secrets, volumes, networks and isolation. The V27 release barrier opened for sustained QLoRA qualification on both Studios.


V27 simultaneous baselines: Studio A held approximately 98–99% at the 20 ms gateway edge, while Studio B had approximately 93% and did not meet the unchanged gateway limit. The owned B actor was audited inactive; its measurement ended inconclusive, and its exact resident was positively drained. No successful B profile is claimed. Remaining shared gateway phases run serially; matching exclusive native memory measurements may run on the other Studio. The serial workflow requires complete-phase conservative gateway evidence and native-active request evidence before releasing each next shared phase.


V27 Studio A also failed the zero-error gate: baseline requests at 500 ms arrivals produced `concurrency_busy` failures. Fresh recent UsageRows measured complete request p95 **0.553568 s** (max **0.908198 s**) and first-token p95 **0.510723 s** (max **0.874325 s**), so the imposed two-request/second workload was unsustainable before native training began. The owned actor was audited inactive and the attempt ended inconclusive with positive resident drain. No qualified profile is claimed.

V30 declares **4,000 input tokens, one output token, concurrency one, 2,000 ms arrival interval** per resident. This bounds qualification to 0.5 requests/second per Studio. Full 900-second baseline/mixed phases, ≥100 completed requests, zero failures, 1.5-second chat p95, 20-ms gateway p95, native-training coverage and zero-swap/safe-thermal gates remain unchanged. Matching isolated memory evidence is keyed to the exact native training configuration; memory runs contain no chat workload.


V30 produced no overlap failures under the revised 0.5-Hz workload, but Studio A gateway overhead remained outside the 20-ms gate. Both attempts ended inconclusive with audited actor stop and positive exact resident drain. Repeated telemetry process creation was replaced with one persistent read-only collector; this does not relax the gate.

V32 private stage timing (preserving the immutable routing wrapper and fresh authority checks) sampled 96 warm requests at gateway p95 **23.578071 ms**. Independent first-frame credential verification started before request-lease commit, competing with admission for database/event-loop work. The private path now signals upstream handoff after lease commit and starts that same fresh check while model work runs; first output still waits for its result. Ordinary streams preserve their early check. A direct-source fallback still rechecks before forwarding, and pre-handoff failure cancels the pending checker. TDD: three new tests failed before the change, then all four new tests and 22 existing stream/disconnect tests passed. T076 is reopened because source changed; release gates must be refreshed after the final candidate.


V33 handoff scheduling diagnostic measured **20.451097 ms p95 over 124 warm requests** (91.94% at or below 20 ms) under instrumentation. This is inconclusive performance evidence, not qualification. Its owned actor was stopped through the audited route; the measurement ended inconclusive and the resident was positively drained. The checked candidate API normal entrypoint was restored and both private profiler files were removed from the dataset volume. Five handoff tests, 13 fresh-authority PostgreSQL cases and strict mypy (1,019 files) pass. Full CPU/integration/image checks are being refreshed. Both Studios now collect matching long dense native memory evidence while timed shared phases remain held.


Final handoff source checks: **3,104 CPU tests passed**, two existing optional skips, 629 deselected, 219.94 s; **1,019 files** pass strict mypy; **1,775 files** pass formatting, plus Ruff/OpenAPI/pin checks. All 16 refreshed ARM64 images pass build/policy/zero-CRITICAL scan/SPDX SBOM gates. Web (228 tests, lint, TypeScript) and Prometheus rule gates remain green on unchanged source. The refreshed full integration suite is still running, so T076 remains open.

Studio B matching exclusive DPO dense LoRA memory evidence: **3,072 updates**, **1,053.059539 s** actual native training, peak **5,215,079,664 bytes**, zero swap growth, safe thermal state and positive stop proof. This is memory evidence only; shared qualification remains pending.


Studio A matching exclusive ORPO dense LoRA memory evidence: **5,632 updates**, **1,110.011098 s** actual native training, peak **3,337,832,944 bytes**, zero swap growth, safe thermal state and positive stop proof. All four exact bare-initialized configurations now have matching isolated native durations above 1,000 seconds. Both dense native workers are stopped, and host acceptance drivers wait behind the barrier. These records establish memory/duration support, not shared gateway qualification.


Refreshed full integration suite passed: **517 passed, 39 existing optional skips, 3,179 deselected**, 220 warnings, **1,606.07 s**. The 39 optional skips remain 34 existing live/manual/paid cases and five existing native evaluation opt-in cases; required 018 native acceptance has no required skips. All current-source automated release gates pass. T076 is complete; T074 and final reconciliation remain pending. V34/V35 timed shared phases are released only after these checks, using checked normal service entrypoints and matching long isolated evidence.


V34 normal-service simultaneous baselines: Studio A had 199/199 requests within the 20-ms histogram edge; Studio B had 195/209 (93.30%) and failed the unchanged gateway budget. B was ended inconclusive by audited owner deactivation, with positive resident drain. A continues. Remaining acceptance uses **one Studio shared workload at a time**, 0.5 requests/s, concurrency one, 4,000 input / one output token; simultaneous two-Studio shared traffic is not qualified. This scope is explicit, and no failed B profile is claimed. All current-source automated release gates remain green.


V34 Studio A also failed the unchanged gateway overhead gate after B drained: late windows fell to approximately 65–85% at or below 20 ms, with about 84.4% over 782 requests. Its owner was audited inactive; the durable measurement ended inconclusive, and the exact resident was positively drained. Neither V34 profile is accepted. The current source release checks remain green, but T074 and T077 remain open.

Remaining-cost investigation: the fresh complete request/command documents were 9,990 and 50,244 bytes; PostgreSQL SHA-256 query execution took 0.304–0.366 ms in five measured runs. A generated-digest schema change is therefore not justified. PostgreSQL `pg_test_fsync` on a disposable file in the existing WAL filesystem measured approximately 415 microseconds per durable 8-KiB fdatasync. The benchmark removed its file. `fsync`, `synchronous_commit`, and `full_page_writes` remain on; `wal_sync_method=fdatasync` and `commit_delay=0` remain the defaults. A metadata-only isolated transaction client measured durable commit p95 1.398423 ms over 100 transactions. V37 adds private driver-commit and ORM-flush timing to diagnose the difference from the live admission path; instrumented samples are diagnostic only and cannot qualify a profile. No implementation or migration changed during this investigation.


The raw-column inventory benchmark did not improve on ORM materialization (100 executions each, two live rows: ORM median/p95 0.855049/1.076466 ms; raw columns 1.054550/1.284593 ms). That source change was rejected before implementation. Driver-level V37 timing confirmed negligible ORM flush work and variable durable commit latency. A separate idle-interval transaction experiment showed slower commits after two-second pauses than after 100-ms pauses; this is diagnostic evidence, not a qualified latency result. V37 was ended inconclusive through audited owner deactivation and its exact resident drained. Its temporary entrypoint is restored to the checked normal API image, and the profiler output removed from the dataset volume.

V38 declares **one request/s per resident**, concurrency one, 4,000 input tokens and one output token. Previous completed-request timing supported a one-second interval (observed maximum 0.908198 s), whereas a 500-ms interval allowed overlaps and failures. Full 900-second baseline and mixed phases, native training throughout mixed, zero failures/swap, and unchanged 1.5-second TTFT / 20-ms gateway p95 limits still apply. A profile can only claim its exact measured workload; neither earlier failed 0.5-request/s attempts nor generic idle workloads are accepted by a later successful run. No source, schema, durability, or isolation setting changed; all current-source release gates remain green.


V38 normal-entrypoint, one-request/s preliminary baselines were ended **inconclusive**, with audited deactivation of both owned actors and positive scoped resident drains. Approximately 522 B requests were 94.44% at/below 20 ms and 502 A requests were 95.22%; incomplete phases do not establish either acceptance or a final phase p95. No profile is claimed from this attempt.

The private gateway now composes the existing fresh-key rate/monthly-quota admission and request-lease writes into one dependent PostgreSQL statement. Owner/key mutation locks, canonical node locks and complete fresh resident checks still precede every write. A refused counter admission yields no lease or hold/in-flight change; rate-first error precedence is retained without retrying admission. No refused request is sent upstream or settled as usage. Non-key and ordinary public admission paths preserve their existing behavior. The regression tightened the cold exact-admission round-trip budget from six to five: it failed for the API-key case before the change (one pass / one failure), then both variants passed. Additional real-Postgres assertions prove rate, quota and simultaneous-limit refusal leaves leases, usage, rate counters, hold usage time and in-flight count unchanged. Nine targeted Postgres cases and strict mypy across 1,019 files pass. T076 is reopened; full CPU/integration/static/image checks are refreshing before this candidate may enter timed shared qualification. Node runtime and native memory evidence are unchanged.


Atomic-admission release retry: all 16 current-source ARM64 image gates passed. The first full CPU run reported 3,103 passes and one pre-existing filesystem-test failure: the private receipt runner inherited `umask 077`, so `mkdir(mode=0755)` produced a `0700` fixture before the foreign-owner refusal assertion. This was runner configuration, not an application permission mutation. The child test processes now use the repository's normal `umask 022`, while private receipt directories/files retain `0700`/`0600`. No test or production permission check was loosened. The full CPU suite is rerunning; the full integration suite follows it using the same normal test umask and the already-passed current images.


Refreshed atomic-admission source release checks passed: **3,104 CPU tests**, two existing optional skips, 629 deselected, 191.08 s; **517 integration tests**, 39 existing optional skips, 3,179 deselected, 1,579.95 s. Strict mypy passes all 1,019 files; formatting, Ruff, OpenAPI freshness and dependency pins pass. All 16 ARM64 production images pass build/policy/zero-CRITICAL scans/SPDX SBOM gates. Web (228 tests, TypeScript and lint) and Prometheus gates remain green on unchanged source. No required 018 gate is skipped. T076 is complete. Checked API/scheduler/web images replaced only the owned services, preserving their secrets, volumes, networks and isolation. V40 starts both exact bare QLoRA profiles at one request/s per resident; V41 dense profiles remain held until QLoRA completes and its full phase overhead evidence passes.


Phase-overhead evidence collection now records the receipt time of the overhead-counter response on the API's clock. A pre-phase snapshot is eligible only if that response arrived before the phase began; exporter lag can only add earlier events to the delta, never exclude a phase event. Legacy observations retain the original 60-second start margin. The post-phase snapshot retains at least 60 seconds of SDK/export/scrape allowance. Every interval outlier is still conservatively assigned to the phase, with only the phase's completed requests as the denominator; neighboring fast requests cannot hide a failing phase. This improves boundary accuracy without changing source, phases, coverage or the 20-ms threshold. The single persistent collector was replaced in place during baseline; no extra repeated process-start observer is running.

### Prepared reservation lease correction — 2026-10-09

V40 ended inconclusive at baseline-to-training transition: Studio A returned 409 on start. The scheduler watchdog renewed only running attempts, leaving the prepared reservation's initial 30-second lease unrenewed throughout the 900-second baseline. The node correctly refused expired work. Studio B was stopped through audited owner disable; both measurements are inconclusive and both exact residents positively drained. No shared profile is accepted from these results.

The watchdog now renews prepared and running reservations after the same fresh authority, binding and safe-resource checks. Node lease expiry and stopped-work rejection remain unchanged. A regression test failed for prepared work before the fix (one failure, two passes), then all 22 measurement unit tests passed, including prepared/running renewal and no renewal of stopped work. Full automated gates and checked images are being refreshed before new idempotency keys and fresh full-duration acceptance.

Prepared-lease refresh checkpoint: full CPU suite passes **3,107 tests**, two existing optional skips, 629 deselected (214.43 seconds). Formatting, Ruff, strict mypy, OpenAPI freshness and dependency pins pass. All 16 ARM64 image build/policy/zero-CRITICAL/SPDX gates pass. The full disposable integration suite is still running; T076 remains open until its completion.

### Prepared-lease full release gates — 2026-10-09

Full disposable integration passes **517 tests**, 39 existing optional skips, 3,182 deselected (1,584.77 seconds). The skip categories remain the pre-existing optional live/manual/paid and opt-in native evaluation tests; required 018 native tests already passed on the Studios without required skips. With 3,107 CPU tests, static/generated checks, unchanged passing web/observability checks and all 16 refreshed image gates, T076 passes again.

Checked normal-entrypoint API/scheduler/web images are deployed for fresh V42 QLoRA and V43 dense LoRA acceptance, using new idempotency keys, one request/s and the unchanged full-duration thresholds. Both Studios retain tested native environment `0.2.0-9b778b3c4c48`; no native source changed for the scheduler lease correction. V42 is running; V43 waits for complete QLoRA evidence. T074/T077 remain open.

### Input-delivery lease coverage — 2026-10-09

V42 exposed a preparation-stage gap before baseline: Studio A preparation and repeated input delivery consumed the initial lease before the watchdog was started. Its first renewal returned 409 at 22:15:46 UTC. This is inconclusive preparation, not a measured performance failure. Studio B was stopped through audited owner disable; both positive stop proofs and both exact resident drains are recorded privately.

The scheduler now starts the watchdog after native prepare and keeps it active throughout input delivery, baseline and training. Delivery races the watchdog; authority/resource failure cancels pending delivery and renewal, then follows the existing independent stop lane. Successful preparation hands the still-running watchdog to the existing baseline/mixed lifecycle. Prepared and running reservations retain 30-second leases; expired/stopped work remains refused. No node source or admission limit changed.

Two regression cases reproduce the former delivery-before-watch ordering as timeouts, then pass with the repaired ordering, including cancellation on authority withdrawal. All 24 measurement unit tests and strict mypy (1,019 files) pass. A first refresh stopped at a Future/Task generic typing mismatch; both lanes now use Task[None], with no type-ignore or weakened checks. Full release gates and checked images are refreshing before fresh V44/V45 measurements; T074/T076/T077 remain open.

### Fresh disk-accounting index and native preparation proof — 2026-10-09

Short exclusive preparation smoke passed on Studio B but Studio A's watchdog timed out while input staging held the shared admission lock. An independent authenticated status observer measured 11.50 and 11.95 seconds while prepared, then 0.07–0.36 seconds after stop. The retained Studio A inventory contains 117 training attempts, 72 measurements and 247 artifact directories. Fresh disk accounting repeatedly scanned remaining files for each ownership scope under the common lock.

Quota accounting now indexes each freshly observed file by its path ancestors once, then consumes matching unclaimed files for each owner. It preserves fresh stat/link checks, overlapping-scope de-duplication, max(envelope, actual), retention credit and unknown-owner accounting; no quota cache, limit change or history deletion. Two scaling/fresh-byte regressions failed before the change (7,380 path checks for 120 owners), then passed. All eight native-registration contracts and 89 focused reservation/disk/quota/cleanup tests pass; strict mypy passes 1,019 files.

On the actual retained Studio A metadata, old and indexed calculations both account **99,032,688 bytes**; time falls from **6.254757 seconds** to **0.086786 seconds**. This is a read-only metadata diagnostic, not a model or accepted shared-performance run.

Both Studios staged and smoke-verified immutable runtime **`0.2.0-e5953d0395eb`**, then activated it through the existing service-account process and unchanged launchd path after all engines were stopped and both journals had zero unreleased reservations. Prior `0.2.0-9b778b3c4c48` remains available. Installed source comparison shows only `coire_node/reservations.py` plus the additive unused `TrainingMeasurementGenerateRequest` class in core; native workers, loss hooks, iterator, checkpoint and engine sources are unchanged.

Fresh authenticated exclusive 16-update memory smokes pass: Studio A DPO measurement **2c9841d0-0338-40d6-be25-77d4083084c0** and Studio B ORPO **a57eb68a-dc6c-4553-a56a-804d8b294a83**, both zero swap and safe thermal. These prove preparation/start/stop on the repaired paths, not full shared profiles. The unchanged API's full integration refresh is finishing; current full CPU/static/image gates and measurement transaction checks are refreshing for the node-index change before V44/V45.

### Final disk-index release checkpoint — 2026-10-09

Current full CPU/contract suite: **3,111 passed**, two existing optional skips, 629 deselected, 214.84 seconds. Formatting, Ruff, strict mypy (1,019 files), OpenAPI and dependency pins pass. All 16 current ARM64 image build/policy/zero-CRITICAL/SPDX gates pass. Unchanged web (228 tests/TypeScript/lint) and observability (14 alert files plus rule tests) checks remain passing.

The unchanged API's full disposable integration run passes **517 tests**, 39 existing optional skips, 3,184 deselected, 1,584.58 seconds. The node-only disk-index delta is additionally covered by current full CPU tests, 89 focused node reservation/disk/cleanup tests, both actual Studio preparation smokes, rebuilt node test image and **seven fresh real-Postgres measurement transaction tests** (9.55 seconds). No node tests are marked integration in the separate node collection; native required evidence runs on Studios. Required 018 gates have no failed/skipped results. T076 is checked.

Checked normal-entrypoint API/scheduler/web images are deployed with native environment `0.2.0-e5953d0395eb`. Fresh V44 QLoRA qualification is running; V45 dense LoRA waits for full phase-overhead and native-presence proofs. The 900-second phases, ≥100 requests, zero failures, 1.5-second TTFT p95 and 20-ms gateway overhead p95 remain unchanged. T074/T077 are open.

### V44 status and deterministic principal identity correction — 2026-10-09

V44 Studio A DPO QLoRA completed its full 900-second baseline with 898 completions and two `concurrency_busy` refusals at the declared one-request/s, concurrency-one workload. It ended inconclusive before native training started. Its gateway histogram also exceeded the 20ms requirement; no profile is accepted. Studio B ORPO QLoRA continues its actual full mixed phase and native 5,120-update execution independently after the paired host driver exited. The metrics observer remains active; final acceptance requires both phase-specific gateway evidence and full native coverage.

A fresh two-second-arrival retry (V46, measurement `6517762b-690a-4c47-9c13-413a4dcf3dc6`) submitted with the original multi-scope administrator exposed a distinct cold-route identity mismatch: every completion was refused with 404. The original administrator stays active. Its exact native prepared attempt was stopped through the authenticated, fenced node control path; a positive stopped receipt is retained. This retry is unqualified and must not be used as performance evidence.

The frozen principal digest previously JSON-sorted dictionary keys but retained arbitrary frozenset array order. Four independent Python hash seeds produced four different digests in a regression test. `payload_digest` now sorts only the principal's unordered fields; ordered fields and all non-principal training-spec serialization remain unchanged. The hash-seed regression now passes, alongside explicit changed-authority and historical ordered-document checks. Full automated gates and checked images are refreshing before new full-duration runs. Two-second arrival is a proposed exact profile rate, not a schema default or relaxation of any duration, sample count, zero-failure, native-coverage or latency gate.

### First sustained shared profile accepted — 2026-10-09

Studio B ORPO QLoRA V44 measurement `efea7897-16ad-4b23-b4ec-0b5f3f5271f1` produced profile `07a58b7e-efe4-4f49-a0f5-9bd988289e48`. Exact workload: one single-Studio resident, concurrency one, 1,000ms arrival, 4,000 input tokens and one output token; 900 baseline and 900 mixed completions, zero failures. Native execution completed 5,120 updates and spans the entire mixed phase; 900 completed gateway requests lie wholly inside native execution, with active-training first-token p95 **0.354656370s**. Zero swap growth, safe thermal state, measured memory within the approved envelope and positive stopped receipts are retained.

Conservative phase-specific histogram proof uses a snapshot received before each phase and at least 60s export/scrape padding after it; all padded-interval >20ms outliers are assigned to the phase. Baseline has 10/900 conservative outliers (**98.8889% ≤20ms**); mixed has 45/900 (**95.0000% ≤20ms**), meeting the p95 requirement without dilution by neighboring fast requests. The exact profile is accepted; broader rates, targets, objectives, parameterizations and training configurations are not inferred. Private complete native observations, phase boundaries, digests and histogram receipts remain outside Git.

Subsequent principal-only hashing correction leaves this single-scope frozen identity's digest unchanged. Checked corrected API/scheduler/web images are now deployed. The original-administrator V46 failed retry is finalized inconclusive through scheduler restart and its owned resident is draining before a new exact attempt. Studio A QLoRA and both dense LoRA profiles remain pending; T074/T077 remain open.

### Principal-identity full automated release gates — 2026-10-09

Current source passes **3,114 CPU tests**, two existing optional skips, 629 deselected (212.29 seconds); the full disposable integration suite passes **517 tests**, 39 existing optional skips, 3,189 deselected (1,597.96 seconds). Required 018 native cases previously passed with zero required skips; optional legacy/live/paid/evaluation categories remain explicitly identified rather than treated as acceptance. Formatting checks 1,776 files; Ruff, strict mypy (1,019 source files), generated OpenAPI freshness and dependency pins pass. All 16 current ARM64 production images pass build, policy, zero-CRITICAL scan and SPDX gates; unchanged web (228 tests, TypeScript/lint) and observability gates remain green. T076 passes. T074/T077 remain open for the three remaining exact shared profiles and final cleanup/reconciliation.

### Two further sustained profiles accepted — 2026-10-10 UTC

- **coire-edge-a dpo qlora**: measurement `52662245-cfec-4330-93a6-64ea6280fdc5`, profile `83ee4aae-4332-4f64-9d56-c933ae5b3068`; 4,096 native updates. Full 900-second baseline and mixed phases each completed 450 requests with zero failures at two-second arrival, concurrency one, 4,000 input / one output token. Active-training TTFT p95 0.508946918s; conservative ≤20ms fractions baseline 98.888889%, mixed 95.333333%. Exact native coverage, zero swap growth, safe thermal state and stopped receipts pass.
- **coire-edge-b dpo lora**: measurement `aeec8a13-13c8-47cc-84e7-d40cbc1fd117`, profile `4ba2d5a3-9ae7-4264-9293-f0c3bebcaa67`; 3,072 native updates. Full 900-second baseline and mixed phases each completed 450 requests with zero failures at two-second arrival, concurrency one, 4,000 input / one output token. Active-training TTFT p95 0.393901280s; conservative ≤20ms fractions baseline 99.333333%, mixed 97.777778%. Exact native coverage, zero swap growth, safe thermal state and stopped receipts pass.

Both exact residents are positively drained. Studio A dense ORPO is the only remaining sustained profile; it now runs independently at the same two-second arrival workload. T074/T077 remain pending; all current automated gates pass.

### Dense ORPO V48 overhead refusal and same-image retry — 2026-10-10 UTC

Studio A dense ORPO V48 measurement `3c04ed82-12e6-45db-85d7-564c04883705` accumulated at least 23 >20ms gateway samples before its declared 450-request baseline could finish. Even an entirely fast remainder could not meet the maximum 22-outlier allowance. The owned actor was disabled through the audited admin API; the watchdog finalized the attempt inconclusive with a positive fenced stopped receipt (`pid=null`, no native trainer launched). Its exact resident is positively drained. This is failed performance evidence, not an accepted profile; raw counters and receipts are retained privately.

The API was restarted with the **same checked image, entrypoint and environment**, and the owned actor was restored through the audited API. V49 retries the identical two-second-arrival, concurrency-one, 4,000-input/one-output workload and 5,632-update configuration in a fresh normal process. No source, rate, latency threshold, sample count or duration changed. All automated gates and the three accepted profiles remain valid; final dense ORPO acceptance remains pending. A read-only CPU diagnostic found a two-CPU API limit and zero CFS throttling, so resource limits were not changed.


### Admission-stage diagnosis and current release checks — 2026-10-10 UTC

Studio A dense ORPO V49 (`458fbb67-0d17-4517-b453-a5d201067bd0`) exceeded the baseline overhead budget after the same-image API restart. V50 (`e3bfe32b-8f3f-4b78-a03c-493922f68886`) moved status polling to the original administrator while retaining the owned A principal for measured generation; it also exceeded that budget. Both attempts ended inconclusive before native training and their exact residents were positively drained. Neither restart nor a separate observer established a performance fix. Historical receipts remain private and no shared profile was accepted from them.

The existing gateway measurement span now records content-free admission-stage events (`stage`, `duration_ms`) for initial authority, canonical node locking, fresh inventory, lease acquisition and commit. Authorization, lease semantics, durability and thresholds are unchanged. A deliberately nonqualifying 60-second diagnostic (`0575ee04-f11f-4ab1-9033-7527daf31782`) captured 30 requests at two-second arrival, then ended inconclusive through audited owner disable and positively drained its resident. Stage medians were authority 3.746 ms, node lock 0.575 ms, inventory 4.246 ms, lease 1.021 ms and commit 6.038 ms; respective observed p95 values were 6.680, 1.308, 5.788, 1.429 and 8.900 ms. This identifies where time is spent, not a demonstrated cause or qualifying performance result. The read-only node-lease snapshot diagnostic was about 2 ms on either node, with zero API CFS throttling; no pool/resource/index/durability changes were inferred.

Current-source release checks pass: 3,114 CPU tests (two existing optional skips), Ruff/formatting, strict mypy across 1,019 files, OpenAPI freshness and pinned images. All 16 current ARM64 production images pass build, policy, zero-CRITICAL scan and SPDX SBOM gates. The nine affected real-Postgres measurement transaction/usage cases pass on those current images. The full 517-case integration suite passed immediately before the timing-only change; its 39 optional skips remain separate from the required native Studio evidence. Unchanged web and alert gates retain their recorded passing evidence. T076 is complete.

V52 tests the remaining exact Studio A dense ORPO configuration with a declared four-second arrival interval (0.25 request/s), concurrency one, 4,000 input tokens, one output token, 5,632 training updates and unchanged 900-second baseline/mixed phases. The rate is part of the prospective workload identity; this attempt cannot qualify either failed two-second-arrival workload. All latency, minimum sample, native coverage, safe thermal, zero swap-growth and stop-proof requirements remain unchanged. Acceptance is pending actual phase-specific receipts.


### Failed shared gate, bounded diagnostics and final cleanup — 2026-10-10 UTC

V52 Studio A dense ORPO (`cea83dd5-1f48-4645-ae0d-62b7a68237a8`) also failed its prospective four-second-arrival workload: 12 observed gateway samples exceeded 20 ms after 159 observed baseline completions, exceeding the maximum 11 outliers permitted by the planned 225-request phase. It was stopped through audited owner deactivation before native training, ended inconclusive, and its exact resident was positively drained. No four-second-arrival profile was accepted. V48/V49/V50/V52 remain failed/inconclusive evidence, not support declarations. T074 remains unchecked.

A separate 60-second metadata diagnostic (`d183a499-26d7-4f46-8a50-f05b677866bf`) sampled PostgreSQL wait categories without retaining query text. It observed WAL-sync waits during commits up to 6.716 ms; the authority query itself executes in approximately 0.25–0.39 ms according to read-only EXPLAIN evidence. A subsequent nonqualifying CPU diagnostic (`5ffa401c-4f10-4a9b-87f6-8ff0fd3f3014`) found no dirty/new/deleted ORM objects at commit. Median commit wall/CPU time was 5.020/0.514 ms; durable-commit median was 4.747 ms, with p95 7.826 ms. Authority and inventory median wall/CPU times were 3.207/1.556 and 3.538/1.753 ms. These observations locate costs without proving a performance fix. Both diagnostics ended inconclusive and drained their residents; the normal checked API entrypoint was restored. No durability, resource limit, authentication, lease, threshold or sample requirement was changed.

Two database-batching prototypes were evaluated only in disposable PostgreSQL. Canonical node-lock/inventory batching passed existing user/key gateway cases and a deterministic concurrent-writer test proving a fresh post-lock-wait snapshot refreshes an already-loaded ORM row (three passing cases). Its 60 interleaved warmed samples per path showed separate/combined medians 3.257/3.400 ms, p95 4.470/4.040 ms. Broader authority-plus-locked-inventory batching passed both gateway cases, including existing mutation/refusal checks; 40 warmed samples per path showed medians 4.746/4.551 ms, p95 6.044/6.246 ms. Neither establishes a useful improvement. The prototypes were not applied to application source, migration 0033 or the live database. All scripts and raw receipts remain private.

Final cleanup re-read and independently verified the three accepted profiles, their full native observations, stop proofs and phase-specific conservative 20-ms histograms. Only the two owned acceptance actors were deactivated and all their API keys revoked. The original administrator and privacy owner remain active. Both training admission flags are restored to false in checked normal API/scheduler images; the owned CI registry is stopped. Both Studios have no remaining MLX serving or preference-training processes. A read-only database check confirms all 69 measurements owned by the acceptance actors are terminal and all 69 corresponding holds released. Published dataset `11d70037-2c0c-5216-9bf5-868ef97a03d5` remains ready, with its original source metadata and physical bytes both matching SHA256 `52426559bf6cbec8c97851ff7a93556ebb226c00d73a981b72dcb02fd1d43fbf`. The dataset API is intentionally unavailable with training disabled, so final preservation was checked through metadata and a content-free file hash. The privacy owner remains opted out at generation **8**: generation 7 was the earlier chosen-context test, followed by its final opt-out. No preference mutation was made during cleanup.

At the earlier handoff, implementation stopped at the failed sequential acceptance task under the invoked skill. The user subsequently explicitly directed continuation; the resumed investigation above and subsequent entries supersede that stop. The draft PR exposes completed implementation and passing automated/native evidence while retaining the failed T074 gate. This is not a completed release, and no unsupported shared configuration is enabled.


### V57 inconclusive stop and V58 diagnostic retry — 2026-10-10 UTC

V57 Studio B dense ORPO memory measurement `9b70a4b8-a25d-4f28-a3b2-de6fc1c3f0ce` ended inconclusive after approximately 779 seconds. The native worker positively stopped at update 3,564 after cancellation by Core, before the requested 5,632 updates; no complete memory observation or shared profile was published. The controller restored both admission flags to false, deactivated its owned actor and revoked the issued key. The frozen binding still matched on a later read-only check, and the execution trace contained no recorded transport exceptions. Thirty subsequent resource-status samples were nominal with zero swap usage, unchanged pre-existing swap-out counter (1,687,552 bytes), and timestamp ages of 8.21–15.456 ms. These later observations do not prove the original watchdog failure cause. The previous controller did not preserve scheduler logs before recreation; V58 corrects that diagnostic gap.

V58 retries the identical Studio B configuration with fresh owned credentials. The checked scheduler image is started through a temporary Python wrapper which adds only content-free exception-type/traceback-frame output to the existing inconclusive handler. The controller saves scheduler/API logs before cleanup, then restores normal entrypoints and disabled flags. All thresholds, fresh authority checks, resource checks, leases and native runtime remain unchanged. T074 remains unchecked.


V58 Studio B dense ORPO memory measurement `b56cc124-1fb6-4404-81f1-6f42a4413830` succeeded: 5,632 updates, peak footprint **3,306,506,712 bytes**, zero swap growth and safe thermal state, with a positive stopped status. Memory profile `9b8ea59c-9f02-4be1-80c4-2200d363b78d` is exact Studio B evidence. Fresh coexistence measurement `950f555c-a246-4fa1-91fa-8aec8cfff430` then began using resident `3e7c1aac-dff5-4d06-be91-557ca784c8f4`, two-second arrival, concurrency one and the unchanged full-duration protocol. Shared acceptance remains pending.


V58 coexistence baseline exceeded the unchanged gateway-overhead budget: 24 exported samples above 20 ms after 303 observed requests, exceeding the maximum 22 permitted by its prospective 450-request phase. The owned actor was withdrawn through the audited API; the measurement ended inconclusive before training, and its exact resident positively drained. Scheduler failure-frame diagnostics show the expected live-authority refusal after withdrawal. The controller preserved logs, revoked its issued key and restored normal entrypoints/disabled flags. The successful exclusive Studio B memory evidence remains valid; V58 supplies no shared admission profile.

V59 prospectively declares **750 ms arrival, concurrency one** for the same Studio B dense ORPO configuration, using the exact valid B memory evidence and checked normal entrypoints. The earlier 500 ms diagnostic had successful durations up to 530 ms and genuine saturation on overlapping calls; this new cadence gives completion room while testing a busier workload. The unchanged full 900-second phases, ≥100 requests, zero failures, native coverage, 1.5-second TTFT p95 and 20-ms gateway-overhead p95 remain mandatory. No failed attempt is discarded or counted as acceptance.


V59 one-slot 750 ms baseline logged two `concurrency_busy` failures and was withdrawn through the audited API. Content-free usage timings showed 323 successful requests, first-token p95 **484.664 ms**, duration p95 **517.068 ms**, and maximum duration **924.306 ms**; two successes lasted **924.306 ms** and **761.802 ms**, explaining the overlap. One additional request failed during withdrawal. The measurement ended inconclusive, its exact resident positively drained, key revoked and disabled flags restored. Studio A's prior short diagnostic was not a sound duration bound for Studio B; no profile is accepted from V59.

V60 prospectively tests one-second arrival, concurrency one, on Studio B with the same dense ORPO configuration and valid B memory evidence. The declared interval exceeds all successful V59 durations, but that observation alone supplies no acceptance: the full phase, zero-failure, conservative overhead and native-coverage proofs remain required. Normal checked service entrypoints and all safety checks/thresholds remain unchanged.
