# Feature Specification: Evaluation Verbs

**Feature Branch**: `feat/017-evaluation-verbs`

**Roadmap ID**: 014a (Phase 4 — Chat UI, images, training)

**Created**: 2026-08-29

**Updated**: 2026-10-07

**Status**: Implementation and task acceptance complete (70/70); review prepared, admissions disabled. Node collection-budget limitation remains recorded.

**Input**: User description: "`coire eval` with harness, task, and judge suites (judge = platform model via the gateway); `TrainingSpec.eval` checkpoint scheduling; before/after scores on adapter rows; console comparison view."

## Overview

Feature 010 introduced one evaluation suite, for harness capability. This feature makes evaluation a first-class verb with three suite types — harness capability, task benchmarks, and model-judged scoring — schedulable at training checkpoints and surfaced as before-and-after comparisons on adapter records. Its purpose is to make publishing a trained adapter a decision supported by numbers rather than an act of hope.

## Clarifications

### Session 2026-08-29

- Q: What are the three suite types for? → A: Harness suites measure whether a model can be driven — tool calling, structured output, edit application, long context. Task suites measure capability on coding and instruction benchmarks small enough to run in minutes on one Studio. Judge suites use a stronger platform model to score outputs pairwise or against a rubric, for qualities the first two cannot capture.
- Q: Which model judges, and how is bias handled? → A: A platform model reached through the gateway like any other client, named explicitly in the suite definition and recorded on every result. A judge model must never judge its own outputs in a pairwise comparison; that pairing is refused rather than silently scored.
- Q: When do evaluations run automatically? → A: At checkpoints declared in a training specification's evaluation block, and at the end of every successfully completed run declaring suites (scope clarified on 2026-10-07). The end-of-run evaluation is what populates the before-and-after comparison the roadmap's acceptance bar requires.
- Q: Is a score ever a gate? → A: Only the harness suite gates, and only for write-capable tasks, exactly as feature 010 established. Task and judge scores inform an admin's publishing decision but never block routing automatically — a benchmark number is not a safety property.
- Q: How are results compared meaningfully? → A: Every result records the model variant, adapter, suite version, engine and harness versions, and run time. A comparison is only offered between results sharing a suite version, so a suite change cannot masquerade as a quality change.

### Session 2026-10-07

The August answers remain the product intent. The following clarification and explicit planning assumptions resolve gaps against the shipped 016 baseline.

- Q: Should automatic task/judge evaluations run only when declared in the recipe or become mandatory for every new training run? → A: Only when declared in the recipe. The earlier phrase “every training run” is scoped accordingly (FR-004); existing held-out-loss-only recipes retain their behavior.
- Evaluation failure does not turn a successfully trained adapter into a failed training job. Its separate evaluation status remains visible and retryable.
- Initial task suites use small project-authored coding/patch and instruction-following cases with deterministic assertions. They are local regression benchmarks, not claims about public benchmark leaderboards. Generated code is never executed.
- A judge cannot score a candidate sharing its registered base-model identity or underlying base artifact, including variants, aliases, and adapters of that base. This conservative rule applies to rubric and pairwise modes.
- Initial checkpoint evaluation serializes all suites with training. Parallel evaluation during an active trainer is deferred until supported by its own admission evidence.

## Scope

Deliver durable admin evaluation commands, versioned harness/task/judge suites, declared checkpoint/final scheduling, and console comparison/history. Reuse dataset analysis and exact adapter verification shipped in 016. Evaluation execution runs on the Studios; core only orchestrates and retains results.

Exclude preference optimization/feedback (018), arbitrary benchmark downloads or executable plugins, custom user repositories, generated-code execution, automatic publishing/routing from quality scores, external judge providers, statistical significance claims, and distributed evaluation workers. Existing sharded base inference may be used through its supported gateway placement; adapters retain 016's single-node serving boundary.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - A trained adapter shows what it changed (Priority: P1)

An adapter record displays base-versus-adapter scores produced automatically at the end of its training run.

**Why this priority**: This is the roadmap's named acceptance bar and the reason the feature exists.

**Independent Test**: Train an adapter with declared task and judge suites and confirm its record shows before-and-after scores for at least one task suite and one judge suite, produced without manual action.

**Acceptance Scenarios**:

1. **Given** a completed training run with an evaluation block, **When** it finishes, **Then** evaluations run automatically against base and adapter.
2. **Given** those results, **When** the adapter is viewed, **Then** base and adapter scores are shown side by side for each suite.
3. **Given** results from different suite versions, **When** comparison is attempted, **Then** it is refused or clearly marked as non-comparable.
4. **Given** an evaluation that fails to run, **When** it fails, **Then** it is recorded as an infrastructure failure distinct from a poor score, while the successful training outcome and adapter remain intact.
5. **Given** a scheduler restart after training finishes, **When** recovery runs, **Then** exactly one automatic comparison group exists for that final artifact.
6. **Given** a 016 recipe declaring only held-out loss, **When** it is replayed or resumed, **Then** its identity and behavior are unchanged and no task/judge suite is invented.

---

### User Story 2 - An admin evaluates any model on demand (Priority: P1)

An admin runs any suite against any model variant or adapter without needing a training run.

**Why this priority**: New models enter the roster continuously and must be assessed before publishing; that path cannot depend on having trained something.

**Independent Test**: Run each of the three suite types against a published model and read the results.

**Acceptance Scenarios**:

1. **Given** a model variant, **When** an admin runs a harness suite, **Then** a scorecard is produced and the verification state is updated accordingly.
2. **Given** a model variant, **When** an admin runs a task suite, **Then** per-task scores are recorded with the suite version.
3. **Given** two candidates, **When** an admin runs a judge suite, **Then** pairwise or rubric scores are recorded naming the judge model.
4. **Given** any suite run, **When** it executes, **Then** it reserves model memory and an evaluation sandbox through the ledger like any other work.
5. **Given** a ready private or unverified target, **When** its admin evaluates it, **Then** evaluation works without publishing it or granting it write-capable user access.
6. **Given** an active evaluation, **When** its admin cancels it, **Then** owned work stops and access is revoked, with retained history and resource release only after stop confirmation.
7. **Given** an unrelated user or a principal without an active admin owner, **When** it submits or reads an evaluation, **Then** access is refused.

---

### User Story 3 - Evaluations run at checkpoints during training (Priority: P2)

A long training run is evaluated at declared checkpoints so a regression is visible before the run ends.

**Why this priority**: Valuable for long runs, but the end-of-run comparison delivers the feature's core value on its own.

**Independent Test**: Declare checkpoint evaluations in a specification and confirm results appear as the run progresses.

**Acceptance Scenarios**:

1. **Given** a specification declaring checkpoint evaluations, **When** a checkpoint is reached, **Then** the declared suites run against that checkpoint.
2. **Given** checkpoint results, **When** the job is viewed, **Then** scores are shown against training step alongside the loss curve.
3. **Given** a declared checkpoint boundary, **When** it is reached, **Then** training pauses at a committed recoverable checkpoint before evaluation loads begin; both ranks stop for data-parallel training.
4. **Given** an evaluation-owned pause, **When** evaluation ends, **Then** training resumes from that exact checkpoint only after current admission and authorization pass.
5. **Given** a newer admin pause/cancel or protective stop, **When** evaluation finishes, **Then** it does not undo that decision.
6. **Given** restart, capacity exhaustion, or checkpoint retention pressure, **When** recovery runs, **Then** pending evaluations remain visible, referenced checkpoints remain protected, and automatic work is neither duplicated nor silently dropped.

---

### User Story 4 - A judge never scores itself (Priority: P2)

A pairwise comparison involving the judge model's own output is refused rather than scored.

**Why this priority**: Self-preference is a well-known and severe bias in model-judged evaluation; allowing it would quietly invalidate every judge result.

**Independent Test**: Configure a pairwise comparison where a candidate is the judge model and confirm refusal.

**Acceptance Scenarios**:

1. **Given** a pairwise suite where one candidate is the judge model, **When** it is submitted, **Then** it is refused with that reason.
2. **Given** any judge result, **When** it is recorded, **Then** the judge model and its variant are recorded with it.
3. **Given** a judge model that is unavailable, **When** a judge suite is run, **Then** it fails as an infrastructure error rather than producing scores.
4. **Given** a differently named variant or adapter of the judge's base, **When** it is submitted as a candidate, **Then** the same refusal applies.
5. **Given** malformed judge output or candidate text containing instructions to the judge, **When** scoring runs, **Then** the judge has no tools, output is validated, and exhausted retries produce an infrastructure failure with no aggregate score.

---

### Edge Cases

- A suite is changed after results exist: existing results MUST retain their suite version and MUST NOT be silently recompared against new ones.
- An evaluation runs against a model that cannot load: it MUST be recorded as an infrastructure failure, never as a zero score.
- A task suite's dataset overlaps the training data: contamination checking MUST be available and its outcome recorded, since an uncaught overlap invalidates the score.
- An adapter's base model is retired: evaluations referencing it MUST remain readable while becoming non-runnable, with a clear reason.
- Evaluation and inference contend for a node: evaluation MUST reserve through the ledger and MUST NOT starve serving.
- A judge produces malformed structured output: it MUST be retried and, on repeated failure, recorded as a failed evaluation rather than a score.
- Two evaluations of one model are submitted concurrently: both MUST have separate records and run when capacity permits; simultaneous execution is not promised.
- A suite takes far longer than expected: it MUST be bounded by a timeout and recorded as timed out.

## Requirements *(mandatory)*

### Functional Requirements

The original FR-001–FR-020 identifiers are preserved; refinements below make their acceptance boundaries explicit.


- **FR-001**: Provide versioned harness, task, and judge suites, with bounded cases, scoring definitions, and immutable content identities. Changed contents require a new version; runnable suites are selected from the supported catalog.
- **FR-002**: An active admin owner MUST be able to submit, list, inspect, rerun, and cancel evaluations against a ready registry model variant or adapter, including private and unverified targets. Concurrent submissions get separate records subject to capacity.
- **FR-003**: Training specifications MUST support declared completed-update checkpoints and a bounded list of exact suite versions. Every declared checkpoint must correspond to a recoverable checkpoint; invalid schedules are rejected before training.
- **FR-004**: Every successfully completed training run declaring suites MUST automatically enqueue base-versus-final-adapter evaluation for every declared suite. Undeclared task/judge evaluation remains off; existing held-out-loss settings are independent.
- **FR-005**: Results MUST retain exact subject and judge model/variant/adapter/artifact identities; suite, cases, scorer, runtime, harness, tokenizer/template and decoding identities; training/checkpoint linkage when present; node, attempt, timestamps, duration and outcome. Unavailable historical fields are labeled, never fabricated.
- **FR-006**: A numerical comparison MUST require matching suite version/content, case selection, scoring, decoding, evaluation runtime, tokenizer/template and judge identity/settings where applicable. Subject identity is the intended difference. Missing or mismatched prerequisites produce explicit non-comparability reasons and no delta.
- **FR-007**: Adapter records MUST show base and adapter results for each declared suite, including pending/failed/non-comparable states. Rubric/task scores show deltas; pairwise results show wins/ties/losses without inventing independent scalar quality scores.
- **FR-008**: Judge suites MUST name an exact ready platform judge resolved through the authenticated gateway. Candidate and judge phases MAY run sequentially so simultaneous model residency is unnecessary; no alternative judge is silently substituted.
- **FR-009**: A judge MUST NOT score outputs from its own registered base model or identical base artifact, including aliases, variants and adapters. Reject such rubric or pairwise submissions before model execution and recheck resolved identities before launch.
- **FR-010**: Complete, measured harness evidence MUST remain the only evaluation input that can change exact-target write verification. Preserve the four categories and existing pass threshold; infrastructure failures do not revoke a prior pass, while a measured harness failure does.
- **FR-011**: Task/judge scores MUST NOT alter verification, publishing, routing preferences, or training success.
- **FR-012**: Evaluation MUST reserve its sandbox and every owned model load through the ledger, preserve serving pins/leases, never oversubscribe memory, and give serving priority. Capacity wait is bounded and visible; unknown process liveness retains reservations.
- **FR-013**: Initial checkpoint evaluation MUST serialize with training: commit and protect the full checkpoint, confirm all trainer processes stopped, evaluate, then resume from that checkpoint only if current authorization/admission and the latest operator intent permit it. A pause taken for evaluation is distinguished from admin/protective pauses.
- **FR-014**: Model load, unavailable judge, invalid collected evidence, and exhausted malformed-judge retries MUST produce an infrastructure failure with no aggregate score. Validly generated wrong answers receive low measured scores instead.
- **FR-015**: Each evaluation MUST have persisted bounded queue and execution deadlines, case/token/output limits and a judge retry limit. Restart/retry cannot reset its overall deadline. Timeouts, cancellation and infrastructure failure are distinct terminal outcomes.
- **FR-016**: Task results MUST record contamination checks against the actual training inputs when available: exact normalized input overlap, algorithm/version, data identities, hit and checked counts, and clean/overlap/unavailable status. “Clean” refers only to this exact check, not proof against semantic or pretraining contamination.
- **FR-017**: Terminal results and their provenance MUST be immutable. Reruns create new linked records; automatic trigger replay and repeated collection commit at most one result for the same execution. Historical results remain readable after target retirement or evidence expiry.
- **FR-018**: The admin console MUST provide evaluation submission, status/history, cancellation, base-versus-adapter comparison, and checkpoint score series with attempt/update identity alongside training loss. Reconnect restores durable state without duplicate points or invented progress.
- **FR-019**: The existing registered-dataset analysis verb MUST remain available and retain 016 behavior; evaluation reuses its data identities rather than creating a second dataset-analysis pipeline.
- **FR-020**: All evaluation administration MUST require authenticated admin access; mutations, refusals and automatic submissions MUST record actor/parent attribution and audit evidence. Evaluation worker credentials grant only the exact inference targets and expiry required, never admin rights.
- **FR-021**: All suite prompting, harness work, deterministic scoring and judging MUST execute in node-owned Studio sandboxes. Evaluation of unverified subjects uses disposable fixed fixtures and no write-capable user tools; generated code, arbitrary commands and user repositories are outside scope.
- **FR-022**: Cancellation and owner revocation MUST stop owned evaluation work, revoke credentials and clean temporary artifacts. Reachable-node cancellation completes within five seconds; unreachable-node status remains unresolved with reservations held until stop proof.
- **FR-023**: Evaluation orchestration MUST survive scheduler/node restart with durable phase, attempt, trigger, checkpoint and result identities. Final evaluation scheduling is atomic with training completion; evaluation errors leave the successful training state intact.
- **FR-024**: Existing 016 recipes, stored normalized/resolved intent hashes, checkpoint manifests and resume behavior MUST remain valid byte-for-byte where previously hashed. New suite declarations use an explicitly versioned training format; old nodes must reject unsupported payloads before launch.
- **FR-025**: Initial suites MUST include bounded coding/patch and instruction cases, plus rubric and pairwise judging with fixed score ranges and aggregation. Candidate text is untrusted data, the judge receives no tools, and pairwise presentation order is deterministic, counterbalanced and recorded.
- **FR-026**: Legacy harness history MUST remain readable. New verification-changing submissions MUST be linked to validated platform execution evidence; an unaffiliated caller-supplied scorecard cannot establish a new pass. Existing CLI harness selectors remain supported through the new execution path.
- **FR-027**: Raw prompts, outputs and training text MUST be excluded from telemetry and audit records. Bounded private evidence has authenticated access, explicit retention and cleanup; result summaries/digests remain after evidence expires, and expired evidence cannot be silently reused for judging.
- **FR-028**: Evaluation MUST emit traces, bounded metrics and structured logs, with a dashboard panel and an alert for stuck or failed work. Durable history, security audit and actionable alerts remain available without optional historical diagnostics.
- **FR-029**: Automated contract, unit, recovery and tiny-model integration tests MUST cover all new boundaries and suite types; documented real-Studio acceptance MUST prove automatic final comparison, checkpoint resume, cancellation and chat coexistence before release. CI never targets the real Studios.
- **FR-030**: Operators MUST have documented enablement, status, kill, evidence-retention and rollback procedures. Disabling new evaluations stops new admission while recovery/cleanup continues; rollback drains evaluation work and preserves historical results and 016 compatibility.

### Key Entities

- **Suite**: Named immutable definition and supported case/scoring version, bounds, mode, rubric and exact judge binding where applicable.
- **Evaluation Run**: One durable execution request with subjects, frozen configuration, owner, deadlines, node/run attempts, reservation and terminal outcome.
- **Evaluation Result**: Immutable measured case/aggregate scores or failure, evidence identities, contamination outcome and complete provenance.
- **Comparison Group**: Explicit baseline/candidate runs for one training boundary or manual comparison; references results and records comparability reasons.
- **Training Evaluation Trigger**: One declared checkpoint/final obligation; protects its checkpoint, records pause ownership and reconciles execution exactly once.
- **Private Evidence**: Bounded candidate/judge outputs retained for inspection and subsequent phases until expiry; summaries and digests outlive bytes.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: An adapter from a declared final schedule shows automatic base-versus-adapter results for at least one task and one rubric judge suite, without manual evaluation submission.
- **SC-002**: All three suite types complete on demand against eligible base and adapter targets, and CLI and console expose the same persisted records.
- **SC-003**: Every self-judge attempt in the alias/variant/adapter test matrix is refused before inference.
- **SC-004**: Every mismatched comparison in the suite/case/scorer/runtime/decoding/judge matrix has explicit reasons and no numeric delta.
- **SC-005**: All injected model-load, unavailable-judge and exhausted-output-validation failures have an infrastructure outcome and no aggregate score; valid incorrect answers remain measurable low scores.
- **SC-006**: With evaluation active, loaded single-node chat with prompts up to 4,000 tokens meets p95 first-token latency ≤1.5 seconds and gateway overhead ≤20 ms p95 excluding model time, with zero swap growth or failed chat requests in the acceptance sample (at least 100 baseline and 100 mixed requests per target).
- **SC-007**: Every completed judge result includes its exact judge and the recorded rubric/order/settings identities; no judge has tools or access to undeclared targets.
- **SC-008**: Explicit rerun creates a new result while the previous record's digest stays unchanged; restart/repeated collection of one execution produces no duplicate terminal result.
- **SC-009**: Declared checkpoint evaluations at two different updates complete and resume from their exact full checkpoints; restart and a superseding admin pause/cancel neither duplicate evaluation nor resume against operator intent.
- **SC-010**: Reachable-node cancellation revokes access and stops the sandbox within five seconds; the unreachable-node case visibly retains reservations until observed cleanup.
- **SC-011**: Frozen 016 recipe/resolved/checkpoint fixtures retain their recorded digests, resume successfully, and run without undeclared task/judge evaluation.
- **SC-012**: An admin can inspect failure/contamination/evidence-expiry status in the console, see the evaluation dashboard, trigger the stuck-work alert, and follow documented kill/rollback steps with historical diagnostics disabled.

## Assumptions and Dependencies

- Baseline is merged 016 at `ec8679c1a3ede4c7b4d897ab4c27b0833a517ecc`, including registry, ledger, exact adapter targets, dataset analysis and recovery. Later numbered draft specs are not assumed shipped.
- Harness categories/thresholds from 010 are reused; their CLI execution location is corrected to Studio-owned evaluation as part of this feature.
- Task suites are local regression checks intended to complete in minutes on a suitably sized Studio model; the acceptance recipe uses at most 16 cases per suite and a 15-minute bound per suite execution.
- Judge quality depends on an admin choosing a suitable distinct acquired model. Small integration fixtures prove execution and failure handling, not judge quality.
- Model acquisition/license approvals use existing admin workflows. No benchmark downloads, new external services or preference-data feedback are required.
- Historical imported harness scorecards retain their identity and legacy provenance; they are not relabeled as 017 executions.
- A raw evidence retention default is set by the plan; immutable result summaries remain until an explicit future data-governance feature provides deletion semantics.
- The user confirmed opt-in scheduling on 2026-10-07. Remaining bounded implementation choices are documented in the plan; no unresolved product decision blocks planning.
