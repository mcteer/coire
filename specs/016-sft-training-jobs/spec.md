# Feature Specification: SFT Training Jobs (LoRA/QLoRA/DoRA)

**Feature Branch**: `016-sft-training-jobs`

**Roadmap ID**: 014 (Phase 4 — Chat UI, images, training)

**Created**: 2026-08-29

**Status**: Draft

**Input**: User description: "`TrainingSpec` schema accepted as YAML and as a console form and stored verbatim on the job; `Conversation` canonical type shared with the gateway's chat-template rendering; dataset registry with loaders, schema check, split, mixtures, `coire data analyze` on upload; `objective=sft` behind an objective registry; checkpoints + resume through DBOS; reservation through the scheduler; loss metrics; adapter row selectable as `model@adapter`; `placement: single | data_parallel`; seed recipes."

## Overview

This feature makes the platform able to improve its own models rather than only serve them. A training job is one declarative specification, validated on submission and stored verbatim so a run can be reproduced. Datasets become registry objects with schema checks and analysis. Training reserves memory through the same ledger as everything else, checkpoints so it survives interruption, and produces an adapter that is immediately usable for inference. Crucially, the conversation type and chat-template rendering are shared with the gateway, so a model never sees a different format in training than in serving.

## Clarifications

### Session 2026-08-29

- Q: Why share the conversation type and template rendering with the gateway? → A: Because a mismatch between training-time and inference-time formatting is a silent quality bug that is extremely hard to diagnose. Sharing one canonical type and one rendering function makes the mismatch impossible by construction rather than by discipline.
- Q: How does training coexist with resident inference models? → A: It reserves memory through the same ledger. The scheduler evicts idle inference models to make room, runs the job, and reloads them afterwards. Training is not a privileged class; it is a reservation holder like any other.
- Q: What is the resume guarantee? → A: Checkpoints of adapter and optimiser state are written every N steps to the node's job directory, and a job interrupted by eviction, a node restart, or an admin pause resumes from the last checkpoint through its durable workflow rather than starting over. Any checkpoint can be promoted to an adapter.
- Q: How does a trained adapter become usable? → A: As an adapter registry row that can be selected for inference as a model-and-adapter pair. It must still pass the harness evaluation gate before write-capable tasks will use it — training a coder adapter does not by itself make it trusted to apply changes.
- Q: Is a merged dataset file materialised for mixtures? → A: No. Mixtures are declared as a list of datasets with sample counts and proportions plus a strategy, and are sampled at load time with explicit seeds. Materialising a merged file would make provenance and reproducibility worse, not better.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - An admin trains an adapter from a recipe (Priority: P1)

An admin submits a training specification as a file or through a form, watches it train with a loss curve, and ends with an adapter that can be used for inference.

**Why this priority**: This is the roadmap's acceptance bar and the feature's entire purpose.

**Independent Test**: Submit a small training run from a recipe and use the resulting adapter for inference.

**Acceptance Scenarios**:

1. **Given** a valid specification submitted as a file, **When** it is accepted, **Then** a job is created with the specification stored verbatim.
2. **Given** the same specification submitted through the console form, **When** it is accepted, **Then** it produces an equivalent job.
3. **Given** a running job, **When** it trains, **Then** loss metrics are recorded and visible as a curve.
4. **Given** a completed job, **When** it finishes, **Then** an adapter record is created referencing its base model, objective, dataset, and metrics.
5. **Given** that adapter, **When** it is selected for inference, **Then** requests are served by the base model with the adapter applied.
6. **Given** an invalid specification, **When** it is submitted, **Then** it is refused at validation naming the offending fields.

---

### User Story 2 - A training run survives interruption (Priority: P1)

A job interrupted by a node restart resumes from its last checkpoint rather than starting over.

**Why this priority**: The roadmap names it, and training runs are long enough that losing one to an unrelated restart is a serious cost.

**Independent Test**: Restart the node mid-run and confirm the job resumes from checkpoint and completes.

**Acceptance Scenarios**:

1. **Given** a running job past its first checkpoint, **When** the node restarts, **Then** the job resumes from that checkpoint and completes.
2. **Given** a job evicted to make room, **When** it resumes, **Then** it continues from its last checkpoint rather than restarting.
3. **Given** a paused job, **When** an admin resumes it, **Then** it continues from checkpoint.
4. **Given** any checkpoint, **When** an admin promotes it, **Then** it becomes an adapter record usable for inference.

---

### User Story 3 - Datasets are validated and understood before training (Priority: P2)

An uploaded dataset is schema-checked, counted, split, and analysed so the admin knows what they are about to train on.

**Why this priority**: Training on a malformed or badly-balanced dataset wastes hours of accelerator time. Catching it at upload is far cheaper than diagnosing it in a loss curve.

**Independent Test**: Upload a dataset and confirm schema validation, counts, split, and analysis are produced.

**Acceptance Scenarios**:

1. **Given** an uploaded dataset, **When** it is registered, **Then** its rows are schema-checked against its declared type and rejected with row-level reasons if invalid.
2. **Given** a valid dataset, **When** registration completes, **Then** row counts, a train and validation split, and provenance are recorded.
3. **Given** a registered dataset, **When** analysis runs, **Then** token-length distribution, role balance, and duplicate detection are reported.
4. **Given** a mixture of datasets, **When** it is declared, **Then** proportions and seeds are explicit and no merged file is materialised.

---

### User Story 4 - Training competes fairly for memory (Priority: P2)

A training job reserves through the ledger, causing idle inference models to be evicted and reloaded afterwards, without ever pushing a node into swap.

**Why this priority**: Training is the largest memory consumer on the platform; unmanaged, it would destabilise serving.

**Independent Test**: Submit a job requiring more memory than is free and confirm eviction, training, and reload.

**Acceptance Scenarios**:

1. **Given** insufficient free memory, **When** a job is admitted, **Then** idle inference models are evicted to make room.
2. **Given** a job that cannot fit even after eviction, **When** admission is attempted, **Then** it is refused with a reason rather than queued indefinitely.
3. **Given** a completed job, **When** it finishes, **Then** its reservation is released and evicted models can reload.
4. **Given** any admitted job, **When** it runs, **Then** the node is never pushed into swap.

---

### Edge Cases

- A dataset's rows reference a chat template the base model cannot render: it MUST be caught at validation rather than at training time.
- Training produces a diverging loss: the job MUST still complete or fail cleanly with metrics retained, since the curve is the diagnostic.
- Disk fills with checkpoints: checkpoint retention MUST be bounded, keeping the most recent and any promoted ones.
- A base model is retired while an adapter references it: the adapter MUST become unusable with a clear reason rather than failing obscurely at inference.
- Two jobs target the same output adapter name: the second MUST be refused or renamed, never silently overwriting.
- A data-parallel job loses one node: it MUST fail as a unit and resume from checkpoint, consistent with sharded inference semantics.
- A specification names a dataset that is later deleted: the stored specification MUST remain readable for reproduction even if it can no longer run.
- An adapter is used for a write-capable task before evaluation: it MUST be refused by the verification gate.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: A training job MUST be defined by a single validated specification covering model, data, objective, parameterisation, optimisation, evaluation, and output.
- **FR-002**: The specification MUST be accepted both as a file and through a console form, and MUST be stored verbatim on the job so a run can be reproduced.
- **FR-003**: The system MUST define a canonical conversation type, and every dataset loader MUST emit it.
- **FR-004**: Chat-template rendering used in training MUST be the same function the gateway uses at inference.
- **FR-005**: Datasets MUST be registry objects with a declared type, schema validation on upload, row counts, an explicit train and validation split, and recorded provenance.
- **FR-006**: Invalid dataset rows MUST be rejected with row-level reasons.
- **FR-007**: Dataset analysis MUST report token-length distribution, role balance, and duplicate detection, and MUST run automatically on upload.
- **FR-008**: Mixtures MUST be declared as datasets with sample counts, proportions, a strategy, and explicit seeds, and MUST NOT materialise a merged file.
- **FR-009**: Objectives MUST be pluggable through a registry, with supervised fine-tuning provided in this feature.
- **FR-010**: Parameterisation MUST support adapter-based training including quantised-base and decomposed variants.
- **FR-011**: Training jobs MUST reserve memory through the same ledger as inference, MUST cause eviction of idle models when necessary, and MUST never push a node into swap.
- **FR-012**: A job that cannot fit after eviction MUST be refused with a reason.
- **FR-013**: Evicted models MUST be reloadable after a job completes and its reservation is released.
- **FR-014**: Checkpoints of adapter and optimiser state MUST be written at a configured interval to the node's job directory.
- **FR-015**: An interrupted job MUST resume from its last checkpoint through a durable workflow rather than restarting.
- **FR-016**: Checkpoint retention MUST be bounded, preserving the most recent and any promoted checkpoints.
- **FR-017**: Any checkpoint MUST be promotable to an adapter record.
- **FR-018**: Loss metrics MUST be recorded during training and displayable as a curve.
- **FR-019**: A completed job MUST produce an adapter record referencing base model, objective, dataset, hyperparameters, and metrics.
- **FR-020**: An adapter MUST be selectable for inference as a model-and-adapter pair, and MUST be subject to the harness verification gate before write-capable use.
- **FR-021**: Placement MUST support single-node and data-parallel across both Studios, with the scheduler choosing nodes and building any required launch configuration.
- **FR-022**: A data-parallel job losing a node MUST fail as a unit and be resumable from checkpoint.
- **FR-023**: Seed recipes MUST be versioned in the repository and available as console templates.
- **FR-024**: Training routes MUST require the admin role and MUST be audited.

### Key Entities

- **Training Spec**: The declarative contract. Base model, dataset or mixture, objective, parameterisation, optimisation settings, evaluation block, output settings, placement, initial adapter.
- **Training Job**: One run. Spec stored verbatim, state, node or nodes, reservation, checkpoints, metrics, timings, failure reason.
- **Dataset**: A registered corpus. Type, schema version, row count, split, provenance, analysis results, created-at.
- **Adapter**: A trained artefact. Base model, objective, dataset, hyperparameters, metrics, checkpoint origin, verification state, published flag.
- **Checkpoint**: A resumable point. Job, step, adapter and optimiser state reference, created-at, promoted flag.
- **Conversation**: The canonical training and inference message structure, including tool calls, images, and a metadata bag.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: An adapter trains successfully from both a recipe file and the console form, producing equivalent jobs.
- **SC-002**: A job interrupted by a node restart resumes from checkpoint and completes, in 100% of trials past the first checkpoint.
- **SC-003**: A trained adapter is usable for inference as a model-and-adapter pair.
- **SC-004**: A loss curve is visible on the jobs dashboard for every training run.
- **SC-005**: Training-time and inference-time chat-template rendering are produced by the same function, verified by test.
- **SC-006**: A training job never pushes a node into swap, across all admitted jobs.
- **SC-007**: Invalid dataset rows are rejected at upload with row-level reasons, never surfacing as training failures.
- **SC-008**: An adapter is refused write-capable tasks until it passes the harness evaluation.

## Assumptions

- Features 001–014 have shipped: the registry, ledger with eviction, durable workflows, credentials and audit, the console, the harness with its verification gate, and the gateway's chat-template rendering.
- Training engines are driven directly on the Studios by the node agent, behind an objective registry so a backend can be replaced without changing a specification.
- Preference objectives are feature 018 and enter through the same objective registry.
- Evaluation scheduling is declared in the specification's evaluation block, but evaluation suites themselves are feature 017; until then the block is stored and honoured only for checkpoint scheduling.
- Fusing an adapter into a standalone model record re-enters the acquisition pipeline at its validation stage, per feature 002.
- Both Studios have 256 GB and 1.8 TB of disk (verified 2026-08-29); checkpoint retention is bounded against that disk.
- Per Principle VII, integration tests train a tiny adapter on a tiny model so CI runs on a single Mac, and real training runs are verified manually on the cluster before merge.

## Design reference

`docs/design/DESIGN.md` §6 "Training" and `docs/design/mockups/training.html` specify this surface:
the 276px runs list with status pills, the 800px run panel with four spec cards, the progress bar
carrying step, loss, ETA and the ledger reservation, the loss chart with checkpoint chips, the
eval-vs-base table, the log tail, and the stored `TrainingSpec` rendered as YAML in three mono
columns. The shell and tokens come from feature 008.

The fixed status vocabulary in §5 — `queued`, `running`, `done`, `failed` — must match the run states
this feature actually persists; if they diverge, the state model wins and the design vocabulary is
amended to follow it.
