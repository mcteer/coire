# Feature Specification: SFT Training Jobs (LoRA/QLoRA/DoRA)

**Feature Branch**: `feat/016-sft-training-jobs`
**Roadmap ID**: 014 (Phase 4 — Chat UI, images, training)
**Created**: 2026-08-29 | **Updated**: 2026-10-07
**Status**: Implemented — supported capability matrix accepted; final release gates passed
**Input**: Roadmap 014: declarative training recipes and console form; canonical conversations;
dataset registration, analysis, splits and mixtures; supervised adapter training; checkpoint
recovery; shared memory reservation; loss curves; selectable adapters; single- and two-Studio
placement. This revision prepares the existing feature 016 draft for implementation.

## Overview

Administrators can train a language-model adapter from a reproducible recipe, monitor its loss,
pause or recover the run without discarding completed training, and make the resulting adapter
available for inference. Dataset validation catches errors before expensive accelerator work.
Training preserves existing chat service and cannot bypass model acquisition or verification.

## Clarifications

### Session 2026-10-03

- Q: Should feature 016 train on text and tool-call conversations only, or also on conversations containing images? → A: Text and tools only: text, instruction/completion pairs, and structured tool-call conversations are supported; image-bearing rows are rejected explicitly. Visual-model training is a later feature.
- Q: When training shares a Studio with a resident chat model, should Coire protect chat responsiveness by pausing training, or may training temporarily take priority? → A: Chat takes priority. Admit only measured coexistence combinations; training waits or checkpoint-pauses on a chat-latency or memory-pressure breach. Pinned models remain protected, including during two-Studio training.
- Q: Should feature 016 accept uploaded dataset files only, or also import datasets directly from Hugging Face? → A: Admin-uploaded JSONL only, in supported text, prompt/completion, and conversation formats, with provenance and automatic analysis. Direct Hugging Face dataset import is deferred; training never downloads datasets.

### Session 2026-08-29

- Q: Why share conversation representation and template rendering? → A: Training and serving must present the same conversation consistently; formatting differences must be detectable before training.
- Q: How does training coexist with resident inference models? → A: It reserves through the same memory budget, evicts only eligible idle models, and restores eligible evicted models afterwards. The October clarification adds chat-priority admission.
- Q: What is the resume guarantee? → A: Preserve adapter and optimizer state at configured checkpoints; interruption, node restart, or admin pause continues from the latest complete checkpoint. A checkpoint can also be promoted to an adapter.
- Q: How does a trained adapter become usable? → A: Register a base-and-adapter inference target. It must independently pass harness verification before write-capable use.
- Q: Is a merged mixture file materialised? → A: No. Dataset identities, counts, proportions, strategy and seeds define deterministic sampling without a merged corpus.

## User Scenarios & Testing *(mandatory)*

### User Story 1 — Train and use an adapter (Priority: P1)

An administrator submits a recipe file or equivalent form, watches training, and uses the adapter.

**Why this priority**: This is the feature's primary end-to-end outcome.
**Independent Test**: Train a small registered model from a recipe and equivalent form; select
each resulting adapter for inference and inspect its recorded provenance and loss curve.

**Acceptance Scenarios**:

1. **Given** a valid recipe, **When** submitted, **Then** the job preserves the exact submitted
   recipe and a separately resolved record of all defaults, immutable inputs and runtime identity.
2. **Given** equivalent form values, **When** submitted, **Then** the resolved training settings
   equal those of the recipe apart from a distinct output name for a separate run; the generated
   recipe is available before submission and afterwards.
3. **Given** invalid or unsupported settings, **When** submitted, **Then** field-specific errors
   appear before training is admitted; settings are never silently ignored or clamped.
4. **Given** a running job, **When** progress arrives, **Then** step, loss, learning rate,
   throughput, memory reservation and checkpoint status appear in the console.
5. **Given** successful training, **When** the final artifact is validated and available on both
   Studios, **Then** one admin-only, initially unverified adapter record becomes ready for inference.
6. **Given** a ready adapter, **When** the admin selects its advertised `model@adapter` identifier,
   **Then** generation uses that exact base variant and adapter, never the base alone as fallback.
7. **Given** an admin-published adapter, **When** an entitled user opens the model picker,
   **Then** it is selectable; unpublished or retired targets remain inaccessible to ordinary users.
8. **Given** an adapter whose base is verified, **When** a write-capable task selects the still
   unverified adapter, **Then** it is refused; passing the existing harness evaluation for the exact
   adapter enables it without changing the base's scorecard.

### User Story 2 — Recover, pause and stop training (Priority: P1)

A long-running job preserves completed progress through interruptions and remains controllable.

**Why this priority**: Losing optimizer progress or running duplicate trainers invalidates recovery.
**Independent Test**: Compare uninterrupted training with a checkpointed run interrupted by a
node restart; exercise pause, resume, cancel and promotion, including duplicate commands.

**Acceptance Scenarios**:

1. **Given** a job with a committed checkpoint, **When** a Studio restarts, **Then** it continues
   from the newest complete checkpoint with optimizer, random and data-sampling state restored.
2. **Given** only a scheduler or node-agent process restart, **When** its trainer is still alive,
   **Then** observation resumes without starting another trainer.
3. **Given** an admin pause or a protective training pause, **When** acknowledged, **Then** the
   job exposes its resumable checkpoint and releases compute only after its trainers have stopped.
4. **Given** a paused job, **When** resumed, **Then** the same immutable inputs and compatible
   runtime are required; incompatible or missing state produces an actionable refusal.
5. **Given** a cancel request racing completion, **When** one outcome wins, **Then** exactly one
   terminal state exists and a cancelled job cannot automatically publish a final adapter.
6. **Given** a complete retained checkpoint, **When** an admin promotes it, **Then** a new adapter
   receives its provenance and independent verification state without altering the checkpoint.
7. **Given** a job interrupted before its first checkpoint, **When** recovering, **Then** it
   explicitly reports restarting at step zero; it never claims to have resumed saved progress.
8. **Given** a partition or corrupt newest checkpoint, **When** recovery runs, **Then** the job
   remains visibly recovering until old processes are fenced and a valid checkpoint is selected;
   it never releases uncertain reservations or starts duplicate work.

### User Story 3 — Understand and reproduce datasets (Priority: P2)

An administrator uploads a corpus, corrects errors, reviews analysis, and declares deterministic
splits or mixtures before training.

**Why this priority**: Bad data wastes accelerator time and makes results difficult to reproduce.
**Independent Test**: Upload each supported format and a malformed corpus; inspect row diagnostics,
analysis and repeatable split/mixture membership without starting a training job.

**Acceptance Scenarios**:

1. **Given** uploaded JSONL, **When** registered, **Then** every row is checked; any invalid row
   prevents readiness and produces a bounded row-number/field diagnostic without echoing secrets.
2. **Given** valid data and a selected registry tokenizer/template identity, **When** analysis
   completes, **Then** counts, token-length distribution, role balance and exact duplicate groups
   are visible, with the input digest and analysis identity.
3. **Given** a split seed, **When** splits are generated again, **Then** membership is identical;
   exact duplicate content never crosses training and validation partitions.
4. **Given** a mixture, **When** sampled with the same identities, counts, proportions, strategy
   and seed, **Then** sample order is reproducible without creating a merged corpus file.
5. **Given** image-bearing data, malformed tool-call relationships or an incompatible template,
   **When** validated, **Then** the offending rows are refused before accelerator training begins.
6. **Given** a dataset referenced by queued, running or paused work, **When** deletion is requested,
   **Then** it is refused with the dependency; completed-job provenance survives later deletion.

### User Story 4 — Train within cluster capacity (Priority: P2)

An administrator chooses single-Studio or two-Studio data-parallel training while Coire protects
chat, pinned models, memory and recoverability.

**Why this priority**: Training shares scarce GPU memory and execution time with serving.
**Independent Test**: Exercise eviction, impossible admission, temporary contention, two-rank
failure, and a measured same-Studio chat/training workload including the pinned operations model.

**Acceptance Scenarios**:

1. **Given** insufficient free capacity but enough after eligible idle eviction, **When** admitted,
   **Then** training reserves its full worst-case requirement and evicts only eligible idle models.
2. **Given** a job that cannot fit even after eligible eviction, **When** assessed, **Then** it is
   refused with the per-node shortfall; temporary contention instead queues with a reason and deadline.
3. **Given** an unmeasured or stale resident-chat combination, **When** training seeks admission,
   **Then** it waits or selects another eligible Studio rather than competing speculatively.
4. **Given** concurrent supported chat and training, **When** a latency, thermal or memory guard
   trips, **Then** training checkpoint-pauses or stops within the protective deadline, chat retains
   priority, and an operator sees the reason and latest recoverable step.
5. **Given** two-Studio placement, **When** either rank or the data link fails, **Then** both ranks
   stop as one attempt and recover only from a common complete checkpoint after capacity is reacquired.
6. **Given** completion, pause, cancellation or failure, **When** termination is confirmed,
   **Then** reservations release and still-eligible evicted models are offered reload; user changes
   to pinning, retirement or placement are not undone.

### Edge Cases

- Partial, oversized, compressed or non-UTF-8 uploads fail with bounded diagnostics; no archive,
  arbitrary path, executable loader, remote template code or remote download is accepted.
- Oversized token sequences and rows with no supervised tokens fail preflight; no silent truncation.
- Nonfinite loss fails cleanly with prior metrics/checkpoints retained; a finite rising loss is
  visible but does not alone imply infrastructure failure.
- Checkpoint disk exhaustion pauses/fails visibly without deleting a promoted artifact or last
  complete recovery point. Incomplete writes never become resumable checkpoints.
- Duplicate submissions are idempotent; changed content with the same key conflicts. Duplicate
  output names conflict; no existing adapter bytes or publication state are overwritten.
- An unavailable peer prevents adapter readiness and durable checkpoint acknowledgement, with
  bounded waiting; existing complete checkpoints remain recoverable.
- A retired base, revoked admin or changed entitlement prevents new execution/publication and
  stops unauthorized work; historical records remain readable to authorized administrators.
- Browser disconnection does not cancel training. Reconnecting replays progress or supplies a
  current snapshot when old events have expired.
- Single- and two-rank training have different reproducibility identities; changing world size
  or engine/runtime versions is a new run, not an exact resume.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: One validated, versioned training specification MUST cover model, data, objective,
  parameterization, optimizer, evaluation, output, placement and seeds.
- **FR-002**: YAML and console form submission MUST preserve the original recipe separately from
  resolved settings; equivalent inputs MUST produce equivalent resolved settings.
- **FR-003**: Dataset loaders MUST normalize supported rows into the existing canonical conversation
  representation, including structured tool calls and bounded provenance metadata.
- **FR-004**: Training and inference MUST use consistent template/tokenizer identities and rendering
  rules; equivalent completed turns MUST format identically, with explicit training-loss masks and
  inference-only generation suffix differences.
- **FR-005**: Datasets MUST have immutable revisions, declared formats, digest, provenance, row count
  and explicit deterministic training/validation splits; exact duplicates MUST stay in one split.
- **FR-006**: Invalid rows MUST prevent dataset readiness, with bounded row/field diagnostics;
  unsupported template rendering and zero-target/overlength samples MUST prevent training admission.
- **FR-007**: Analysis MUST run automatically on upload against a selected registered model's
  tokenizer/template and report token lengths, role balance and exact duplicate groups. Admins
  MUST be able to rerun analysis using `coire data analyze` without mutating the source revision.
- **FR-008**: Mixtures MUST declare immutable sources, sample counts, proportions, strategy and
  seeds; deterministic sampling MUST NOT materialize a merged corpus or leak validation rows.
- **FR-009**: This feature MUST execute only supervised fine-tuning (`sft`); unsupported objectives
  MUST be rejected rather than accepted without execution.
- **FR-010**: LoRA, QLoRA on an already quantized base, and DoRA MUST each have a tested supported
  configuration. Unsupported base/parameterization combinations MUST fail preflight.
- **FR-011**: Training MUST reserve all weights, optimizer state, activations, buffers and fixed
  node overhead through the shared memory budget; admission MUST preserve no-swap headroom.
- **FR-012**: Impossible-fit jobs MUST be refused with a reason; temporarily blocked jobs MUST
  have a visible queue reason, bounded waiting and cancellation.
- **FR-013**: On confirmed trainer termination, reservations MUST release exactly once and eligible
  evicted models MUST be offered reload without undoing subsequent administrator changes.
- **FR-014**: At configured completed-update intervals, checkpoints MUST capture adapter, optimizer,
  schedule position, random state, sampler cursor and immutable input/runtime identities atomically.
- **FR-015**: Interrupted jobs MUST recover from the latest valid complete checkpoint, never merely
  reload adapter weights with a fresh optimizer. Re-adopt live owned trainers rather than duplicate them.
- **FR-016**: Retention MUST be bounded by count and bytes, preserving the latest complete recovery
  point and promoted artifacts; checkpoints MUST have independently verified copies on both Studios
  before being advertised as durable. A node reboot must not require manual state reconstruction.
- **FR-017**: Every complete retained checkpoint MUST be promotable to a distinct adapter through
  the same validation, replication, audit and verification rules as final output.
- **FR-018**: Training/validation losses and progress MUST persist independently of optional
  diagnostics history and remain displayable after restart or completion.
- **FR-019**: Successful training MUST produce one immutable adapter with exact base variant,
  datasets, settings, runtime, metrics and checkpoint lineage; readiness requires verified copies
  on both Studios and a successful inference smoke test.
- **FR-020**: Registry-issued `model@adapter` targets MUST support ordinary inference and independent
  harness evaluation. Write-capable routing MUST require verification of the exact pair, never inherit
  it from the base. Existing base-only identifiers MUST remain compatible.
- **FR-021**: Placement MUST support one Studio or data-parallel training across both declared
  Studios, with atomic two-node admission and a healthy measured data-fabric link.
- **FR-022**: Loss of either rank MUST fail the whole attempt; recovery MUST fence old ranks and
  use a common complete checkpoint with unchanged world size before resuming.
- **FR-023**: Versioned LoRA, QLoRA and DoRA seed recipes MUST be available as console templates;
  selecting registry models and datasets MUST replace template choices before submission.
- **FR-024**: Dataset, training, checkpoint and adapter management MUST require current admin
  authority. Mutations and denied privileged actions MUST be audited without dataset contents,
  credentials or arbitrary engine output; mandatory audit failure MUST refuse the mutation.
- **FR-025**: Training MUST support text, prompt/completion pairs and structured text/tool
  conversations, and explicitly refuse images and visual-model training.
- **FR-026**: Chat MUST retain priority. Same-Studio concurrency requires current measured evidence;
  stale/unmeasured combinations wait, and latency/thermal/memory breaches checkpoint-pause training
  with a bounded forced-stop fallback. Pinned models and active requests MUST remain protected.
- **FR-027**: Dataset ingestion MUST accept admin-uploaded JSONL only; training consumes registered
  immutable uploads and MUST NOT download datasets or run caller-provided code.
- **FR-028**: Every start, pause, resume, cancel, promotion and final publication MUST be idempotent
  and concurrency-safe. A stale attempt MUST NOT publish progress, checkpoints or adapters.
- **FR-029**: Admins MUST be able to pause, resume and cancel from console and CLI. Unknown process
  liveness MUST remain visible and keep reservations until termination or fencing is established.
- **FR-030**: Resumable inputs and runtime identities MUST be pinned. Referenced active/paused
  inputs cannot be deleted; purged terminal inputs leave readable provenance and an explicit
  non-reproducible status. New checkpoint contents never silently replace a registered identity.
- **FR-031**: Only admin-acquired ready local language-model variants may train. Execution and
  tokenization stay on Studios; caller paths, external provider targets and automatic model pulls
  MUST be refused. Core MUST NOT load models or execute user harnesses.
- **FR-032**: Adapters MUST default to admin-only and unverified. Explicit publication MUST obey
  base publication, readiness and entitlements; retirement/unpublication MUST stop new unauthorized
  selection without substituting another model. Adapter artifacts MUST NOT be public downloads.
- **FR-033**: The training evaluation block MUST execute scheduled held-out loss evaluation.
  Task/judge suites and automatic before/after comparisons belong to feature 017 and MUST be refused
  when requested here; the existing harness verification path remains available for adapters.
- **FR-034**: The console MUST expose accurate state/reasons, recipes, datasets, immutable settings,
  losses, checkpoints and adapter status with keyboard-accessible controls, error/empty/loading
  states and reconnectable progress; unavailable evaluations MUST be labeled, not fabricated.
- **FR-035**: Upload, recipe, row, queue, execution, event, log and storage limits MUST be enforced
  before unbounded work, surfaced to administrators, and documented with cleanup/rollback procedures.
- **FR-036**: Training MUST emit content-free traces, metrics and structured logs and include a
  dashboard plus baseline alerts for stalled/recovering jobs and chat/memory protection failures.
  Turning off diagnostic history MUST NOT hide job progress, audit or alert delivery.

### Key Entities

- **Training specification**: Original recipe and normalized/resolved settings with separate
  identities; base variant, data/mixture, objective, parameterization, optimizer, loss evaluation,
  output, placement and seeds.
- **Dataset revision / analysis**: Immutable source and canonical row identities, provenance,
  deterministic split membership; tokenizer/template-specific immutable analysis results.
- **Training job / attempt**: Durable user intent and successive fenced executions; state,
  participants, reservations, progress, recovery reason and terminal result.
- **Checkpoint**: A completed training update with full restart state, input/runtime identity,
  per-rank state where necessary, manifests and independently verified copies.
- **Adapter / inference target**: Immutable artifact and base pair; lineage, copy readiness,
  visibility, inherited restrictions and independent verification record.
- **Conversation**: Existing ordered messages extended compatibly with tool-call relationships
  and bounded metadata; images remain valid for chat but invalid for this training feature.
- **Coexistence evidence**: Measured supported resident-chat/training bounds tied to exact node,
  runtime, model and workload configuration; invalidated by changes or guard breaches.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A recipe and equivalent form each complete training with equal resolved training
  settings apart from output identity;
  each of LoRA, QLoRA and DoRA produces an adapter that successfully answers an inference request.
- **SC-002**: All three post-checkpoint node-restart trials resume at the last committed update
  with restored optimizer and sample position; recovered same-environment loss/weights match an
  uninterrupted control within a declared numeric tolerance. No duplicate trainer survives.
- **SC-003**: A trained adapter is selectable by its advertised pair identifier; base-only
  requests remain unchanged and unpublished/retired pairs never leak into ordinary-user selection.
- **SC-004**: Every accepted job retains a readable progress/result history; healthy connected
  observers see new progress within 2 seconds of its recording and reconnect without lost state.
- **SC-005**: Every supported conversation fixture has identical completed-turn formatting for
  training and serving; intended loss-mask and generation-suffix differences are explicit.
- **SC-006**: Across impossible-fit, concurrent-admission and 15-minute mixed-workload trials,
  no admitted job causes swap growth. Supported resident chat retains loaded-model first-token
  latency at or below 1.5 seconds p95 for prompts up to 4,000 tokens while training advances.
- **SC-007**: Every malformed-row fixture is rejected before training, with its row and field;
  repeated split and mixture trials produce identical membership/order and no exact-duplicate
  leakage between training and validation.
- **SC-008**: An unverified adapter is refused in every write-capable request despite a verified
  base; an independent passing harness run enables that exact pair and no other adapter.
- **SC-009**: Healthy-node cancellation stops all owned trainer processes within 5 seconds;
  pause reaches a durable checkpoint within 60 seconds or reports a forced stop and last saved
  recovery point. Partition trials hold uncertain reservations and prevent late publication.
- **SC-010**: A two-Studio training run completes and serves its adapter; a rank-loss trial
  stops both ranks and resumes from their common checkpoint without partial publication.
- **SC-011**: Permission, duplicate-request, cancellation/publication and corrupt-checkpoint
  test matrices produce zero unauthorized mutations, duplicate adapters or incomplete ready artifacts.
- **SC-012**: Operators can observe, cancel and recover a job with diagnostics history disabled;
  injected stalled-job and memory/chat-protection failures produce actionable baseline alerts.

## Assumptions

- Existing registry/acquisition, ledger, durable orchestration, authenticated admin API, canonical
  chat representation, console shell and harness evaluation are foundations, not proof that
  training, tool-aware canonical rendering or adapter routing already exist.
- Supported data is uploaded UTF-8 JSONL; text/tokenizer work is bounded and analyzed on Studios.
  Exact duplicates are in scope; near-duplicate similarity and evaluation-set contamination are
  later evaluation work. No chat-history extraction or feedback consent changes are included.
- Training holds one trainer slot per participating Studio; full base weights and optimizer state
  must fit on every data-parallel participant. Data parallelism does not shard a too-large base.
- Checkpoints and adapters live on Studios with mirrored verified copies and control-plane
  metadata; core may store private uploaded datasets but never model weights. Simultaneous loss
  of both Studio disks is outside the resume guarantee; incompatible environments refuse resume.
- Checkpoints are advertised as durable only after both copies verify. An unavailable peer pauses
  checkpoint advancement after bounded transfer waiting; it does not discard the last durable point.
- A protective pause may resume automatically only after current admission checks pass with
  cooldown; an administrator pause requires explicit resume. Cancellation is terminal.
- Full fine-tuning, preference objectives, visual training, direct Hub dataset acquisition, adapter
  fusion/export, sharded adapter inference and task/judge evaluation are outside this feature.
- Single-node adapter inference uses a dedicated base-plus-adapter instance. Existing sharded
  base inference remains compatible; publishing an adapter never changes an existing base instance.
- Default upload and execution limits, numeric recovery tolerance and measured workload bounds are
  fixed in the implementation plan and acceptance procedure before implementation begins.
- Per Principle VII, a tiny-model single-Mac integration gate and real-Studio training/recovery/
  two-rank/coexistence evidence are required before the feature is complete.

## Design reference

`docs/design/DESIGN.md` §6 and `docs/design/mockups/training.html` govern the Training surface:
runs rail, spec cards, progress/loss chart, checkpoint controls, stored recipe, datasets and
adapters. Feature 008 supplies shell/tokens. Actual persisted states govern status labels;
the feature 017 comparison area explicitly says unavailable until that feature is implemented.
