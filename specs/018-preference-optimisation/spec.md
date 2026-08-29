# Feature Specification: Preference Optimisation and Feedback Capture

**Feature Branch**: `018-preference-optimisation`

**Roadmap ID**: 014b (Phase 4 — Chat UI, images, training)

**Created**: 2026-08-29

**Status**: Draft

**Input**: User description: "`preference` dataset type; `objective=dpo|orpo` jobs with `init_adapter` chaining from an SFT adapter and 2× base memory reservation; chat UI thumbs, regenerate-and-compare, and admin pairwise review queue writing feedback rows; admin export of feedback → preference dataset with filters; per-user feedback opt-out and disclosure."

## Overview

The platform already sees every prompt and response, which means it can collect the comparisons preference optimisation needs without a separate labelling tool. This feature adds the preference dataset type and the objectives that consume it, the feedback capture surfaces in the chat UI and an admin review queue, and the export path that turns collected comparisons into a training dataset. It also carries a real obligation to users: they are told their feedback may be used to improve models here, and they can opt out.

## Clarifications

### Session 2026-08-29

- Q: Why does preference training need roughly twice the memory of the base model? → A: The objective holds a frozen reference copy of the base alongside the policy being trained. The job's estimate is therefore about twice base weights plus activations, and it reserves that through the ledger like any other consumer — which usually means evicting resident inference models first.
- Q: How does a preference run relate to a prior supervised run? → A: Through adapter chaining. A preference job may start from an existing adapter rather than the bare base, which is the standard recipe — supervised fine-tuning first, then preference optimisation on the same adapter. The chain is recorded so an adapter's lineage is inspectable.
- Q: What exactly does the chat UI capture? → A: Thumbs on any message, and a regenerate-and-compare flow where producing a second candidate lets the user pick the better one, making the loser the rejected response. Both write feedback rows carrying user, model, adapter, and conversation identity.
- Q: What are users told? → A: That feedback may be used to improve models on this platform, disclosed in the interface rather than buried. Capture can be disabled per user, and when disabled no feedback rows are written for that user at all — not written-and-filtered.
- Q: Does a preference-trained adapter get special trust? → A: No. It passes through the same harness verification gate as any other adapter before write-capable tasks will use it. Training on preferences improves a model; it does not certify it.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Comparisons collected in chat become a dataset (Priority: P1)

Users express preferences during normal conversation, and an admin exports those comparisons as a preference dataset.

**Why this priority**: This is the roadmap's acceptance bar and the feature's distinguishing idea — the platform is its own labelling tool.

**Independent Test**: Collect several comparisons through the chat UI and export them as a valid preference dataset.

**Acceptance Scenarios**:

1. **Given** a response, **When** a user marks it with a thumb, **Then** a feedback row records the judgement with user, model, adapter, and conversation identity.
2. **Given** a response, **When** a user regenerates and picks the better candidate, **Then** a comparison is recorded with the chosen and rejected responses.
3. **Given** collected feedback, **When** an admin exports it with filters for model, date, tag, or user, **Then** a preference dataset is produced and registered.
4. **Given** that dataset, **When** it is registered, **Then** it passes the same schema validation and analysis as any other dataset.

---

### User Story 2 - A preference run continues from a supervised adapter (Priority: P1)

An admin trains a preference objective starting from an existing adapter, and the resulting adapter records its lineage.

**Why this priority**: This is the standard post-training recipe and the roadmap's second acceptance bar.

**Independent Test**: Run a preference job chained from a supervised adapter and confirm it completes with recorded lineage.

**Acceptance Scenarios**:

1. **Given** an existing adapter and a preference dataset, **When** a preference job is submitted chaining from it, **Then** training starts from that adapter rather than the bare base.
2. **Given** that job, **When** memory is reserved, **Then** the reservation reflects the reference-copy requirement of roughly twice base weights.
3. **Given** a completed job, **When** the adapter is viewed, **Then** its lineage through the supervised adapter to the base model is inspectable.
4. **Given** insufficient memory even after eviction, **When** admission is attempted, **Then** the job is refused with a reason.

---

### User Story 3 - Users control and understand feedback capture (Priority: P1)

Users are told feedback may be used to improve models here, and any user can turn capture off entirely.

**Why this priority**: Collecting people's conversations to train models without clear disclosure and a genuine opt-out is not acceptable, regardless of how private the deployment is.

**Independent Test**: Disable capture for a user, exercise every feedback surface, and confirm no rows are written.

**Acceptance Scenarios**:

1. **Given** any user, **When** they use the chat UI, **Then** the disclosure that feedback may be used to improve models on this platform is visible in the interface.
2. **Given** a user who has opted out, **When** they use any feedback surface, **Then** no feedback row is written for them.
3. **Given** a user who opts out after feedback exists, **When** they opt out, **Then** the setting takes effect immediately for new feedback, and prior rows are handled according to the stated policy.
4. **Given** an export, **When** it runs, **Then** it excludes users who have opted out.

---

### User Story 4 - An admin reviews comparisons deliberately (Priority: P2)

An admin works through a queue of pairwise comparisons, adding judgements that organic feedback did not produce.

**Why this priority**: Organic feedback is sparse and biased toward memorable failures; a review queue fills gaps. It is valuable but not required for the loop to close.

**Independent Test**: Populate the review queue, judge several pairs, and confirm the judgements join the exportable pool.

**Acceptance Scenarios**:

1. **Given** candidate pairs, **When** an admin opens the review queue, **Then** pairs are presented for judgement.
2. **Given** a judged pair, **When** it is submitted, **Then** a feedback row is written attributed to the admin as reviewer.
3. **Given** admin judgements, **When** an export runs, **Then** they are included and distinguishable from user feedback.

---

### Edge Cases

- A user gives contradictory feedback on one response: the most recent judgement MUST win and the change MUST be recorded rather than producing duplicate conflicting rows.
- An export produces too few rows to train on: the admin MUST be warned before a job is submitted against it.
- Feedback references a model or adapter that has since been retired: rows MUST remain exportable with their recorded identity intact.
- A regenerate produces an identical response: it MUST NOT be recorded as a comparison, since there is no preference to express.
- A preference dataset contains rows whose chosen and rejected responses are identical: they MUST be rejected at validation.
- A preference job's reference copy cannot fit alongside the policy: the job MUST be refused at admission rather than failing partway.
- A user deletes a conversation that produced feedback: the deletion policy for derived feedback MUST be stated and applied consistently.
- An adapter trained on feedback is used for a write-capable task: it MUST be refused until it passes harness verification.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The system MUST support a preference dataset type whose rows carry a prompt, a chosen response, and a rejected response, validated on registration.
- **FR-002**: Rows whose chosen and rejected responses are identical MUST be rejected at validation.
- **FR-003**: The system MUST support preference objectives through the existing objective registry, without changing the training specification's shape.
- **FR-004**: A preference job MUST support chaining from an existing adapter as its starting point.
- **FR-005**: An adapter's lineage through any chained adapters to its base model MUST be recorded and inspectable.
- **FR-006**: A preference job's memory estimate MUST account for a frozen reference copy alongside the policy, and MUST reserve through the ledger.
- **FR-007**: A preference job that cannot fit after eviction MUST be refused with a reason.
- **FR-008**: The chat UI MUST allow marking any response with a positive or negative judgement.
- **FR-009**: The chat UI MUST support regenerating a response and choosing between candidates, recording the unchosen one as rejected.
- **FR-010**: An identical regenerated response MUST NOT be recorded as a comparison.
- **FR-011**: Feedback rows MUST record user, model, adapter, conversation, judgement, and timestamp.
- **FR-012**: Contradictory feedback on one response MUST resolve to the most recent judgement, with the change recorded.
- **FR-013**: The system MUST provide an admin pairwise review queue whose judgements are recorded and attributed to the reviewing admin.
- **FR-014**: Admin judgements MUST be distinguishable from user feedback in exports.
- **FR-015**: An admin MUST be able to export feedback as a preference dataset with filters for model, date, tag, and user.
- **FR-016**: Exports MUST exclude users who have opted out.
- **FR-017**: The interface MUST disclose that feedback may be used to improve models on this platform.
- **FR-018**: Feedback capture MUST be disableable per user, and when disabled no feedback rows may be written for that user.
- **FR-019**: The system MUST state and consistently apply a policy for feedback derived from deleted conversations and for prior feedback after opt-out.
- **FR-020**: An adapter trained on preferences MUST pass the harness verification gate before write-capable use.
- **FR-021**: An export producing too few rows to train on MUST warn the admin.
- **FR-022**: Feedback and export routes MUST be audited, and export MUST require the admin role.

### Key Entities

- **Feedback Row**: One recorded judgement. User, conversation, message, model, adapter, judgement type, chosen and rejected references, source (user or admin review), timestamp.
- **Preference Dataset**: A registered corpus of comparisons. Rows of prompt with chosen and rejected responses, provenance including export filters, validation and analysis results.
- **Preference Job**: A training run on a preference objective. Spec, initial adapter, reference-copy reservation, checkpoints, metrics, resulting adapter.
- **Adapter Lineage**: The chain from an adapter through prior adapters to a base model, with the objective and dataset at each step.
- **Feedback Preference**: A user's capture setting. User, enabled flag, changed-at.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: Comparisons collected in chat export to a valid, registered preference dataset.
- **SC-002**: A preference run continues from a supervised adapter and completes, with lineage inspectable.
- **SC-003**: The resulting adapter is refused write-capable tasks until it passes harness verification.
- **SC-004**: A user who has opted out generates zero feedback rows across every feedback surface.
- **SC-005**: The feedback disclosure is visible in the chat interface without requiring navigation to find it.
- **SC-006**: A preference job's reservation reflects the reference-copy requirement and never pushes a node into swap.
- **SC-007**: Exports exclude opted-out users in 100% of cases.
- **SC-008**: Identical regenerated responses are never recorded as comparisons.

## Assumptions

- Features 001–017 have shipped: training jobs with the objective registry, datasets and analysis, the chat UI, evaluation, the ledger, and audit.
- Preference objectives are provided through a pinned external trainer, with an in-house implementation as the documented fallback if that dependency stalls; either sits behind the objective registry so specifications are unaffected.
- Both Studios have 256 GB (verified 2026-08-29), which is what makes a roughly-double reservation feasible for mid-size bases and infeasible for the largest ones — the refusal path matters in practice.
- Reward-function and verifier frameworks needed by reinforcement-style objectives are explicitly backlog and out of scope.
- The stated policy for feedback after conversation deletion or opt-out is an operator decision recorded in the spec's implementation; this feature requires only that it exist, be stated, and be applied consistently.
- Feedback capture surfaces extend the chat UI from feature 014, whose message model was built to accommodate them.
- Per Principle VII, integration tests exercise the export and a tiny preference run; real runs are verified manually on the cluster.
