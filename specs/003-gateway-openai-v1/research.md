# Research: Gateway and OpenAI-Compatible /v1

## R1 — Compatibility boundary

**Decision**: Implement the documented Chat Completions subset (`models`, streaming and
non-streaming `chat/completions`) and Anthropic Messages at typed route boundaries. Preserve
compatible optional generation fields and reject unsupported behavior explicitly.

**Rationale**: Official SDKs depend on wire shape and SSE termination, not an internal SDK. OpenAI
documents SSE streaming and additive response compatibility; Anthropic documents an ordered event
sequence. Coire can test those boundaries without adding either SDK to production.

**Alternatives considered**: Proxy untyped JSON (violates Principle III); implement Responses API
(not in scope); depend on production OpenAI/Anthropic SDKs (unnecessary runtime weight).

References: https://platform.openai.com/docs/api-reference/chat and
https://docs.anthropic.com/en/api/messages-streaming

## R2 — Provisional identity and entitlement

**Decision**: Use the existing `CurrentPrincipal` seam. Admin sees unpublished models; non-admin
sees only `published`, `ready`, and either unentitled models or models whose entitlement labels are
present in principal scopes. Feature 007 replaces credential resolution without changing policy.

**Rationale**: The spec precedes feature 007 but requires the authorization decision to exist.
Centralizing it on `Principal` avoids a permissive route-local placeholder.

**Alternatives considered**: Wait for feature 007; duplicate API-key tables early; treat every caller
as admin (violates Principles IV and V).

## R3 — Model resolution and engine selection

**Decision**: Accept only the registry UUID exposed by `/v1/models`. Resolve the model, verified
copy, node, and a non-draining READY engine from database state. If cold, call the existing audited
load service with the resolved model row; never forward request `model` to MLX. Until feature 005
there is at most one live instance selected per model.

**Rationale**: This preserves registry lifecycle and makes caller-string isolation testable while
leaving placement/multi-instance decisions behind explicit later seams.

**Alternatives considered**: Accept slug/repository ids; send caller model through to MLX; create a
scheduler in the gateway.

## R4 — Streaming, keep-alives, and cancellation

**Decision**: Use one cancellation-aware `httpx.AsyncClient.stream` per request. Forward engine SSE
incrementally through FastAPI `StreamingResponse`, translate chunks for Anthropic, send comment
keep-alives only while a cold model loads, and close upstream on client cancellation. Engine EOF
without a protocol terminator becomes an explicit terminal error event and failed usage.

**Rationale**: It bounds memory, prevents generation for disconnected clients, and preserves SDK
framing. Nginx buffering stays disabled and upstream read timeout exceeds the wait ceiling.

**Alternatives considered**: Buffer complete responses; background consumer queues; WebSockets.

## R5 — Cold-load coordination and overload

**Decision**: A per-model async coordinator deduplicates concurrent load requests. Default streaming
requests receive comment keep-alives while polling engine state; explicit
`coire_wait_for_model=false` returns 503 with integer `Retry-After`. A configurable semaphore caps
each engine; saturation returns 429 with `Retry-After` rather than an unbounded queue.

**Rationale**: This meets user-visible semantics without implementing feature 004 placement.

**Alternatives considered**: Unbounded queues; pass concurrency directly to MLX; durable queueing.

## R6 — Usage accounting

**Decision**: Insert one append-only usage row in a shielded finalizer for every accepted inference
request. Prefer engine-reported usage; for streams accumulate the latest usage chunk. Record zero or
known partial counts on failure/disconnect, duration, and outcome. Do not store prompt/output.

**Rationale**: Durable attribution is needed before budgets exist, while avoiding sensitive content.

**Alternatives considered**: Logs only; require final usage chunks; store request content.

## R7 — Context length

**Decision**: Enforce a conservative preflight estimate using the model context window and a bounded
message-character estimator, then preserve engine-reported context errors as a 400 problem detail.
Exact tokenizer accounting is deferred until tokenizer service support exists.

**Rationale**: It prevents obviously oversized work without loading tokenizer artifacts on core.

**Alternatives considered**: Tokenize on core; trust only engine failure; claim character counts are
exact tokens.

## R8 — Compatibility test dependencies

The official `openai` and `anthropic` Python packages are development-only compatibility clients;
neither enters the `coire-api` production dependency set or image. Both are MIT licensed. The web
schema generator `openapi-typescript` is also development-only and MIT licensed. Exact resolved
versions and transitive artifacts are pinned by `uv.lock` and `pnpm-lock.yaml`.
