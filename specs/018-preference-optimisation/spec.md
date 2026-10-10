# Feature Specification: Preference Optimisation and Feedback Capture

**Feature Branch**: `feat/018-preference-optimisation`

**Roadmap ID**: 014b (Phase 4 — Chat UI, images, training)

**Created**: 2026-08-29

**Updated**: 2026-10-09

**Status**: Implemented; four exact shared profiles qualified; required native resource CI and final release reconciliation pending (T076/T077)

**Input**: User description: "`preference` dataset type; `objective=dpo|orpo` jobs with `init_adapter` chaining from an SFT adapter and 2× base memory reservation; chat UI thumbs, regenerate-and-compare, and admin pairwise review queue writing feedback rows; admin export of feedback → preference dataset with filters; per-user feedback opt-out and disclosure."

## Overview

Users can explicitly contribute feedback and response comparisons without a separate labelling tool. Merely chatting does not enroll conversation content into a training dataset. This feature adds the preference dataset type and the objectives that consume it, the feedback capture surfaces in the chat UI and an admin review queue, and the export path that turns collected comparisons into a training dataset. It also carries a real obligation to users: they are told their feedback may be used to improve models here, and they can opt out.

## Clarifications

### Session 2026-08-29

- Q: Why does preference training need roughly twice the memory of the base model? → A: DPO requires a frozen reference matching the initial policy; ORPO is reference-free. The original blanket two-copy assumption is corrected in this refresh: reserve an objective-specific measured envelope, including policy, DPO reference where needed, adapters, optimizer, activations, buffers and safety headroom.
- Q: How does a preference run relate to a prior supervised run? → A: Through adapter chaining. A preference job may start from an existing adapter rather than the bare base, which is the standard recipe — supervised fine-tuning first, then preference optimisation on the same adapter. The chain is recorded so an adapter's lineage is inspectable.
- Q: What exactly does the chat UI capture? → A: Thumbs on completed assistant responses, and a regenerate-and-compare flow where producing a second candidate lets the user pick the better one, making the loser the rejected response. Both write feedback rows carrying user, model, adapter, and conversation identity.
- Q: What are users told? → A: That feedback may be used to improve models on this platform, disclosed in the interface rather than buried. Capture can be disabled per user, and when disabled no feedback rows are written for that user at all — not written-and-filtered.
- Q: Does a preference-trained adapter get special trust? → A: No. It passes through the same harness verification gate as any other adapter before write-capable tasks will use it. Training on preferences improves a model; it does not certify it.

### Session 2026-10-08

- Q: When someone disables feedback capture or deletes a conversation, what should happen to feedback already exported into a training dataset? → A: Preserve published datasets and adapters; purge unexported feedback. Accepted by the user. FR-016/018/019 and US3 define immediate exclusion and the 24-hour purge deadline.
- Repository-derived clarification: shipped training recipes are immutable v1 and v2 SFT documents. Preference intent requires a new version rather than altering historical hashes (FR-003/024).
- Corrected technical assumption: ORPO has no reference model; DPO freezes the initial policy including its adapter (FR-004/006). The earlier blanket reference-copy explanation is superseded.
- Bounded design defaults: both objectives, single-Studio LoRA/QLoRA; explicit text-only comparisons; no passive harvesting or implicit thumb-to-pair conversion. Other runtime combinations remain refused until separately specified and measured.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Comparisons collected in chat become a dataset (Priority: P1)

Users express preferences during normal conversation, and an admin exports those comparisons as a preference dataset.

**Why this priority**: This is the roadmap's acceptance bar and the feature's distinguishing idea — the platform is its own labelling tool.

**Independent Test**: Collect several comparisons through the chat UI and export them as a valid preference dataset.

**Acceptance Scenarios**:

1. **Given** a completed assistant response owned by the user, **When** the user marks it with a thumb, **Then** a feedback row records the judgement with user, model, adapter, and conversation identity.
2. **Given** a response, **When** a user regenerates and picks the better candidate, **Then** a comparison is recorded with the chosen and rejected responses.
3. **Given** collected feedback, **When** an admin exports it with filters for model, date, tag, or user, **Then** a preference dataset is produced and registered.
4. **Given** that dataset, **When** it is registered, **Then** it passes preference schema validation, deterministic split and model-specific analysis before use.
5. **Given** only thumbs or an incomplete/identical pair, **When** an export runs, **Then** no chosen/rejected pair is invented from that feedback.
6. **Given** a comparison request, **When** regeneration completes, **Then** both candidates share the captured prompt and exact serving target; only an explicit choice replaces the active conversation answer, and the unchosen candidate stays outside later model context.

---

### User Story 2 - A preference run continues from a supervised adapter (Priority: P1)

An admin trains a preference objective starting from an existing adapter, and the resulting adapter records its lineage.

**Why this priority**: This is the standard post-training recipe and the roadmap's second acceptance bar.

**Independent Test**: Run a preference job chained from a supervised adapter and confirm it completes with recorded lineage.

**Acceptance Scenarios**:

1. **Given** an existing adapter and a preference dataset, **When** a preference job is submitted chaining from it, **Then** training starts from that adapter rather than the bare base.
2. **Given** that job, **When** memory is reserved, **Then** DPO includes a frozen copy of the initial policy and ORPO includes no reference copy; both include all training overhead and require current measured support.
3. **Given** a full committed checkpoint, **When** the scheduler or node restarts, **Then** recovery restores the optimizer, sampler, randomness and frozen input identities; initial adapter loading does not overwrite recovered policy weights.
4. **Given** declared evaluation suites, **When** a recoverable checkpoint or final adapter is committed, **Then** feature 017 evaluation obligations run under its existing ownership rules independently of harness verification.
5. **Given** a completed job, **When** the adapter is viewed, **Then** its lineage through the supervised adapter to the base model is inspectable.
6. **Given** insufficient memory even after eviction, **When** admission is attempted, **Then** the job is refused with a reason.

---

### User Story 3 - Users control and understand feedback capture (Priority: P1)

Users are told feedback may be used to improve models here, and any user can turn capture off entirely.

**Why this priority**: Collecting people's conversations to train models without clear disclosure and a genuine opt-out is not acceptable, regardless of how private the deployment is.

**Independent Test**: Disable capture for a user, exercise every feedback surface, and confirm no rows are written.

**Acceptance Scenarios**:

1. **Given** any user, **When** they use the chat UI, **Then** the disclosure that feedback may be used to improve models on this platform is visible in the interface.
2. **Given** a user who has opted out, **When** they use any feedback surface, **Then** no feedback row is written for them.
3. **Given** a user who opts out after feedback exists, **When** they opt out, **Then** the setting takes effect immediately for new feedback, and prior unexported feedback content and candidate copies become inaccessible immediately and are purged within 24 hours; re-enabling starts a new capture period.
4. **Given** an export, **When** it publishes a dataset, **Then** it rechecks capture settings and conversation deletion and excludes withdrawn contributions.
5. **Given** a conversation deletion, **When** it commits, **Then** its feedback is unavailable to reads/review/export immediately and the same purge policy applies.
6. **Given** an already published immutable dataset or a trained adapter, **When** the user withdraws feedback, **Then** the interface explains the historical-artifact policy in FR-019; the system never claims to remove knowledge from a trained model.

---

### User Story 4 - An admin reviews comparisons deliberately (Priority: P2)

An admin works through a queue of pairwise comparisons, adding judgements that organic feedback did not produce.

**Why this priority**: Organic feedback is sparse and biased toward memorable failures; a review queue fills gaps. It is valuable but not required for the loop to close.

**Independent Test**: Populate the review queue, judge several pairs, and confirm the judgements join the exportable pool.

**Acceptance Scenarios**:

1. **Given** eligible pairs explicitly created through comparison, **When** an admin opens the review queue, **Then** unreviewed pairs are presented with source provenance and without hidden reasoning or unrelated conversation content.
2. **Given** a judged pair, **When** it is submitted, **Then** a feedback row is written attributed to the admin as reviewer.
3. **Given** admin judgements, **When** an export selects that source, **Then** they are distinguishable from user feedback; the default export prefers the latest owner judgement, otherwise the latest admin judgement, and emits at most one row per pair.
4. **Given** two admins reviewing the same pair, **When** the second submits a stale version, **Then** the system reports a conflict instead of silently replacing the first judgement.
5. **Given** an opted-out conversation owner, **When** an admin tries to review their pair, **Then** the review is refused; admin status cannot bypass the owner's capture choice.

---

### Edge Cases

- A user gives contradictory feedback on one response: the most recent judgement MUST win and the change MUST be recorded rather than producing duplicate conflicting rows.
- An export produces too few rows to train on: the admin MUST be warned before a job is submitted against it.
- Feedback references a model or adapter that has since been retired: rows MUST remain exportable with their recorded identity intact.
- A regenerate produces an identical response: retain only a content-free operation outcome, never an exportable preference pair.
- A preference dataset contains rows whose chosen and rejected responses are identical: they MUST be rejected at validation.
- A DPO reference cannot fit alongside its policy, or an ORPO pair-training envelope cannot fit: refuse at admission rather than failing partway.
- A user deletes a conversation that produced feedback: apply FR-019 immediately to unexported contributions, while preserving previously published snapshots.
- An adapter trained on feedback is used for a write-capable task: it MUST be refused until it passes harness verification.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: Support uploaded or exported preference datasets containing a shared text conversation prompt, one chosen text assistant response and one rejected text assistant response; validate before use. Initial support excludes images, file-derived context, tools, agent/code runs and hidden reasoning.
- **FR-002**: Reject empty or identical chosen/rejected answers, malformed conversation roles and any sample with no supervised response tokens. Group all comparisons sharing the same canonical prompt into one deterministic split; reject training/validation overlap across sources.
- **FR-003**: Support both `dpo` and `orpo` through the existing training objective registry with objective-specific validated settings. Preserve existing SFT recipe versions, normalized identities, hashes, job history and resume behavior; introduce a new recipe version for preference training.
- **FR-004**: Allow an exact ready registered adapter on the same immutable base variant as the initial policy, or an explicit bare-base start. Validate adapter architecture, parameterization and trainable tensor layout; refuse incompatible or unavailable artifacts before launch. DPO's frozen reference MUST equal the initial policy, including the initial adapter when supplied.
- **FR-005**: Record and expose immutable adapter ancestry, base and initial adapter manifests, objective, dataset identities and source job. New adapters contain the full resulting adapter tensors, serve independently of ancestor file paths, and preserve lineage metadata after ancestor retirement.
- **FR-006**: Reserve the measured full objective-specific memory envelope through the ledger: DPO policy plus frozen reference, ORPO policy alone, and both objectives' optimizer, activations, adapter tensors, buffers and safety margin. Initial support is single-Studio dense LoRA and affine 4-bit/group-64 QLoRA; preference DoRA and distributed placement are explicitly refused.
- **FR-007**: Refuse impossible memory fits and unsupported or unmeasured configurations before launch. Protect pinned/leased serving models, preserve image/training exclusion, require fresh exact coexistence evidence for shared-node chat, and retain reservations until owned-process termination is proven.
- **FR-008**: Allow an authenticated conversation owner to set or clear thumbs on their completed assistant responses. Thumbs alone MUST NOT become preference training pairs.
- **FR-009**: Allow an owner to regenerate the latest eligible completed text answer against its captured prompt and exact serving target, see both candidates, and choose one. Freeze the source revision, allow only one pending comparison per conversation, and block context-changing turns until it is chosen or dismissed. Only the chosen answer enters subsequent conversation context.
- **FR-010**: Identical, failed, cancelled, empty or incomplete candidates MUST NOT produce an exportable comparison. A minimal content-free operation receipt may record the outcome. Dismissing a pair keeps the original active answer; failed regeneration releases the conversation for continued chat.
- **FR-011**: Feedback MUST bind the owner, actor, conversation, source message/turn, recorded model identity, judgement, source and timestamp. Comparisons additionally require exact variant/adapter manifests, source revision and prompt identity. Legacy thumbs may explicitly report unavailable exact provenance and remain non-exportable; never infer missing manifests from current defaults. Resolve evidence server-side and reject foreign, stale or fabricated references.
- **FR-012**: Latest accepted owner judgement wins, with optimistic version control, request idempotency and content-free change audit. A repeated request MUST NOT create duplicate feedback or a second regeneration charge.
- **FR-013**: Provide an authenticated admin review queue of eligible explicitly created comparison pairs. Admin decisions include chosen candidate or skip and bind the reviewing admin; a skipped pair remains undecided and can be revisited.
- **FR-014**: Preserve owner and admin judgements as separate sources. Exports choose `owner`, `admin`, or default `owner_preferred`; default uses the latest owner choice when present, otherwise the latest admin choice, never duplicate/conflicting rows for one pair.
- **FR-015**: An admin may create a durable idempotent export filtered by exact model/adapter, source, inclusive start/exclusive end date, feedback tag and owner. Export produces one immutable private registered preference dataset with a provenance manifest, source versions, actual inclusion counts and exclusions. It is not a public download link.
- **FR-016**: Revalidate owner capture eligibility, source deletion and current feedback versions at dataset publication. An opt-out/deletion committed before publication excludes that contribution, including when generation, review or export began earlier. Abandoned temporary export content is cleaned after restart.
- **FR-017**: Show a concise feedback-training disclosure beside chat feedback controls and the capture setting, including what withdrawal does to prior published datasets and trained models. Feedback controls and comparison selection are keyboard accessible with announced pending, conflict and failure states.
- **FR-018**: Feedback capture is enabled by default after visible disclosure and can be disabled by the owner. When disabled, no new feedback, comparison content or admin review judgement for that owner's conversations may be stored. Disable atomically invalidates in-flight capture; audits may record actor/IDs/reason but never content. Re-enable MUST NOT revive withdrawn contributions.
- **FR-019**: Historical-artifact policy: opt-out or conversation deletion immediately hides unexported feedback and copied candidate content and purges it within 24 hours, retaining content-free audit/provenance tombstones. Datasets already published and jobs/adapters already derived from those immutable datasets remain unchanged; withdrawal affects future exports. This limitation MUST be disclosed before contribution.
- **FR-020**: Preference outputs remain admin-only and unverified until independently evaluated and curated through existing paths. Harness verification MUST apply to the exact new base/adapter pair; no trust is inherited from the parent, reference, task score or judge score.
- **FR-021**: Exports below 20 eligible pairs MUST warn about small-sample limitations. Fewer than two distinct prompt groups cannot produce nonempty disjoint train/validation splits and MUST fail dataset readiness and training admission with a clear reason; no automatic data duplication or split leakage.
- **FR-022**: Authenticate and scope all feedback, settings, review, export, dataset and training routes; owner operations require ownership and admin operations require admin scope. Audit capture changes, accepted feedback mutations, admin actions and refused privacy/authorization actions without storing prompt/response content in telemetry or audit.
- **FR-023**: Reuse durable training lifecycle, complete checkpoints, two independently verified Studio copies, fenced recovery, cancellation and drained rollback. Checkpoint state includes objective/runtime/input identities, optimizer/schedule, policy tensors, sampler and RNG state; DPO reference is reconstructed from immutable initial artifacts. Never silently restart from initial weights after a resume failure.
- **FR-024**: Preference recipes may opt into feature 017 task/judge suites and recoverable checkpoint schedules; declared final evaluations are mandatory obligations. Keep training success and evaluation outcomes distinct; retain base-versus-result comparison semantics and parent lineage separately. Objective loss and preference accuracy are durable job metrics, not harness certification.
- **FR-025**: Keep models, tokenization, numerical training and evaluation on Studios under node-owned processes; core performs bounded validation, orchestration and storage only. Consume only registry/acquisition-approved local artifacts, with no user code, remote model code, automatic downloads or caller-specified filesystem paths.
- **FR-026**: Bound capture, candidate lifetimes, pages, source rows, export bytes and concurrent work; apply existing chat charging/budgets to regeneration and private dataset quotas to exports. Enable preference training separately with a default-off admission flag while cleanup, history and cancellation continue to work.
- **FR-027**: Emit content-free structured logs, traces and bounded metrics for feedback, export, withdrawal cleanup and preference jobs; include Jobs dashboard panels and baseline alerts for stuck exports/cleanup and training faults, effective with historical diagnostics disabled.
- **FR-028**: Provide recipe and console submission, dataset analysis, adapter lineage and preference metric views using existing admin/CLI workflows. Contract tests cover each changed boundary; tiny-model numerical/train/resume/serve tests and real-Studio acceptance cover every advertised objective/parameterization combination before enablement.

### Key Entities

- **Feedback Preference**: Owner, enabled flag, monotonically increasing capture generation, disclosure version and change time.
- **Feedback Row**: Current thumb or pair judgement with actor/owner, source, exact identities, version and captured eligibility generation; content-free mutation history.
- **Comparison Pair**: Explicit regeneration intent, frozen prompt/source revision, exact target, original and new visible answer, terminal outcome and active answer selection.
- **Preference Export**: Durable admin request, filters/source policy, frozen provenance, publication eligibility checks, progress, resulting dataset and failure/cleanup state.
- **Preference Dataset**: Immutable private prompt/chosen/rejected examples, grouped split, model-specific analysis, source provenance and small-sample diagnostics.
- **Preference Job**: Versioned recipe, objective-specific settings, initial/reference identities, measured reservation, recoverable checkpoints, metrics, evaluations and result adapter.
- **Adapter Lineage**: Immutable base-to-parent-to-result metadata, independent final adapter artifact and exact verification status.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: At least 20 explicit comparisons across at least two prompt groups export to one valid private dataset, with exactly one chosen/rejected row per selected pair and no fabricated pairs from thumbs.
- **SC-002**: Both DPO and ORPO complete a tiny run from a supervised adapter and a bare-base start; resulting adapter lineage and exact serving are demonstrable for each advertised parameterization.
- **SC-003**: Every new preference adapter is refused write-capable tasks until its own exact harness gate passes; parent verification and task/judge scores never unlock it.
- **SC-004**: Across concurrent generation, review, export and withdrawal tests, disabled capture yields zero new contribution rows/content and all withdrawals hide affected source content immediately and purge it within 24 hours.
- **SC-005**: Users can find the disclosure and toggle capture without leaving chat; all feedback and compare actions are operable with keyboard alone and expose pending/error states to assistive technology.
- **SC-006**: Real-Studio qualification records zero swap growth, no unauthorized eviction and measured peak memory within the approved envelope; shared-node chat retains first-token latency at or below 1.5 seconds p95 and gateway overhead at or below 20 milliseconds p95, excluding model time.
- **SC-007**: 100% of contributions withdrawn before export publication are excluded, including export restart and racing requests; declared historical-artifact behavior is verified separately.
- **SC-008**: Identical, partial, stale, foreign and ineligible response pairs are never exported; comparison selection never puts both alternatives into subsequent chat context.
- **SC-009**: For each supported objective, interrupted training resumes from complete mirrored checkpoints with loss/final tensors matching an uninterrupted fixed-seed reference within declared numerical tolerances; reachable cancellation stops owned processes within five seconds and failed stop proof retains reservations.
- **SC-010**: Existing SFT v1/v2 recipe hashes, parsing, checkpoint recovery and evaluation obligations remain compatible; unsupported preference combinations fail before engine launch.
- **SC-011**: With 10,000 feedback records and 10 concurrent readers, feedback/settings mutations complete within 500 ms p95 and review pages within one second p95, excluding generation; a 10,000-pair export reaches local publication or a clear terminal error within five minutes after execution admission, excluding queued Studio token analysis.
- **SC-012**: Required dashboards, alerts, privacy-safe audit and operational recovery/rollback instructions are present; alert tests pass with historical diagnostics disabled and real-Studio execution evidence precedes release.

## Assumptions

- Baseline is merged features 016/017 and node collection-budget fix PR 94 (`d2e4bbf`). Reuse shipped contracts and lifecycle; the old blanket claim that all earlier roadmap acceptance is complete is not required.
- Initial preference training supports both DPO and ORPO on one Studio, with dense LoRA and affine 4-bit/group-64 QLoRA. Distributed preference training, preference DoRA, full tuning, reward/verifier/RL objectives, adapter fusion and model unlearning are outside this feature.
- Both Studios have 256 GB; actual admission uses observed available memory and exact supported profiles rather than this nominal capacity.
- Text-only explicit comparisons are the training source. Thumbs on other completed assistant responses remain feedback-only. There is no passive conversation harvesting or conversion of implicit behavior into training preferences.
- Withdrawal uses the user-confirmed historical-snapshot policy in FR-019. One clarification question was asked and answered; no product clarification remains unresolved. Engine selection, objective math and compatibility details are resolved in planning research.
- Implementation was subsequently authorized with `$speckit-implement`. The application and native runtime are implemented; all four exact shared profiles pass, including Studio B dense ORPO with complete exact overhead evidence. Required hosted resource CI awaits fresh results after fixing its framework-interpreter command mismatch. See [execution-record.md](execution-record.md) for current measured evidence.
