# Feature Specification: Model Acquisition Pipeline

**Feature Branch**: `002-acquisition-pipeline`

**Roadmap ID**: 001b (Phase 1 — Single-node inference works end to end)

**Created**: 2026-08-29

**Status**: Draft

**Input**: User description: "DBOS workflow that inspects an HF repo's metadata (architecture support, MLX vs raw vs GGUF, size, fit-by-precision), pulls once, runs `mlx_lm.convert` with chosen bits/group size/mode/mixed recipe as a ledger-reserving job, validates (generation smoke test, perplexity vs reference, tool-call template check), replicates, and records variants on the model row."

## Overview

Feature 001 can acquire a repo that is already MLX-format. This feature makes Coire able to accept *any* plausible Hugging Face repo and decide what must happen to make it servable: inspect metadata before moving bytes, pull once, convert or quantise on-node as a memory-reserving job, validate the result against measurable criteria, replicate, and record the outcome as a named variant on the model row. It is what turns "add a model" from a narrow happy path into a durable, resumable, observable pipeline whose every stage is visible in the console.

## Clarifications

### Session 2026-08-29

- Q: What exactly disqualifies a repo, and how early? → A: Three rejections, all at inspect time before any weights move: the architecture is unsupported by the inference engine; the repo is GGUF-only; or no candidate precision fits any supported placement. Each returns a specific reason and, for GGUF, a pointer to the original source repo to use instead.
- Q: Conversion needs memory — how does it coexist with loaded models before the scheduler exists? → A: Conversion is submitted as a memory-reserving job against the same per-node budget feature 001 enforces. Until feature 004 provides eviction, a conversion that does not fit is queued rather than refused, and the admin is shown what is occupying the node. Feature 004 upgrades this to evict-then-convert without changing the contract.
- Q: What is the pass/fail bar for validation? → A: Three checks, all recorded on the variant. A generation smoke test over a fixed prompt set must produce non-empty, non-degenerate output; perplexity on a small held-out text must be within an admin-configurable tolerance of the reference variant when one exists; and the chat template must render a tool call correctly. Failing perplexity marks the variant `validated: false` with the score recorded but does not delete it — the admin decides.
- Q: Are raw weights kept after conversion? → A: Deleted by default once conversion succeeds and the converted output validates, unless the admin sets keep-raw at add time so future re-quantisation needs no second pull. Kept raw weights count against disk and are shown in the console.
- Q: Can a model have several variants, and which does a user get? → A: Yes — variants like 4-bit, 6-bit, mixed, and bf16 share one base model identity. Only published variants appear to users; the admin marks one the default for the picker. Adding a variant later re-runs convert, validate, and replicate only, from kept raw weights or by dequantising.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Admin adds a raw PyTorch repo and gets a served model (Priority: P1)

An admin names a repo that is not in MLX format. Coire inspects it, pulls it, converts it to a chosen precision on-node, validates the result, replicates it, and marks it `ready` — with each stage visible as it happens.

**Why this priority**: This is the pipeline's reason to exist. Most interesting open-weights repos are not published in MLX format.

**Independent Test**: Add one small raw repo and confirm it ends `ready` on both Studios with a recorded, validated variant.

**Acceptance Scenarios**:

1. **Given** an admin and a raw safetensors repo, **When** the model is added with a target precision, **Then** inspect, pull, convert, validate, and replicate each run in order and the model ends `ready`.
2. **Given** a running pipeline, **When** the admin inspects the job, **Then** the current stage, its progress, and the outcome of every completed stage are visible.
3. **Given** a successful conversion, **When** the pipeline completes, **Then** a variant row records the precision, recipe, resulting byte size, and validation results.
4. **Given** the admin did not request keep-raw, **When** conversion and validation succeed, **Then** the raw weights are deleted and the reclaimed disk is reflected in node status.

---

### User Story 2 - Unservable repos are refused early with a usable reason (Priority: P1)

An admin names a repo Coire cannot serve. It is refused during inspection, before any weights transfer, with a message that says what is wrong and what to do instead.

**Why this priority**: Pulling 300 GB before discovering it cannot be loaded wastes most of an hour of gigabit bandwidth; the whole point of an inspect stage is to fail before that.

**Independent Test**: Add a GGUF-only repo and confirm rejection at inspect with a pointer to the source repo, and that no bytes were transferred.

**Acceptance Scenarios**:

1. **Given** a GGUF-only repo, **When** the admin adds it, **Then** it is rejected at inspect naming GGUF as the reason and suggesting the original source repo, with zero bytes pulled.
2. **Given** a repo whose architecture the engine does not support, **When** the admin adds it, **Then** it is rejected at inspect naming the architecture.
3. **Given** a repo too large to fit any supported placement at any candidate precision, **When** the admin adds it, **Then** it is rejected at inspect showing required versus available memory per placement.
4. **Given** a gated repo without accepted licence terms, **When** the admin adds it, **Then** the failure names gating specifically rather than presenting as a generic download error.

---

### User Story 3 - Admin compares quantisation recipes before publishing (Priority: P2)

An admin produces more than one variant of the same base model and compares their measured cost and quality before deciding which to publish.

**Why this priority**: Choosing a quantisation blind is guesswork; the validation numbers are what make publishing a decision rather than a hope. It is not required for the first model to serve.

**Independent Test**: Produce two variants of one base model at different precisions and confirm the console shows byte size and validation scores side by side.

**Acceptance Scenarios**:

1. **Given** a model with kept raw weights, **When** the admin requests a second variant at a different precision, **Then** only convert, validate, and replicate run — no second external pull occurs.
2. **Given** two validated variants, **When** the admin views the model, **Then** each variant's precision, recipe, byte size, perplexity, and smoke-test outcome are shown together.
3. **Given** several variants, **When** the admin marks one as default and publishes it, **Then** users see only published variants and the default is preselected.

---

### User Story 4 - A pipeline survives interruption (Priority: P2)

A conversion or replication is interrupted by a node restart or a control-plane restart, and the pipeline resumes from the last completed stage instead of starting over or orphaning files.

**Why this priority**: Stages here run for tens of minutes; restarting a 45-minute pull because the scheduler was upgraded is unacceptable, and Principle VII requires recovery to be automatic.

**Independent Test**: Restart the control plane mid-convert and confirm the pipeline resumes and completes.

**Acceptance Scenarios**:

1. **Given** a pipeline mid-pull, **When** the control plane restarts, **Then** the pull resumes from its existing partial state and the model still ends `ready`.
2. **Given** a pipeline mid-convert, **When** the node restarts, **Then** the conversion restarts at the stage boundary and the model still ends `ready`, with no partial output published.
3. **Given** a pipeline that failed at validation, **When** the admin retries it, **Then** only validation onward re-runs; pull and convert results are reused.

---

### Edge Cases

- A repo's unquantised size exceeds a single node's memory budget: local conversion MUST be refused with a pointer to a pre-quantised MLX repo, rather than attempting and thrashing.
- Conversion succeeds but produces output larger than its estimate: the variant MUST record the true size and the discrepancy MUST be visible.
- Validation cannot run because no reference variant exists for perplexity comparison: the smoke test and template check MUST still run, and perplexity MUST be recorded as not-comparable rather than as a pass.
- The chat template renders but produces malformed tool calls: this MUST fail the template check specifically, since a model that cannot express tool calls is unusable to every agent profile.
- Two admins convert the same model to the same precision concurrently: the second MUST attach to the first job rather than run a duplicate conversion.
- Disk fills mid-conversion: the job MUST fail with a disk reason, and partial output MUST be removed rather than left to be mistaken for a variant.
- A repo updates upstream after acquisition: this feature MUST NOT auto-refresh; re-acquiring is an explicit admin action creating a new variant.
- Publishing to Hugging Face MUST NOT be possible from any route; nothing on the platform pushes upstream.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The pipeline MUST run as a durable workflow whose stages are inspect, pull, convert, validate, and replicate, each independently observable and individually retryable.
- **FR-002**: The workflow MUST resume from the last completed stage after a control-plane or node restart, without re-running completed stages.
- **FR-003**: Inspect MUST read repository metadata only, transferring no weights, and MUST determine architecture support, format, total size, gating status, and memory fit at each candidate precision.
- **FR-004**: Inspect MUST reject unsupported architectures, GGUF-only repos, and repos that fit no supported placement, each with a specific reason and, for GGUF, a pointer to a usable source repo.
- **FR-005**: Pull MUST download once to the Studio with the most free disk, with resume support and checksum verification.
- **FR-006**: Convert MUST run on-node with admin-chosen precision, group size, mode, and mixed recipe, and MUST reserve memory against that node's budget for the duration.
- **FR-007**: A conversion that does not currently fit MUST be queued with the occupying models shown, not silently refused.
- **FR-008**: Convert MUST be refused when the repo's unquantised size exceeds the node's memory budget, with guidance to use a pre-quantised repo.
- **FR-009**: Validate MUST run a generation smoke test over a fixed prompt set, compute perplexity on a held-out text, compare against a reference variant when one exists, and verify the chat template renders tool calls.
- **FR-010**: Validation results MUST be recorded on the variant, and a perplexity failure MUST mark the variant unvalidated without deleting it.
- **FR-011**: A failed template check MUST fail validation.
- **FR-012**: Replicate MUST copy to the peer Studio over the LAN, and the variant MUST become `ready` only when both copies verify.
- **FR-013**: A model MUST support multiple named variants sharing one base identity, each with its own precision, recipe, size, and validation results.
- **FR-014**: Adding a variant later MUST re-run only convert, validate, and replicate, from kept raw weights or by dequantising.
- **FR-015**: Raw weights MUST be deleted after successful conversion and validation unless the admin requested keep-raw at add time.
- **FR-016**: Only published variants MUST be visible to users, and the admin MUST be able to designate one variant as default.
- **FR-017**: Every stage transition and admin decision MUST write an audit record.
- **FR-018**: The system MUST expose no route that publishes or pushes any artefact to Hugging Face.
- **FR-019**: Concurrent identical conversion requests MUST attach to the running job rather than duplicating work.
- **FR-020**: All pipeline routes MUST require the admin role.

### Key Entities

- **Acquisition Workflow**: A durable pipeline run. Model, requested precision and recipe, current stage, per-stage status and timings, failure reason, resumption state.
- **Inspection Result**: Pre-transfer findings. Architecture, support verdict, format, total bytes, gating status, per-precision memory estimates, fit-by-placement verdicts.
- **Model Variant**: One converted form of a base model. Name, precision, group size, mode, recipe, byte size, validation results, published flag, default flag, per-node copy status.
- **Validation Result**: Measured quality for a variant. Smoke-test outcome, perplexity, reference variant compared against, tolerance applied, template check outcome, timestamp.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A raw PyTorch repo and an already-MLX repo both reach `ready` on both Studios through the same pipeline.
- **SC-002**: A GGUF-only repo is rejected at inspect with zero bytes transferred, and the message names the source repo to use instead.
- **SC-003**: Every rejection reason is specific enough to act on; no rejection surfaces as a generic error.
- **SC-004**: The console shows per-variant validation results for every variant produced.
- **SC-005**: A control-plane restart mid-pipeline resumes and completes without re-running any completed stage.
- **SC-006**: Adding a second variant from kept raw weights performs zero external pulls.
- **SC-007**: Conversion reserves node memory for its duration and never pushes a node into swap.
- **SC-008**: Raw weights are absent after a successful default-configuration acquisition, and reclaimed disk is reflected in node status.

## Assumptions

- Feature 001 has shipped: the registry, admin-only acquisition, the pull-and-replicate path, the node agent's load and status verbs, and per-node memory budgets exist.
- Durable workflow execution runs on the control plane and drives node work through the node agent's API; workers do not run on the Studios.
- Conversion runs natively on a Studio because it must own Metal; it is a node-agent-managed process like an engine.
- Until feature 004, a conversion that does not fit is queued rather than triggering eviction. Feature 004 upgrades this behaviour without changing this feature's contract.
- Perplexity tolerance is an admin-configurable setting with a documented default, not a hard-coded constant.
- Image-model kinds reuse this pipeline with their own validation step; that arrives with feature 015 and does not change the stage sequence.
- Adapter fusion from feature 016 enters this pipeline at the validate stage as a new variant.
- Integration tests use models of 1 GB or less; conversion of a large repo is verified manually on the real cluster before merge, per Principle VII.
