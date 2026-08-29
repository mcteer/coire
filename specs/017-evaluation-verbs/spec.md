# Feature Specification: Evaluation Verbs

**Feature Branch**: `017-evaluation-verbs`

**Roadmap ID**: 014a (Phase 4 — Chat UI, images, training)

**Created**: 2026-08-29

**Status**: Draft

**Input**: User description: "`coire eval` with harness, task, and judge suites (judge = platform model via the gateway); `TrainingSpec.eval` checkpoint scheduling; before/after scores on adapter rows; console comparison view."

## Overview

Feature 010 introduced one evaluation suite, for harness capability. This feature makes evaluation a first-class verb with three suite types — harness capability, task benchmarks, and model-judged scoring — schedulable at training checkpoints and surfaced as before-and-after comparisons on adapter records. Its purpose is to make publishing a trained adapter a decision supported by numbers rather than an act of hope.

## Clarifications

### Session 2026-08-29

- Q: What are the three suite types for? → A: Harness suites measure whether a model can be driven — tool calling, structured output, edit application, long context. Task suites measure capability on coding and instruction benchmarks small enough to run in minutes on one Studio. Judge suites use a stronger platform model to score outputs pairwise or against a rubric, for qualities the first two cannot capture.
- Q: Which model judges, and how is bias handled? → A: A platform model reached through the gateway like any other client, named explicitly in the suite definition and recorded on every result. A judge model must never judge its own outputs in a pairwise comparison; that pairing is refused rather than silently scored.
- Q: When do evaluations run automatically? → A: At checkpoints declared in a training specification's evaluation block, and at the end of every training run. The end-of-run evaluation is what populates the before-and-after comparison the roadmap's acceptance bar requires.
- Q: Is a score ever a gate? → A: Only the harness suite gates, and only for write-capable tasks, exactly as feature 010 established. Task and judge scores inform an admin's publishing decision but never block routing automatically — a benchmark number is not a safety property.
- Q: How are results compared meaningfully? → A: Every result records the model variant, adapter, suite version, engine and harness versions, and run time. A comparison is only offered between results sharing a suite version, so a suite change cannot masquerade as a quality change.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - A trained adapter shows what it changed (Priority: P1)

An adapter record displays base-versus-adapter scores produced automatically at the end of its training run.

**Why this priority**: This is the roadmap's named acceptance bar and the reason the feature exists.

**Independent Test**: Train an adapter and confirm its record shows before-and-after scores for at least one task suite and one judge suite, produced without manual action.

**Acceptance Scenarios**:

1. **Given** a completed training run with an evaluation block, **When** it finishes, **Then** evaluations run automatically against base and adapter.
2. **Given** those results, **When** the adapter is viewed, **Then** base and adapter scores are shown side by side for each suite.
3. **Given** results from different suite versions, **When** comparison is attempted, **Then** it is refused or clearly marked as non-comparable.
4. **Given** an evaluation that fails to run, **When** it fails, **Then** it is recorded as an infrastructure failure distinct from a poor score.

---

### User Story 2 - An admin evaluates any model on demand (Priority: P1)

An admin runs any suite against any model variant or adapter without needing a training run.

**Why this priority**: New models enter the roster continuously and must be assessed before publishing; that path cannot depend on having trained something.

**Independent Test**: Run each of the three suite types against a published model and read the results.

**Acceptance Scenarios**:

1. **Given** a model variant, **When** an admin runs a harness suite, **Then** a scorecard is produced and the verification state is updated accordingly.
2. **Given** a model variant, **When** an admin runs a task suite, **Then** per-task scores are recorded with the suite version.
3. **Given** two candidates, **When** an admin runs a judge suite, **Then** pairwise or rubric scores are recorded naming the judge model.
4. **Given** any suite run, **When** it executes, **Then** it reserves memory through the ledger like any other work.

---

### User Story 3 - Evaluations run at checkpoints during training (Priority: P2)

A long training run is evaluated at declared checkpoints so a regression is visible before the run ends.

**Why this priority**: Valuable for long runs, but the end-of-run comparison delivers the feature's core value on its own.

**Independent Test**: Declare checkpoint evaluations in a specification and confirm results appear as the run progresses.

**Acceptance Scenarios**:

1. **Given** a specification declaring checkpoint evaluations, **When** a checkpoint is reached, **Then** the declared suites run against that checkpoint.
2. **Given** checkpoint results, **When** the job is viewed, **Then** scores are shown against training step alongside the loss curve.
3. **Given** checkpoint evaluation that would exceed the job's memory reservation, **When** it is scheduled, **Then** it is serialised with training rather than run concurrently.

---

### User Story 4 - A judge never scores itself (Priority: P2)

A pairwise comparison involving the judge model's own output is refused rather than scored.

**Why this priority**: Self-preference is a well-known and severe bias in model-judged evaluation; allowing it would quietly invalidate every judge result.

**Independent Test**: Configure a pairwise comparison where a candidate is the judge model and confirm refusal.

**Acceptance Scenarios**:

1. **Given** a pairwise suite where one candidate is the judge model, **When** it is submitted, **Then** it is refused with that reason.
2. **Given** any judge result, **When** it is recorded, **Then** the judge model and its variant are recorded with it.
3. **Given** a judge model that is unavailable, **When** a judge suite is run, **Then** it fails as an infrastructure error rather than producing scores.

---

### Edge Cases

- A suite is changed after results exist: existing results MUST retain their suite version and MUST NOT be silently recompared against new ones.
- An evaluation runs against a model that cannot load: it MUST be recorded as an infrastructure failure, never as a zero score.
- A task suite's dataset overlaps the training data: contamination checking MUST be available and its outcome recorded, since an uncaught overlap invalidates the score.
- An adapter's base model is retired: evaluations referencing it MUST remain readable while becoming non-runnable, with a clear reason.
- Evaluation and inference contend for a node: evaluation MUST reserve through the ledger and MUST NOT starve serving.
- A judge produces malformed structured output: it MUST be retried and, on repeated failure, recorded as a failed evaluation rather than a score.
- Two evaluations of one model run concurrently: both MUST be permitted and recorded separately, since results are timestamped records rather than a single mutable field.
- A suite takes far longer than expected: it MUST be bounded by a timeout and recorded as timed out.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The system MUST provide three suite types: harness capability, task benchmark, and model-judged.
- **FR-002**: Suites MUST be runnable on demand against any model variant or adapter by an admin.
- **FR-003**: Suites MUST be schedulable at checkpoints declared in a training specification's evaluation block.
- **FR-004**: An evaluation MUST run automatically at the end of every training run whose specification declares one.
- **FR-005**: Every result MUST record model variant, adapter if any, suite identity and version, engine and harness versions, and run time.
- **FR-006**: Comparison MUST be offered only between results sharing a suite version, and MUST otherwise be refused or clearly marked non-comparable.
- **FR-007**: Adapter records MUST display base-versus-adapter scores for each suite that produced them.
- **FR-008**: Judge suites MUST use a platform model reached through the gateway, named in the suite definition and recorded on every result.
- **FR-009**: A pairwise comparison in which a candidate is the judge model MUST be refused.
- **FR-010**: The harness suite MUST remain the only suite that gates routing, and only for write-capable tasks.
- **FR-011**: Task and judge scores MUST NOT automatically gate routing.
- **FR-012**: Evaluations MUST reserve memory through the ledger and MUST NOT starve serving.
- **FR-013**: Checkpoint evaluations MUST be serialised with training rather than run concurrently when they would exceed the job's reservation.
- **FR-014**: An evaluation that cannot run MUST be recorded as an infrastructure failure, distinct from a low score.
- **FR-015**: Every suite run MUST be bounded by a timeout and recorded as timed out when exceeded.
- **FR-016**: Contamination checking against training data MUST be available for task suites and its outcome recorded.
- **FR-017**: Results MUST be immutable records; re-running MUST create a new record rather than overwriting.
- **FR-018**: The console MUST provide a comparison view across results for a model or adapter.
- **FR-019**: Dataset analysis MUST be available as a verb over registered datasets, consistent with feature 016.
- **FR-020**: Evaluation routes MUST require the admin role and MUST be audited.

### Key Entities

- **Suite**: A named, versioned evaluation definition. Type, version, tasks or rubric, judge model where applicable, timeout, contamination-check configuration.
- **Evaluation Run**: One execution. Suite and version, subject model variant and adapter, node, reservation, state, timings, outcome.
- **Evaluation Result**: An immutable score record. Run, per-task or per-rubric scores, aggregate, judge model where applicable, engine and harness versions, recorded-at.
- **Comparison**: A base-versus-adapter view. Subject pair, suite version, per-suite deltas, comparability verdict.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: An adapter record shows base-versus-adapter scores for at least one task suite and one judge suite, produced automatically at the end of a training run.
- **SC-002**: All three suite types run on demand against a published model and produce recorded results.
- **SC-003**: A pairwise comparison naming the judge as a candidate is refused in 100% of attempts.
- **SC-004**: Results from differing suite versions are never silently compared.
- **SC-005**: An evaluation that cannot load its model is recorded as an infrastructure failure, never as a score.
- **SC-006**: Evaluation never starves serving; chat latency stays within target during an evaluation run.
- **SC-007**: Every judge result names the judge model and variant.
- **SC-008**: Re-running a suite creates a new record and leaves prior records intact.

## Assumptions

- Features 001–016 have shipped: the registry with verification, the ledger, the gateway, the harness and its capability suite, the console, and training jobs with their evaluation block.
- The harness capability suite from feature 010 is absorbed here as one of the three suite types rather than reimplemented.
- Task suites are small enough to run in minutes on one Studio; they are not research-scale benchmarks.
- Judge models are ordinary platform models reached through the gateway, subject to the same entitlement and routing rules as any caller.
- Preference-data quality feedback loops are feature 018 and consume this feature's results rather than extending it.
- Dataset analysis introduced in feature 016 is exposed here as a verb alongside evaluation for consistency.
- Per Principle VII, integration tests exercise each suite type against a tiny model, with meaningful scoring verified manually on the cluster.
