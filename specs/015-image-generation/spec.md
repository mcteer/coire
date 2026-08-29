# Feature Specification: Image Generation

**Feature Branch**: `015-image-generation`

**Roadmap ID**: 013 (Phase 4 — Chat UI, images, training)

**Created**: 2026-08-29

**Status**: Draft

**Input**: User description: "Typed `ImageSpec` (txt2img, img2img, fill, control, LoRA stack, upscale, n, seed) with per-model bounds; resident `mflux` image worker per Studio managed by coire-node with ledger reservation and idle TTL; queued jobs with SSE events and cancel; stage-level LRU; outputs streamed to a `coire-blobs` volume on core with expiring URLs; full spec embedded in PNG metadata; registry kinds for image models; admin presets; `/v1/images/generations` adapter; gallery with reuse-settings/regenerate; explicit entitlement enforcement and NSFW tagging."

## Overview

This feature adds image generation as a first-class subsystem: a typed specification that is the contract rather than a node graph, a resident image worker per Studio managed exactly like a language-model engine, queued jobs with progress and cancellation, stage-level caching so iterating on a seed does not re-encode a prompt, outputs that live on core rather than the workers, and every image carrying the full recipe that produced it. It also carries the platform's most sensitive policy surface: explicit content is permitted for entitled users, which makes entitlement, audit, and gallery filtering load-bearing rather than incidental.

## Clarifications

### Session 2026-08-29

- Q: Why a typed spec rather than a node graph? → A: Because a user-editable graph is a code-execution surface on a public platform. Every pipeline shape the platform supports is a fixed stage sequence, and new shapes arrive as code plus a spec version, never as user-supplied node definitions. Admins get presets — named, partially-filled specs — as the one graph-like affordance.
- Q: How does image work coexist with language decoding on the same node? → A: The image worker is a resident process managed like any engine, holding its model, reserving its footprint in the ledger, and obeying an idle TTL. The scheduler serialises image jobs per node and prefers Studio B, where image models are pinned by default, so image work does not starve decoding on the node running the largest model.
- Q: What exactly does the stage cache key on? → A: Text-encoder outputs on the model plus prompt and negative prompt; LoRA-patched weights on the model plus the LoRA stack; control preprocessor outputs on the control type plus a hash of the control image. Changing only the seed or step count therefore reaches the denoiser directly. The LoRA-patch cache holds one stack at a time because a patched copy of a multi-billion-parameter model is a real memory cost.
- Q: How is explicit content governed? → A: Generation is unfiltered at the prompt and model level for entitled users — there is no safety checker in the pipeline. The controls are entitlement, identity, and audit: a per-user grant, an authenticated human identity, an audit row per generation, and never available to service tokens. A classifier tags outputs for gallery filtering and to keep them out of shared views, never to block an entitled user's request.
- Q: Where do output bytes live? → A: Streamed back to core and written to a blob volume there, served through authenticated expiring URLs. Studios retain nothing after a job completes, which keeps user content off the worker nodes.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - An entitled user generates an image from the UI (Priority: P1)

A user describes an image, submits it, watches progress, and receives a result they can view and reuse.

**Why this priority**: This is the feature's core loop and the roadmap's first acceptance bar.

**Independent Test**: Submit a generation from the UI and receive an image with visible progress along the way.

**Acceptance Scenarios**:

1. **Given** an entitled user and a published image model, **When** they submit a generation, **Then** a job is queued and its position is returned immediately.
2. **Given** a queued job, **When** it progresses, **Then** queued, started, progress, and completion events are delivered in order.
3. **Given** a completed job, **When** it finishes, **Then** the images are retrievable through authenticated, expiring URLs.
4. **Given** a running job, **When** the user cancels it, **Then** it stops and is recorded as cancelled.
5. **Given** a submitted specification, **When** it violates per-model bounds, **Then** it is refused at validation with the offending field named.

---

### User Story 2 - An image reproduces itself from its own metadata (Priority: P1)

An image dragged back into the interface reconstructs the exact settings that produced it.

**Why this priority**: The roadmap names it explicitly, and it is what makes iteration and auditing possible without a separate record-keeping discipline.

**Independent Test**: Generate an image, drag it back in, and confirm the reconstructed settings match, then regenerate identically.

**Acceptance Scenarios**:

1. **Given** a generated image, **When** it is inspected, **Then** it embeds the full resolved specification including effective seed, model variant, and LoRA versions.
2. **Given** that image dragged into the UI, **When** it is read, **Then** the settings are reconstructed exactly.
3. **Given** reconstructed settings, **When** regenerated unchanged, **Then** the output is identical.
4. **Given** any image, **When** an admin inspects it, **Then** the stored specification on the image record matches what is embedded.

---

### User Story 3 - Iterating on a seed is fast (Priority: P2)

Changing only the seed or step count skips prompt encoding, visibly.

**Why this priority**: This is the difference between an image tool that feels responsive and one that does not, and the roadmap makes it an acceptance bar with a trace-visible test.

**Independent Test**: Generate, then regenerate changing only the seed, and confirm the prompt-encoding stage is skipped in the trace.

**Acceptance Scenarios**:

1. **Given** a completed generation, **When** only the seed changes, **Then** the cached text-encoder output is reused and the skip is visible in the trace.
2. **Given** two users using the same preset, **When** both generate, **Then** they share the prompt encoding.
3. **Given** a changed prompt, **When** generation runs, **Then** the encoder runs again rather than serving a stale cached value.
4. **Given** a changed LoRA stack, **When** generation runs, **Then** the patched-weight cache is replaced rather than accumulating.

---

### User Story 4 - Explicit content is governed, not filtered (Priority: P1)

An entitled user generates without prompt-level filtering; a non-entitled user cannot, and every such generation is audited.

**Why this priority**: This is the platform's most sensitive policy surface. Getting entitlement, identity, and audit right is a precondition for the capability existing at all.

**Independent Test**: Attempt an explicit-capable preset as an entitled user, a non-entitled user, and a service token, and confirm the three distinct outcomes.

**Acceptance Scenarios**:

1. **Given** an entitled authenticated human user, **When** they generate, **Then** the pipeline applies no prompt-level filtering and the generation is audited with the entitlement recorded.
2. **Given** a non-entitled user, **When** they name an explicit-capable preset, **Then** the request is refused at validation and the refusal is audited.
3. **Given** a non-entitled user, **When** they browse models and presets, **Then** explicit-capable presets are absent from their list.
4. **Given** a service token, **When** it attempts an explicit generation, **Then** it is refused regardless of any entitlement on the owning user.
5. **Given** any output, **When** it is produced, **Then** it is tagged for gallery filtering and kept out of shared views when tagged.

---

### User Story 5 - Image work does not starve language decoding (Priority: P2)

Image jobs run without degrading chat responsiveness on the same node.

**Why this priority**: Both subsystems contend for the same accelerator; the architecture names this as a real risk requiring explicit scheduling.

**Independent Test**: Run a sustained image workload while chatting against a model on the same node and confirm chat latency stays within bounds.

**Acceptance Scenarios**:

1. **Given** a node serving a language model, **When** image jobs are submitted, **Then** they are serialised per node and chat latency stays within its target.
2. **Given** both Studios available, **When** an image job is scheduled, **Then** Studio B is preferred.
3. **Given** the image worker idle beyond its TTL, **When** the TTL passes, **Then** it unloads and releases its reservation.
4. **Given** the image worker resident, **When** the ledger is inspected, **Then** its footprint appears as a reservation.

---

### Edge Cases

- A generation requests more images than the per-request bound: it MUST be refused at validation naming the bound.
- A control or initialisation image is malformed or too large: it MUST be refused with the reason rather than failing mid-pipeline.
- The worker crashes mid-job: the job MUST be recorded as failed with a reason, its reservation released, and the user notified through the event stream.
- The blob volume fills: submissions MUST be refused with a capacity reason rather than producing images that cannot be stored.
- An expiring URL is used after expiry: it MUST be refused, and a fresh URL MUST be obtainable by the owner.
- A user is de-entitled while holding previously generated explicit images: existing images MUST remain governed by their recorded entitlement and MUST NOT retroactively become visible in shared views.
- Two jobs request conflicting LoRA stacks concurrently on one node: they MUST serialise rather than thrashing the patched-weight cache.
- A preset references a retired model: it MUST be refused with a clear reason and flagged to the admin.
- A cancelled job's partial output MUST NOT be stored or served.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The system MUST accept a typed image specification covering text-to-image, image-to-image, fill, control, LoRA stacks, upscaling, count, and seed, validated against per-model bounds on submission.
- **FR-002**: The system MUST NOT expose user-editable pipeline graphs or user-supplied node definitions; supported pipeline shapes MUST be fixed stage sequences changed only by code and a spec version.
- **FR-003**: Admins MUST be able to define presets as named, partially-filled specifications that appear in the picker.
- **FR-004**: Each Studio MUST run a resident image worker managed by the node agent, reserving its footprint in the ledger and obeying an idle TTL.
- **FR-005**: Image jobs MUST be queued, MUST return a job identity and queue position immediately, and MUST be serialised per node.
- **FR-006**: Scheduling MUST prefer Studio B, where image models are pinned by default.
- **FR-007**: The system MUST stream queued, started, progress, completion, and error events per job, and MUST support cancellation.
- **FR-008**: A cancelled job's partial output MUST NOT be stored or served.
- **FR-009**: The worker MUST cache text-encoder outputs keyed on model, prompt, and negative prompt; LoRA-patched weights keyed on model and stack; and control preprocessor outputs keyed on control type and image hash.
- **FR-010**: The patched-weight cache MUST hold one stack at a time.
- **FR-011**: Output bytes MUST be streamed to core and stored on a blob volume there; Studios MUST retain nothing after a job completes.
- **FR-012**: Outputs MUST be served through authenticated, expiring URLs scoped to their owner.
- **FR-013**: Every output MUST embed the full resolved specification, including effective seed, model variant, and LoRA versions, and the same specification MUST be stored on the image record.
- **FR-014**: The system MUST reconstruct settings from a supplied image's embedded specification, and regenerating unchanged MUST produce an identical output.
- **FR-015**: Image model kinds — base model, LoRA, control model, and upscale model — MUST be acquired through the existing admin-only acquisition pipeline with image-specific validation, and MUST replicate to both Studios.
- **FR-016**: The system MUST expose an OpenAI-compatible image generation endpoint as a thin adapter onto the same specification.
- **FR-017**: Explicit generation MUST require a per-user entitlement granted only by an admin, an authenticated human identity, and MUST be refused to service tokens.
- **FR-018**: Every explicit generation and every refusal MUST be audited with the entitlement recorded.
- **FR-019**: Explicit-capable presets MUST be absent from non-entitled users' listings.
- **FR-020**: The pipeline MUST apply no prompt-level or model-level content filtering for entitled users.
- **FR-021**: Outputs MUST be tagged for gallery filtering, and tagged outputs MUST be kept out of shared or public views.
- **FR-022**: The gallery MUST offer reuse-settings and regenerate-with-new-seed on any image the user owns.
- **FR-023**: Submissions MUST be refused with a capacity reason when blob storage is exhausted.

### Key Entities

- **Image Spec**: The generation contract. Model, prompt, negative prompt, dimensions, steps, guidance, seed, count, LoRA stack, initialisation image and strength, mask, control settings, upscale settings, output settings.
- **Image Job**: One queued generation. Owner, spec, state, queue position, node, progress, timings, failure reason, entitlement recorded.
- **Image Record**: A produced output. Owner, job, storage reference, resolved spec, content tags, entitlement under which produced, created-at.
- **Preset**: An admin-defined partial spec. Name, description, model, LoRA stack, prompt prefix, defaults, explicit-capable flag, entitlement requirement.
- **Image Worker**: A resident generation process. Node, model, reservation, idle TTL, state, cache occupancy.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: Entitled users generate successfully through both the UI and the OpenAI-compatible endpoint.
- **SC-002**: An image dragged back into the UI reproduces itself exactly from embedded metadata.
- **SC-003**: Changing only the seed skips prompt encoding, visible in the trace, in 100% of trials.
- **SC-004**: Non-entitled requests naming an explicit preset are refused and audited, in 100% of attempts.
- **SC-005**: Service tokens are refused explicit generation in 100% of attempts.
- **SC-006**: Image jobs do not starve decoding on the same node; chat latency stays within its target during a sustained image workload.
- **SC-007**: Studios retain no output bytes after job completion, verified by inspection.
- **SC-008**: A cancelled job produces no stored or served output.
- **SC-009**: Every explicit generation has a corresponding audit row naming the user, entitlement, and time.

## Assumptions

- Features 001–014 have shipped: the acquisition pipeline, the ledger with reservations and idle TTL, instances, entitlements and audit, the console, and the chat UI this extends.
- The image engine is driven directly by the node agent as a resident process, exactly like a language-model engine; no wrapper is introduced.
- Image models are pinned to Studio B by default, consistent with the architecture's placement guidance.
- The explicit-content entitlement type was defined in feature 007; this feature enforces it at generation.
- Content tagging is for gallery filtering and shared-view exclusion only, and never blocks an entitled user's request.
- Blob storage is a volume on core behind the API; object-storage semantics are an implementation option, not a requirement.
- Training image LoRAs on the platform is backlog; image LoRAs are imported through the acquisition pipeline.
- A second worker type behind the same specification is the intended path if a needed model is unsupported; that is backlog and does not change this contract.
- Per Principle VII this feature requires a documented manual verification on the real cluster before merge.
