# Feature Specification: Image Generation

**Feature Branch**: `feat/015-image-generation`

**Roadmap ID**: 013 (Phase 4 — Chat UI, images, training)

**Created**: 2026-08-29

**Updated**: 2026-09-30

**Status**: In implementation; native runtime child in draft PR #31

**Input**: User description: "Typed `ImageSpec` (txt2img, img2img, fill, control, LoRA stack, upscale, n, seed) with per-model bounds; resident `mflux` image worker per Studio managed by coire-node with ledger reservation and idle TTL; queued jobs with SSE events and cancel; stage-level LRU; outputs streamed to a `coire-blobs` volume on core with expiring URLs; full spec embedded in PNG metadata; registry kinds for image models; admin presets; `/v1/images/generations` adapter; gallery with reuse-settings/regenerate; explicit entitlement enforcement and NSFW tagging."

## Overview

This feature adds image generation as a first-class subsystem: a typed specification that is the contract rather than a node graph, a resident image worker per Studio managed exactly like a language-model engine, queued jobs with progress and cancellation, stage-level caching so iterating on a seed does not re-encode a prompt, outputs that live on core rather than the workers, and every image carrying the full recipe that produced it. It also carries the platform's most sensitive policy surface: explicit content is permitted for entitled users, which makes entitlement, audit, and gallery filtering load-bearing rather than incidental.

## Clarifications

### Session 2026-09-30

- Q: What should “regenerate identically” guarantee? → A: Identical pixels with the same inputs, model versions, runtime, and hardware. Otherwise restore settings and disclose that exact reproduction is unavailable; preserving old environments after upgrades is outside scope.
- Q: Should recipe import accept generated PNGs larger than the generation-input limit? → A: Yes. Keep generation inputs capped at 10 MiB, accept recipe-only PNG imports up to the 64 MiB output limit, and extract at most 64 KiB of metadata without decoding pixels.

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
3. **Given** reconstructed settings and all original inputs, model versions, runtime, and hardware, **When** regenerated unchanged, **Then** decoded pixels are identical; file timestamps and container metadata need not be byte-identical.
4. **Given** any image, **When** an admin inspects it, **Then** the stored specification on the image record matches what is embedded.
5. **Given** a changed execution environment or a missing input, **When** the user restores settings, **Then** the interface discloses that exact reproduction is unavailable and never silently substitutes a dependency.

---

### User Story 3 - Iterating on a seed is fast (Priority: P2)

Changing only the seed or step count skips prompt encoding, visibly.

**Why this priority**: This is the difference between an image tool that feels responsive and one that does not, and the roadmap makes it an acceptance bar with a trace-visible test.

**Independent Test**: Generate, then regenerate changing only the seed, and confirm the prompt-encoding stage is skipped in the trace.

**Acceptance Scenarios**:

1. **Given** a completed generation with its encoding still resident, **When** only the seed changes, **Then** the cached text-encoder output is reused and the skipped work is observable.
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
- **FR-004**: Each Studio MUST support one resident image worker managed by the node agent, loaded on demand, reserving its full footprint in the ledger and obeying an idle TTL.
- **FR-005**: Image jobs MUST be queued, MUST return a job identity and queue position immediately, and MUST be serialised per node.
- **FR-006**: Scheduling MUST prefer Studio B, where image models are pinned by default.
- **FR-007**: The system MUST stream queued, started, progress, completion, and error events per job, and MUST support cancellation.
- **FR-008**: A cancelled job's partial output MUST NOT be stored or served.
- **FR-009**: The worker MUST cache text-encoder outputs keyed on model, prompt, and negative prompt; LoRA-patched weights keyed on model and stack; and control preprocessor outputs keyed on control type and image hash.
- **FR-010**: The patched-weight cache MUST hold one stack at a time.
- **FR-011**: Output bytes MUST be streamed to core and stored on a blob volume there; Studios MUST retain no job input or output files after a job completes. Only the bounded non-image stage caches in FR-009 may remain resident until eviction or unload.
- **FR-012**: Outputs MUST be served through authenticated, expiring URLs scoped to their owner.
- **FR-013**: Every output MUST embed the full resolved specification, including effective seed, model variant, and LoRA versions, and the same specification MUST be stored on the image record.
- **FR-014**: The system MUST reconstruct settings from a supplied image's embedded specification. Regeneration MUST produce identical decoded pixels when inputs, model versions, runtime, and hardware match; otherwise the interface MUST disclose that exact reproduction is unavailable. Metadata does not contain source image bytes, so missing initial, mask, or control images must be reattached and their digests validated.
- **FR-015**: Image model kinds — base model, LoRA, control model, and upscale model — MUST be acquired through the existing admin-only acquisition pipeline with image-specific validation, and MUST replicate to both Studios.
- **FR-016**: The system MUST expose an OpenAI-compatible image generation endpoint as a thin adapter onto the same specification.
- **FR-017**: Explicit generation MUST require a per-user entitlement granted only by an admin, an authenticated human identity, and MUST be refused to service tokens.
- **FR-018**: Every explicit generation and every refusal MUST be audited with the entitlement recorded.
- **FR-019**: Explicit-capable presets MUST be absent from non-entitled users' listings.
- **FR-020**: The pipeline MUST apply no prompt-level or model-level content filtering for entitled users.
- **FR-021**: Outputs MUST be tagged for gallery filtering, and tagged outputs MUST be kept out of shared or public views.
- **FR-022**: The gallery MUST offer reuse-settings and regenerate-with-new-seed on any image the user owns.
- **FR-023**: Submissions MUST be refused with a capacity reason when blob storage is exhausted.
- **FR-024**: Every job, event stream, input, image record, metadata import, and download MUST be authenticated and owner-scoped. Administrative inspection and cancellation MUST use explicit admin routes and be audited. A URL alone MUST NOT grant access.
- **FR-025**: Submission retries with the same owner and idempotency key MUST return the same job; reuse with different effective settings MUST fail. A control-plane restart MUST resume tracking accepted work without submitting duplicate generations. A worker crash MUST fail its active job without silently regenerating it.
- **FR-026**: Reconnecting clients MUST recover ordered job events without losing a terminal result. Cancellation MUST have one authoritative outcome even when it races completion; no result from a successfully cancelled job may be published. Owners and admins MUST be able to cancel queued and running jobs.
- **FR-027**: Presets and all referenced base models, adapters, control models, and upscalers MUST be resolved and authorised together. Explicit requirements MUST be the union of those dependencies and the requested content mode; overriding a preset MUST NOT remove a requirement. Current identity, scopes, entitlement, publication, and resource availability MUST be checked again before execution.
- **FR-028**: Jobs MUST retain the effective settings, immutable dependency versions, input digests, execution version, and seeds used for each output. Imported metadata is untrusted settings, confers no authority, and MUST NOT trigger acquisition, arbitrary file access, or remote URL fetches. Missing dependencies or inputs MUST produce a named validation error.
- **FR-029**: A batch MUST publish all requested outputs together only after storage and metadata validation. Failed, timed-out, or cancelled work MUST expose no partial output. Temporary inputs and outputs on a Studio MUST be deleted after termination; any bounded resident preprocessing cache MUST contain no retrievable source or generated image bytes.
- **FR-030**: Image inputs MUST be owned, bounded, validated still images; supported masks and control inputs MUST satisfy the selected model's requirements. Unsupported combinations MUST fail before allocating a worker. Output is PNG with embedded settings in this version. Settings-only import MUST accept every generated PNG within the output size limit, independently of the smaller generation-input limit; it MUST NOT turn a recipe-only upload into a generation input.
- **FR-031**: Queue admission MUST enforce per-owner and system limits, reserve output storage before accepting work, and report capacity or throttling reasons. Stored images remain private until owner deletion; configurable retention and quotas MUST include temporary uploads, failed transfers, and orphan cleanup. Deleted records MUST invalidate download grants immediately.
- **FR-032**: Classification failures MUST produce an `unknown` tag and a visible diagnostic; they MUST NOT block an entitled owner's completed image. Explicit or unknown images MUST never enter shared views. This release provides a private gallery and no public sharing workflow.
- **FR-033**: Operators MUST see queue depth, wait time, stage timing, cache reuse, worker reservation, storage capacity, failures, and cancellation delay. Actionable alerts and a dashboard MUST accompany the feature; disabling historical diagnostics MUST preserve metrics, alerts, and audits and disclose that history is unavailable.
- **FR-034**: The form, progress, gallery, metadata import, and cancellation flows MUST support keyboard use, labelled inputs, visible focus, both existing themes, and clear loading, empty, refusal, reconnecting, expired-link, and failure states.
- **FR-035**: Contract, lifecycle, authorization, and concurrency tests MUST cover every new boundary. Acceptance MUST include a local tiny-model integration test and documented operator verification of image generation and chat coexistence on the real cluster before merge.
- **FR-036**: Changes to presets and image model publication MUST use the existing admin and audit path. Image generation MUST not acquire models, expose an engine listener to users, execute on core, or add image tools to the coding-only MCP surface.

### Key Entities

- **Image Spec**: The generation contract. Model, prompt, negative prompt, dimensions, steps, guidance, seed, count, LoRA stack, initialisation image and strength, mask, control settings, upscale settings, output settings.
- **Image Job**: One queued generation. Owner, spec, state, queue position, node, progress, timings, failure reason, entitlement recorded.
- **Image Record**: A produced output. Owner, job, storage reference, resolved spec, content tags, entitlement under which produced, created-at.
- **Preset**: An admin-defined partial spec. Name, description, model, LoRA stack, prompt prefix, defaults, explicit-capable flag, entitlement requirement.
- **Image Worker**: A resident generation process. Node, model, reservation, idle TTL, state, cache occupancy.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: Entitled users generate successfully through both the UI and the OpenAI-compatible endpoint.
- **SC-002**: In ten same-environment round trips, an image dragged back into the UI restores every effective setting and produces identical decoded pixels when required inputs are present; changed environments and missing inputs are identified in every negative trial.
- **SC-003**: In 20 consecutive warm-cache trials with unchanged encoding inputs, changing only seed or step count reuses the encoding in every trial; eviction or worker restart is reported as a cache miss.
- **SC-004**: Non-entitled requests naming an explicit preset are refused and audited, in 100% of attempts.
- **SC-005**: Service tokens are refused explicit generation in 100% of attempts.
- **SC-006**: During a 15-minute mixed workload on a supported co-resident configuration, loaded single-node chat with prompts of at most 4,000 tokens maintains first-token latency at or below 1.5 seconds p95. Image dispatch waits or uses another eligible Studio when that bound cannot be maintained.
- **SC-007**: Studios retain no output bytes after job completion, verified by inspection.
- **SC-008**: A cancelled job produces no stored or served output.
- **SC-009**: Every explicit generation has a corresponding audit row naming the user, entitlement, and time.
- **SC-010**: Under a healthy control plane, accepted native submissions return a job identity within one second p95, independently of generation time; progress becomes visible within two seconds of a worker event.
- **SC-011**: On a reachable healthy node, cancellation stops execution within five seconds; during a partition, the job remains visibly cancelling until termination is proven and produces no downloadable output.
- **SC-012**: Restart/retry, forged-metadata, cross-owner, revoked-credential, storage-exhaustion, and cancellation-race tests produce no duplicate published batches, unauthorized access, or retrievable partial output.

## Scope Boundaries

All original modes remain in scope, with support advertised and tested per model rather than
promised for every model. The first usable increment is private text-to-image generation with
authorization, cancellation, durable ownership, and storage; it is not completion of feature 015.
The compatible generation endpoint accepts the supported text-to-image subset; advanced modes
use the native form and image specification. Public galleries, user graphs, external generation
providers, image LoRA training, video, and a second generation engine are outside this feature.

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
- Personal API keys represent their active human owner and require both image scopes for explicit work. Service, ops, run, and identity-free emergency credentials cannot generate images. Entitlement is checked at submission, dispatch, and publication; revocation cancels queued/running explicit work and denies new explicit-content downloads, while retained records keep their original entitlement and tag history.
- Stored outputs default to retention until owner deletion, subject to a finite storage quota. An operator may configure a shorter retention period, which must be shown to users before submission. Source images retained on core are required for exact reuse; deletion makes the corresponding reproduction unavailable.
- Exact same-node chat coexistence must be proven for each supported execution configuration. Serialising image jobs is necessary but insufficient; an unmeasured or failing combination remains unavailable for concurrent execution.

## Design reference

`docs/design/DESIGN.md` §6 "Images" and `docs/design/mockups/images.html` specify this surface: the
236px preset rail with a swatch per preset, the 400px `ImageSpec` form, and the 704px output panel
holding the current job's event timeline, a four-column square results grid, and worker residency
facts. The shell and tokens come from feature 008; the event-timeline component is shared with
training run logs.

Two rules carry behavioural weight: the entitlement pill appears on explicit presets, and generated
results carry an `explicit` tag when applicable — both depend on the entitlement model from feature
007 rather than on styling.
