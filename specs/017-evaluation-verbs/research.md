# Research: Evaluation Verbs

Date: 2026-10-07. Baseline: `ec8679c1a3ede4c7b4d897ab4c27b0833a517ecc` (016 merged).

Research used the shipped source, tests, constitution and architecture, including two read-only research agents as required by the planning skill. No new dependency or external benchmark is selected. All design questions are resolved below.

## R1 — Execution belongs on the Studios

**Decision:** Add typed evaluation dispatch inside the existing `python -m coire_agent` entrypoint and reuse node-owned sandbox/run orchestration. Core handles admission, immutable configuration, bookkeeping and result validation; all prompting, suite assertions and judge execution occur in the Studio container.

**Evidence:** `apps/coire-api/src/coire_api/cli.py` currently runs `_run_suite` locally. `apps/coire-node/src/coire_node/runs.py` allows only the existing agent entrypoint. `coire_agent/__main__.py`, `coire_scheduler/runs.py` and `coire_api/run_executor.py` already implement request validation, create/start/wait/collect/remove, cancellation and recovery. `coire_node/workspaces.py` currently prepares MCP repositories, so evaluation needs its own bounded preparation operation, not a fictitious repository.

**Rationale:** Satisfies Principles II and II-a while preserving hardened images and lifecycle evidence. **Alternatives rejected:** core CLI runner; a new always-on evaluation service; arbitrary node argv; MCP repository cloning for fixed fixtures.

## R2 — Preserve 016 hashes by versioning intent

**Decision:** Keep existing `TrainingSpec`/`ResolvedTrainingSpec` v1 classes and serialized shapes unchanged. Introduce separate `TrainingSpecV2`/`ResolvedTrainingSpecV2` beside v1 in `models/training.py`, with discriminated document types/helpers at embedding boundaries. The new companion contains only schedule/value types and does not import training documents, preventing circular imports. V2 adds explicit suite schedules; v1 does not serialize empty new defaults. Keep `eval.at_end` and `loss_every_updates` as held-out-loss controls; declared suites always run at successful final completion.

**Evidence:** `coire_core/models/training.py:canonical_sha256`, `coire_api/training/specs.py`, node checkpoint/import/extraction/recovery paths hash normalized and resolved payloads. Adding an empty default field changes existing digests.

**Rationale:** Historical checkpoint identity is binding, not a migration convenience. **Alternatives rejected:** adding `suites=[]` directly to v1; rehashing old rows; conflating held-out loss with task/judge scheduling. Freeze actual 016 serialized receipts/manifests as sanitized regression fixtures before changing parsers.

## R3 — Automatic evaluation is opt-in and independently durable

**Decision:** User confirmed declared-only execution. Each training boundary creates a comparison group and suite runs through durable trigger rows. Final triggers are inserted in the same transaction that makes the mirrored final adapter ready and training succeeded. Evaluation failure leaves training succeeded and appears separately. Baseline is generated after training stops, at the same boundary as the candidate; no baseline cache across boundaries or jobs in 017.

**Evidence:** `coire_api/training/adapters.py:finalize_serving_adapter` and `coire_scheduler/training_controller.py:_finalize` already require ready copies, serving smoke and trainer stop proof.

**Rationale:** A base artifact is immutable; “before/after” describes subjects rather than clock order. Recomputing a small baseline simplifies provenance and retention. **Alternatives rejected:** mandatory evaluation for old recipes; training failure on low scores; a global cache; browser-triggered automatic work.

## R4 — Checkpoint evaluation serializes with training

**Decision:** At a scheduled committed update, atomically create a trigger/pin and an evaluation-owned pause command before acknowledging the checkpoint boundary. Stop all ranks from the common full checkpoint, prove stop, release trainer holds, extract a private evaluation adapter using existing promotion primitives, then run evaluation. Resume only after evaluation cleanup, current measured training admission and live authorization, with no newer admin/protective intent. Process at most one checkpoint boundary per training job at a time.

**Evidence:** `coire_api/placement/service.py:accelerator_load_allowed` blocks new loads under TRAINING holds. `training/checkpoints.py:commit_checkpoint`, `coire_node/training/worker.py` and `training_controller.py` provide commit acknowledgment, coordinated pause, stop proof and resume. `training/service.py` and `_resume_protective` distinguish existing pause owners.

**Rationale:** Spare memory alone is insufficient authorization for candidate/judge loads. **Alternatives rejected:** weakening reverse admission; treating evaluation as an admin/protective pause; polling loss progress; resuming automatically over a newer cancel. The node commit acknowledgment becomes a typed versioned response; old payloads retain old behavior and unsupported v2 combinations fail preflight.

## R5 — Exact target authority and provenance

**Decision:** Resolve model/variant/adapter/artifact identity once and recheck before each phase. Extend internal run purpose and preparation contracts so only scheduler-authored evaluation runs can use ready private/unverified targets. Every phase uses an active human-admin owner, a READ inference grant for the exact target, no user tools and a bounded expiry/budget. Human-owned admin API keys are supported; ownerless legacy/service credentials are refused for submission.

**Evidence:** `coire_core/models/adapters.py:InferenceTarget`, `coire_api/gateway/targets.py` and `run_tokens.py` support exact grants. General `runs.py`/`run_executor.py` require publication, while gateway grants already distinguish admin-owned private targets. `AgentRunRow` requires a real active user.

**Rationale:** Evaluation must assess unverified adapters without granting user write access. **Alternatives rejected:** manufacturing a user, publishing a target as a workaround, granting admin credentials to a container, loosening all run admission.

## R6 — Harness verification remains singular

**Decision:** Reuse four measured categories and the existing minimum-category 0.8 threshold. Only validated complete platform harness evidence reaches `coire_api/evaluations.py:record`. Preserve prior pass on infrastructure error, revoke on measured failure, and never inherit base verification for an adapter. Keep legacy GET history; the old score-submission POST returns 409 `evaluation_execution_required` and creates no verification update. Retain existing positional variant UUID, optional adapter and expected engine-version CLI syntax through submit-and-wait translation.

**Evidence:** `routes/admin_evaluations.py` currently accepts caller scores/verdict; `evaluations.py` writes exact-target verification. `AdapterDetail.evaluation_id` is specifically a harness result link.

**Rationale:** A second unbound score writer would undermine provenance. **Alternatives rejected:** task/judge verification; relabeling imported history as executed; silently accepting old scorecards. Record the compatibility transition in release/runbook documentation and tests.

## R7 — Bounded authored suites, no executable benchmark plugins

**Decision:** Ship built-in manifests/cases under `coire_core/evaluation_suites/`, packaged as shared immutable data for API discovery and Studio execution. Initial IDs: `harness-capability`, `task-coding-instructions`, `judge-rubric`, `judge-pairwise`. Cases are project-authored and released under the repository license. Admin suite registration selects a supported template/version; judge registration binds a distinct exact registry target. No case upload, external benchmark download, dynamic scorer import or generated-code execution.

**Rationale:** Deterministic instruction assertions and applying a validated patch to a disposable fixed fixture give useful local regression scores without an arbitrary code-execution subsystem. **Alternatives rejected:** public leaderboard claims from a tiny subset; large benchmark frameworks/dependencies; arbitrary regular expressions, shell, Python evaluators or repository paths supplied by clients. Declarative assertions are from a fixed allowlist with explicit bounds.

## R8 — Judge identity and bounded scoring

**Decision:** Reject any judge/candidate sharing model ID or immutable base-manifest digest, including aliases, quantized variants under the same registry model and adapters. Apply to rubric and pairwise. Rubric dimensions have integer scores 0–4; normalized aggregate is the equal-weight mean divided by four. Pairwise emits A/B/tie; each case is judged in both orders, disagreements become a tie. Record both presentations, raw validated verdicts and deterministic remapping. Two malformed-output retries per presentation (three total attempts), within the same deadline. The judge has no tools and candidate outputs are delimited untrusted data.

**Rationale:** Conservative identity checks and recorded order make obvious self-preference and position effects reviewable; they do not promise unbiased or statistically significant quality estimates. **Alternatives rejected:** name-only comparison, silent judge fallback, treating malformed output as a loss, hidden retry-until-success.

## R9 — Comparability and contamination

**Decision:** Compute comparison compatibility from suite/content/case/scorer/decoding/tokenizer/template/runtime/harness and judge fingerprints. Exact subject is allowed to differ; missing legacy fields or failed runs produce explicit non-comparability. Rubric/task support per-subject deltas; pairwise supports preference counts only. Record exact normalized input overlap against the selected training inputs and immutable source/split/mixture identities.

**Evidence:** `TrainingExample.content_sha256()` and `SplitManifest.row_content_sha256` identify full examples; full-example equality alone misses reused prompts with different completions. Implement a versioned input-only projection from existing normalized data during the Studio evaluation preparation/contamination step. Bound scanning to the selected 016 dataset limits; do not load a tokenizer on core. Full-example hashes may additionally be reported, never substituted for input checks.

**Rationale:** Suite version alone does not hold experimental settings constant. “No exact overlap found” is a narrow observation. **Alternatives rejected:** semantic duplicate promises; treating missing data as clean; a second dataset registry/analysis verb; conflating score loss with infrastructure failure.

## R10 — Resource admission, protection and recovery

**Decision:** Default to one active evaluation group cluster-wide, executing one model phase at a time plus one bounded sandbox. Use existing ordered node locks, model reservations and sandbox slice. Protect pins/live serving leases; reuse a serving instance only with matching exact identity and validated evaluation coexistence evidence. Unknown liveness holds resources. Real measurements qualify evaluation coexistence; active guard pauses/cancels evaluation admission on stale telemetry, latency/thermal/memory breach. Do not reuse a training profile as if it measured an evaluation workload.

**Decision:** Evaluation pins participate in 016 checkpoint quota/retention under the job lock (three checkpoints, existing byte quota). One boundary at a time provides backpressure; no hidden fourth checkpoint allowance. A schedule unable to reserve its necessary checkpoint fails preflight or pauses visibly. Delete/cancel coordinates with pins; terminal history retains subject snapshots after artifact retirement.

**Rationale:** Bounds make fairness and recovery tractable. **Alternatives rejected:** unmetered automatic gateway loads, using available-memory arithmetic alone, releasing holds on a lost connection, unlimited pending checkpoints. DBOS orchestration stays inside `coire_scheduler`; API transactions record intent only.

## R11 — Private evidence and observability

**Decision:** Keep immutable result/provenance metadata in Postgres. Store bounded raw output evidence in an isolated namespace of the existing private API/scheduler training-data volume (no new service/mount audience), accessed only by authenticated admin endpoints or scheduler-authored bounded workspace staging. Default seven-day expiry, 8 MiB/run, 1 GiB total quota reserved before accepting work; pending phases pin bytes only within persisted deadlines. Clean Studio scratch after collection. Metadata/digests remain after expiry and history views label missing bytes.

**Decision:** Add bounded metrics, baseline Prometheus alerts and jobs-dashboard panels; no IDs in metric labels, no prompt/output content in logs/audit. Structured logs/spans carry evaluation/run/job/target IDs where appropriate. Diagnostic storage remains optional.

**Alternatives rejected:** raw output in telemetry, an unauthenticated blob URL, unbounded database JSON payloads, requiring Grafana/Loki/Tempo for kill/history, retaining evidence indefinitely by retrying.

## R12 — Compatibility and release gates

**Decision:** One reversible migration after 0031 adds evaluation catalog/runs/attempts/results/groups/triggers/events and pin/pause metadata; old immutable training JSON remains untouched. New API/node capabilities negotiate evaluation workload version and training schema v2. New admission defaults off; reconciliation/kill/expiry works even when disabled. Drain/cancel and return evaluation-owned pauses to explicit admin pause before rolling back binaries; preserve schema/history unless explicit migration downgrade is requested.

**Rationale:** Rollout can be stopped without orphaning trainers or losing past evidence. **Alternatives rejected:** passing new fields to old nodes, disabling recovery with admission, deleting 016 rows to downgrade. No constitutional exception or dependency addition is required.
