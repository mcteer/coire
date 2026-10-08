# Admin Evaluation API Contract

Design contract, not generated OpenAPI. Implement Pydantic wire types first; generate OpenAPI/TypeScript from the actual routes. All routes use authenticated `CurrentAdmin`; create/measure/rerun additionally require an active human admin owner (human-owned admin API keys work). Internal workers cannot call admin mutation routes. Normal user tokens receive 403; unrecognized target IDs are non-disclosing 404.

## Endpoints

| Method/path | Request → response | Semantics |
|---|---|---|
| GET `/api/v1/admin/evaluation-suite-templates` | → bounded template page | Supported installed template metadata and digests; no executable client content. |
| POST `/api/v1/admin/evaluation-suites` | `EvaluationSuiteRegistration` → 201 `EvaluationSuite` | Register immutable slug/version from installed template/version; bind exact judge when needed. Idempotency-Key required; change needs new version. |
| GET `/api/v1/admin/evaluation-suites` | cursor/filter → suite page | Limit 1–100, opaque cursor, includes retired flag. |
| GET `/api/v1/admin/evaluation-suites/{suite_id}/versions/{version}` | → suite | Historical immutable definition. |
| POST `/api/v1/admin/evaluation-suites/{suite_id}/versions/{version}/retire` | expected version → suite | Stop new admission; retain history. |
| POST `/api/v1/admin/evaluations` | `EvaluationSubmission` → 202 receipt | One suite and one/two registry subject references, optional registered training-data context; freeze targets/config, reserve bounded queue/evidence quota. |
| GET `/api/v1/admin/evaluations` | model/variant/adapter/job/group/state/cursor → page | Stable keyset order `(created_at,id)`, ≤100 items. |
| GET `/api/v1/admin/evaluations/{id}` | → `EvaluationRunDetail` | State, phase, deadlines, cleanup, result/provenance/evidence availability. |
| GET `/api/v1/admin/evaluations/{id}/events` | Last-Event-ID → SSE | Typed snapshot/state/progress/terminal/reset; no raw case text. |
| POST `/api/v1/admin/evaluations/{id}/cancel` | expected_version → 202 receipt | Audited cancel with credential revocation; idempotent replay, stale version 409. |
| POST `/api/v1/admin/evaluations/{id}/rerun` | `EvaluationRerunRequest` → 202 receipt | New ID/key, original suite version and subjects revalidated; expired/deleted inputs require an explicit new submission. |
| GET `/api/v1/admin/evaluations/{id}/evidence/{evidence_id}` | → bounded private bytes | Admin authorization on each access; 410 on expiry; no reusable public URL. |
| GET `/api/v1/admin/evaluation-groups/{id}` | → `EvaluationGroupDetail` | Training boundary and all suite/base/candidate states/results. |
| GET `/api/v1/admin/evaluation-groups/{id}/events` | Last-Event-ID → SSE | Durable group progress independent of training terminal stream. |
| GET `/api/v1/admin/evaluation-comparisons` | left/right result+subject references → comparison | Always return explicit compatibility reasons; no delta if incompatible or unsuccessful. Pairwise uses preference presentation. |
| POST `/api/v1/admin/evaluation-measurements` | `EvaluationMeasurementRequest` → 202 receipt | Explicit controlled baseline/mixed qualification using fixed suite workload and resident target set. |
| GET `/api/v1/admin/evaluation-measurements/{id}` | → measurement/report/profile | Freshness, counts, failures, profile fingerprint and expiry visible. |
| POST `/api/v1/admin/evaluation-measurements/{id}/cancel` | expected_version → 202 receipt | Same kill/cleanup guarantees as evaluation. |

Existing dataset analysis routes/CLI remain unchanged. Existing harness-evaluation GET target/list/detail remain readable. Legacy POST `/api/v1/admin/harness-evaluations` returns 409 `evaluation_execution_required`, with the submit route in problem metadata; it cannot accept raw scores or change verification. Existing legacy records/verification are retained with legacy provenance, not backfilled into invented executions.

## Request shapes and validation

`EvaluationSuiteRegistration`: `suite_id` slug ≤63, `version` integer ≥1, `template_id`, `template_version`, fixed allowed `generation` settings, timeout 60–1800 s, optional `judge` registry reference/settings. Generation is a fixed typed shape: temperature 0–2 (default 0), top_p >0–1 (default 1), seed uint32 (default 0), max_tokens 1–4096 (default 512), and at most four stop strings ≤128 characters. Unsupported settings are rejected. Catalog limits can be narrowed, never exceeded. Judge required for rubric/pairwise, forbidden otherwise. Rubric v1 fixes correctness, instruction adherence and clarity dimensions, each with versioned 0–4 anchors; callers cannot silently replace dimensions or scoring rules. Exact resolved targets/digests are server output, not accepted input. Frozen targets also bind the registry-selected bare engine backend. Historical text targets omit the default mlx_lm field, preserving their serialized hashes; visual targets explicitly serialize mlx_vlm. Runtime attestation checks the owned process backend and its recorded spawn package version (mlx-lm or mlx-vlm), rather than labeling visual inference with the text package version.

`EvaluationSubmission`: `suite_id`, `suite_version`, `subjects` (one or two `{model_id,variant_id,adapter_id?}` records), optional `{training_job_id}` contamination context. Harness accepts one, pairwise exactly two, task/rubric one or two. Two references must be distinct exact subjects. Known data context must belong to a visible training job with immutable resolved inputs; no file path, engine ID, arbitrary prompt, code, budget override or raw result is accepted. `Idempotency-Key` is required for mutations: header length 1–128, unique per admin and operation; same key/body replays, changed body conflicts. `EvaluationRerunRequest` uses `expected_version` and no hidden override; changed settings use new submission.

Receipt: evaluation ID, group ID, version, state, detail/events paths. Detail includes typed case IDs/scores and provenance but raw outputs are only in separate bounded evidence responses. Scores reject NaN/infinity; missing aggregate is null, never zero. Pages and cursor decoding are bounded and scope-checked. Suite retirement has its own mutable `registry_version` for optimistic locking, separate from immutable suite version.

Judge validation compares resolved candidate/judge model UUID and base manifest; same identity or artifact fails 409 `evaluation_self_judge` before creating execution resources. Bind aliases/variants/adapters before this comparison and repeat before phase launch. Current entitlements and owner role are rechecked on every launch and gateway request.

## Problems and audit

All new errors are `CoireError` subclasses serialized as RFC 9457. Statuses: 401 unauthenticated; 403 owner/admin/scope failure; 404 hidden/unknown subject; 409 stale version, same-key/different-body, immutable-suite conflict, self-judge or unsupported legacy execution; 410 expired evidence; 422 invalid schedule/bounds/subject count; 429 queue/evidence quota; 503 new admission disabled or unavailable compatible runtime. Persisted capacity wait is not an immediate 503 after acceptance. Responses never include engine stack traces or output content.

Audit actions cover suite.register/retire, evaluation.submit/rerun/cancel/refused, measurement.submit/cancel and automatic.trigger/resume decisions with requesting admin, credential, parent job, exact target IDs and reason. Reads use existing request/security logging, never mutate scores. Worker result collection validates run ownership/fence/content before calling the internal finalizer; no public “submit scores” endpoint is added.

## Bounds, deadlines and SSE

At most 100 pending runs and one active group by default. Absolute queue deadline is set on acceptance. Execution deadline is set once at first phase start and applies across all phases/retries; recovery cannot extend either. Overall run also cannot outlive queue timeout plus declared execution allowance. Judge presentation retries ≤2. Cancellation revokes tokens before waiting for node acknowledgment; resource release waits for observation.

SSE event ID is the durable integer sequence scoped to the requested stream. Reconnect uses Last-Event-ID; history outside the replay window emits reset with full current snapshot. Terminal result digests remain stable. Redaction and credential revalidation follow existing stream conventions. No event invents score/progress from log text.
