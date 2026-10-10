# Administrator API, CLI and Console Contracts

All payloads are declared in `coire-core/models/upgrades.py`; documentation is not hand-authored OpenAPI. Extend existing error, auth, console-capability and client contracts first, then regenerate `apps/coire-api/openapi.json` and `apps/coire-web/src/api/schema.d.ts`.

## Authentication and authorization

Use authenticated, live human-admin identity for every route below, including event streams. Ordinary user, harness/run and ops-service credentials cannot mutate upgrades. Revalidate initiating user/key state before first activation. Internal executor credentials cannot call these routes. Every accepted registration, start, cancellation and rollback writes audit in the same transaction as mutation; rejected security attempts use the existing security audit path. Reads never reveal credential material, backup paths, raw environment, model input or full logs.

## Routes

| Method and route | Request → response | Behavior |
|---|---|---|
| `GET /api/v1/admin/upgrades/releases` | cursor/kind → `UpgradeReleasePage` | Prepared immutable releases and compatibility/readiness summary; bounded pagination. |
| `POST /api/v1/admin/upgrades/releases` | `UpgradeReleaseRegister` → `UpgradeRelease` | Register an opaque prepared artifact ID plus its expected digest; resolve only trusted prepared stores. Manifest fields alone are not proof of preparation. |
| `GET /api/v1/admin/upgrades/targets` | none → `UpgradeTargetList` | Desired/observed/retained identity, drift, blockers and bootstrap readiness for both Studios and Core. |
| `POST /api/v1/admin/upgrades/nodes/{node_id}` | `UpgradeStartRequest` → `UpgradeOperation` | Accept one-node upgrade; target release ID and expected source digest; return 202 plus operation location. |
| `POST /api/v1/admin/upgrades/control-plane` | `UpgradeStartRequest` → `UpgradeOperation` | Accept fixed project/cohort upgrade after readiness/compatibility preflight; same 202 semantics. |
| `POST /api/v1/admin/upgrades/{job_id}/rollback` | `UpgradeRollbackRequest` → `UpgradeOperation` | Create a new operation to the successful operation's retained compatible source; reject arbitrary destinations and nonterminal parents. |
| `POST /api/v1/admin/upgrades/{job_id}/cancel` | `UpgradeCancelRequest` → `UpgradeOperation` | Idempotent cancellation, unchanged cleanup before activation or restoration afterward; never a force-kill parameter. |
| `GET /api/v1/admin/upgrades/{job_id}` | none → `UpgradeOperation` | Authoritative durable status, restoration result, blockers and selected event cursor. |
| `GET /api/v1/admin/upgrades/{job_id}/events` | `Last-Event-ID` or after cursor → `UpgradeEventPage` / SSE | Poll or stream; sequence replay and snapshot reset, revalidate access throughout. |

Start requests carry only release ID, expected source digest, an acknowledgment of the presented maintenance policy and an opaque idempotency key. Timings come from validated configuration captured in the operation, not arbitrary per-request shell/env overrides. Project comes from configured deployment identity, not user-controlled routing. Registry smoke model selection is server-side from configured verified eligible assets; administrators prepare missing assets through existing acquisition separately.

Same actor/key/request retries return the existing job (200 on replay); a changed body with the same key returns 409. Concurrent operation, source drift, incompatible schema/journal, unknown inventory, missing backup, unready bootstrap, absent artifacts or insufficient disk return structured 409 prerequisite problems before activation. Invalid shape is 422, unauthenticated 401, unauthorized 403, missing visible resource 404. Errors inherit CoireError and RFC 9457 problem details, with safe `coire_` extensions where needed. During maintenance, affected new workload admissions return 503 with `Retry-After: 5` and a maintenance reason while the ingress is reachable; upgrade status/cancel, authentication, lease completion, cleanup and recovery traffic stay available.

## Event semantics

Events include job/generation, monotonic sequence, phase, safe message/reason, occurred time, source/candidate identities and restoration status. Terminal events are retained with the operation summary. Repeated SSE frames deduplicate by sequence; reconnect resumes, never resubmits a mutation. Expired cursors yield a full current snapshot and a new cursor. Batch pages are capped at 100 events; each safe evidence excerpt is capped at 8 KiB, total diagnostic evidence at 256 KiB per operation. Raw secrets, prompts, feedback or datasets are never included. Additional private diagnostics follow bounded existing storage policies.

## CLI

Add `coire upgrades releases`, `versions`, `node NODE --release RELEASE`, `control-plane --release RELEASE`, `status JOB [--watch]`, `rollback JOB`, and `cancel JOB`. Commands use the shared SDK and authenticated admin API; they cannot execute Docker, SSH or install packages. Provide `--json` output from the same wire models. Starting commands require the displayed maintenance acknowledgment and persist/reuse an idempotency key on request retry. Watch reconnects after temporary API unavailability and exits nonzero for failed/rolled-back outcomes, with the terminal job ID retained.

Artifact/backup preparation remains in the existing host deployment tool, with fixed `prepare-upgrade` and `backup-release` modes and private output stores. The CLI and console report missing preparation precisely; they cannot turn an untrusted path into a prepared release.

## Console

Add an admin upgrades capability and page under existing navigation. Use generated types and client functions in `src/api/`; use the shared `useEventStream`. Show source and candidate labels plus exact digests, maintenance effect, rollback eligibility and blockers before submission. Disable unavailable actions with explanations; duplicate clicks reuse the operation identity. The page keeps the job ID and reconnects after API/SSE outage. Display `rolled_back` distinctly from `succeeded`, and show recovery-required state with the runbook action.

Support loading/empty/error/offline states, keyboard operation, focus restoration and polite stage announcements. No raw internal mounts, tokens or implementation paths appear in product flows. Existing non-admin navigation and `/v1` contracts are preserved.
