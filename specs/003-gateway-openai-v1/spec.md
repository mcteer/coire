# Feature Specification: Gateway and OpenAI-Compatible /v1

**Feature Branch**: `003-gateway-openai-v1`

**Roadmap ID**: 002 (Phase 1 — Single-node inference works end to end)

**Created**: 2026-08-29

**Status**: Draft

**Input**: User description: "FastAPI gateway proxies `/v1/chat/completions` (streaming) and an Anthropic-compatible `/v1/messages` adapter to the right instance and serves `/v1/models` filtered to published, ready, entitled models with Coire extensions (load state, tags, description); rejects unknown model ids; usage capture, keep-alive while loading, Retry-After semantics."

## Overview

This is the feature that makes Coire usable from tools people already run. The gateway resolves a requested model against the registry, finds or triggers a loaded engine, and proxies the request — streaming straight through — while capturing usage. It speaks two dialects on one backend: OpenAI Chat Completions and an Anthropic Messages adapter, the latter being what lets Anthropic-SDK tools point at the platform by changing a base URL. It also establishes the boundary that matters most for safety: a caller's `model` string is resolved against the registry or rejected, and never reaches an engine.

## Clarifications

### Session 2026-08-29

- Q: What happens when a request names a model that is `ready` but not loaded? → A: The request waits by default, and the gateway sends keep-alive comments on the stream so intermediaries do not time out while the engine loads. A caller that opts out of waiting receives `503` with `Retry-After` set from the model's estimated warm-up time. Waiting is the default because a cold large model takes minutes and most clients would otherwise fail.
- Q: How does an unknown or unentitled model id behave? → A: An id that is not in the registry is `404`. A model that exists but is unpublished, not `ready`, or not entitled to the caller is also `404` for a user — existence is not disclosed. For an admin the same id resolves normally, which is what makes testing an unpublished model possible.
- Q: What guarantees a caller's string never reaches an engine? → A: The gateway resolves the string to a registry id and passes only the registry-resolved local path to the engine. Any request whose model field does not match a registry id is rejected before an engine is contacted, and the adapter fields the engine accepts for model and adapter selection are never populated from caller input.
- Q: Which OpenAI surface is in scope here? → A: `chat/completions` streaming and non-streaming, and `models`. Completions, embeddings, and images are out of scope: images arrive with feature 015 and embeddings are backlog. The Anthropic `messages` adapter is in scope because it is the cheap win that makes Claude Code work against the platform.
- Q: How is usage captured for a streamed response that the client abandons? → A: Usage is recorded from tokens actually produced, and a disconnect is recorded as a completed-with-disconnect outcome rather than discarded. Abandoned streams still consumed engine time and must be attributable for later budgeting.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Existing OpenAI-SDK tooling works unchanged (Priority: P1)

A developer points an OpenAI SDK client, or an editor integration, at Coire's base URL and it works without code changes: models list, chat completions stream, and token usage comes back.

**Why this priority**: Compatibility is the whole product surface for programmatic users; a bespoke API would strand every tool they already use.

**Independent Test**: Run an unmodified OpenAI SDK against the gateway, list models, and stream a completion.

**Acceptance Scenarios**:

1. **Given** a valid credential and a published, ready model, **When** an OpenAI SDK client streams a chat completion, **Then** tokens arrive incrementally in the expected wire format and the stream terminates correctly.
2. **Given** the same client, **When** it lists models, **Then** it receives only published, ready models the caller is entitled to, each carrying Coire's load state, tags, and description alongside the standard fields.
3. **Given** a non-streaming request, **When** it completes, **Then** the response includes prompt and completion token counts.
4. **Given** an editor integration configured against the base URL, **When** it is used normally, **Then** it functions without modification.

---

### User Story 2 - Anthropic-SDK tooling works by changing a base URL (Priority: P1)

A developer sets an Anthropic-SDK tool's base URL to Coire and it works against local models, streaming included.

**Why this priority**: The roadmap names this explicitly as an acceptance bar, and it is what makes the platform useful from Claude Code and similar tools at near-zero cost.

**Independent Test**: Point an Anthropic-SDK client at the gateway and complete a streamed exchange.

**Acceptance Scenarios**:

1. **Given** an Anthropic-SDK client pointed at Coire, **When** it sends a messages request, **Then** the adapter translates it to the backend and returns a correctly-shaped Anthropic response.
2. **Given** the same client streaming, **When** tokens are produced, **Then** they arrive as the Anthropic event sequence the SDK expects.
3. **Given** a request using system prompts and multi-turn history, **When** it is adapted, **Then** the rendered prompt preserves roles and ordering.

---

### User Story 3 - A cold model does not look like a failure (Priority: P1)

A caller requests a model that is not loaded. Rather than an error or a silent hang, the request waits with the connection kept alive, or returns a retry-after response if the caller asked not to wait.

**Why this priority**: Loading a large model takes minutes. Without this the common first request of any session looks like an outage.

**Independent Test**: Request a cold model and confirm the connection survives the full load and then streams.

**Acceptance Scenarios**:

1. **Given** a `ready` but unloaded model, **When** a caller requests it with the default wait behaviour, **Then** the connection is held with keep-alive traffic and the response streams once the engine is ready.
2. **Given** the same, **When** the caller has opted out of waiting, **Then** the response is `503` with `Retry-After` set from the estimated warm-up time.
3. **Given** a cold model whose load fails, **When** the failure occurs, **Then** the caller receives an error identifying the load failure rather than a timeout.
4. **Given** a wait that exceeds the configured ceiling, **When** the ceiling is reached, **Then** the caller receives `503` with `Retry-After` rather than waiting indefinitely.

---

### User Story 4 - Unknown and unentitled models are refused safely (Priority: P1)

A caller names a model id that does not exist, is unpublished, or is not theirs to use, and is refused without learning whether it exists.

**Why this priority**: This is the constitutional boundary of Principle V. It is also the control that keeps caller strings away from engines.

**Independent Test**: Request an unpublished model as a user and as an admin, and confirm 404 and success respectively.

**Acceptance Scenarios**:

1. **Given** a model id absent from the registry, **When** a caller requests it, **Then** the response is `404` and no engine is contacted.
2. **Given** an unpublished model, **When** a user requests it, **Then** the response is `404`; **When** an admin requests the same id, **Then** it resolves normally.
3. **Given** a model the caller is not entitled to, **When** it is requested, **Then** the response is `404` rather than `403`, so existence is not disclosed.
4. **Given** any request, **When** it is proxied, **Then** the engine receives only a registry-resolved path, never the caller's string.

---

### User Story 5 - Every request is attributable (Priority: P2)

An operator can see what was spent, by whom, on which model, including for streams that were abandoned mid-response.

**Why this priority**: Usage capture is the input to budgets and rate limits in feature 007 and to the Traffic dashboard in feature 009. It must be collected from the first request, but nothing in Phase 1 blocks on reading it.

**Independent Test**: Issue several requests including one abandoned mid-stream and confirm each produced a usage record with correct attribution.

**Acceptance Scenarios**:

1. **Given** a completed request, **When** it finishes, **Then** a usage record captures caller, model, prompt tokens, completion tokens, and duration.
2. **Given** a stream abandoned by the client, **When** the disconnect happens, **Then** usage is recorded from tokens actually produced and marked as disconnected.
3. **Given** a request that failed at the engine, **When** it fails, **Then** the outcome is recorded with the failure reason.

---

### Edge Cases

- The engine dies mid-stream: the caller MUST receive a terminated stream with an error rather than a silently truncated response that looks complete.
- The caller disconnects mid-stream: the gateway MUST stop consuming from the engine promptly rather than continuing to generate for nobody.
- More requests arrive for one instance than it can serve: in-flight requests per instance MUST be capped at the gateway, with excess either queued or refused with `Retry-After` — never passed through to overwhelm the engine.
- A request arrives while its model is being unloaded: it MUST NOT be routed into a dying process; it MUST wait for a fresh load or receive `Retry-After`.
- An intermediary imposes an idle timeout shorter than a model's load time: keep-alive traffic MUST be frequent enough to prevent that timeout.
- A request names a model with an adapter suffix before adapters exist: it MUST be rejected as an unknown id rather than partially parsed.
- A malformed request body MUST be rejected with a validation error before any registry or engine work.
- Extremely long prompts exceeding the model's context: the caller MUST receive a clear context-length error naming the limit and the prompt's size.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The gateway MUST expose OpenAI-compatible `chat/completions` supporting streaming and non-streaming, and `models`.
- **FR-002**: The gateway MUST expose an Anthropic-compatible messages endpoint that adapts requests and responses, including streaming events, onto the same backend.
- **FR-003**: The models listing MUST return only published, `ready` models the caller is entitled to, and MUST include Coire extensions for load state, tags, description, and context size.
- **FR-004**: Coire-specific fields MUST be additive to the compatible surface and MUST be namespaced so standard clients ignore them.
- **FR-005**: The gateway MUST resolve every requested model id against the registry, and MUST reject any id that does not resolve before contacting an engine.
- **FR-006**: The gateway MUST pass only registry-resolved local paths to an engine, and MUST never populate engine model or adapter fields from caller input.
- **FR-007**: An id that is absent, unpublished, not `ready`, or not entitled MUST return `404` to a non-admin caller, without disclosing existence.
- **FR-008**: An admin caller MUST be able to resolve unpublished models for testing.
- **FR-009**: A request for a `ready` but unloaded model MUST by default wait for the load, with keep-alive traffic on the stream sufficient to defeat intermediary idle timeouts.
- **FR-010**: A caller MUST be able to opt out of waiting and receive `503` with `Retry-After` derived from estimated warm-up time.
- **FR-011**: Waiting MUST be bounded by a configured ceiling, after which the caller receives `503` with `Retry-After`.
- **FR-012**: The gateway MUST cap in-flight requests per instance and MUST NOT pass excess load through to an engine.
- **FR-013**: The gateway MUST record a usage entry for every request, including caller, model, token counts, duration, and outcome.
- **FR-014**: Usage MUST be recorded for abandoned streams from tokens actually produced, marked as disconnected.
- **FR-015**: A client disconnect MUST cause the gateway to stop consuming from the engine promptly.
- **FR-016**: An engine failure mid-stream MUST terminate the client stream with an explicit error rather than a truncated success.
- **FR-017**: Requests MUST NOT be routed to an instance that is draining or unloading.
- **FR-018**: A prompt exceeding the model's context window MUST return an explicit context-length error naming the limit.
- **FR-019**: The gateway MUST be the only component that talks to an engine; engines MUST remain unreachable from anywhere else.
- **FR-020**: Every request MUST be traced end to end so latency is attributable across gateway, queueing, load, and generation.

### Key Entities

- **Model Resolution**: The mapping from a caller's string to a registry record. Requested id, resolved model and variant, entitlement verdict, resolved local path.
- **Usage Record**: One request's accounting. Caller, credential, model, prompt and completion tokens, duration, outcome, disconnect flag, timestamp.
- **Request Context**: Per-request state. Wait preference, deadline, trace identity, in-flight slot, target instance.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: An unmodified OpenAI SDK completes a streamed chat against Coire, and an unmodified Anthropic SDK does the same by changing only a base URL.
- **SC-002**: An editor integration and an Anthropic-SDK CLI tool both work against the platform without modification.
- **SC-003**: Gateway overhead is at most 20 ms at the 95th percentile, excluding model time.
- **SC-004**: First token for a loaded single-node model arrives within 1.5 s at the 95th percentile for prompts of 4k tokens or fewer.
- **SC-005**: A request for a cold model holds its connection through the entire load and then streams, with no intermediary timeout, for loads up to the configured ceiling.
- **SC-006**: An unpublished model id returns 404 to a user and resolves for an admin, in 100% of trials.
- **SC-007**: No caller-supplied string ever reaches an engine, verified by inspection of engine-bound requests across the test suite.
- **SC-008**: 100% of requests, including abandoned streams and engine failures, produce an attributable usage record.

## Assumptions

- Features 001 and 002 have shipped: models can reach `ready`, and the node agent can load, health-check, and unload an engine.
- Placement and eviction policy is feature 004; this feature asks for a load and waits, but does not decide what to evict.
- The instance state machine and multi-instance routing are feature 005. Until then the gateway routes to at most one instance per model.
- Authentication is feature 007. This feature depends on a caller identity and an entitlement check being available, and hardens later without changing routing rules.
- Rate limits and budgets are feature 007; usage capture here is the data those will consume.
- Streaming passes through the tunnel and nginx, so response buffering must be disabled on these paths and read timeouts must be long — established in feature 000.
- Image and embedding endpoints are out of scope; images arrive with feature 015.
- Adapter-suffixed model ids are not valid until feature 016 introduces them.
- Integration tests run against a model of 1 GB or less so CI can run on a single Mac.
