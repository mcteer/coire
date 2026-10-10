# Core Executor and Deployment Contract

## Placement and permissions

Introduce one `coire-upgrade-executor` service with its own Python-only distroless/Chainguard image, non-root user, read-only rootfs, all capabilities dropped, no-new-privileges, bounded memory/PIDs/logs, healthcheck and restart policy. It runs one process and exposes no public listening port. Healthcheck reads private local readiness state. Its image is outside its managed application cohort.

**Concrete network change requiring authorization before implementation:** attach only the new executor to existing `coire-docker` (proxy access), `coire-internal` (authenticated API claim/receipt traffic) and `coire-telemetry` (local observability). Do not attach it to `coire-db` or `coire-edge`. No other service gains access, and no proxy flag, firewall rule, CORS policy or Linux capability is widened. The executor never receives the raw Docker socket.

Mount frozen prepared manifests read-only, a dedicated coordination directory read-write, and a private journal volume. Mount only its own Keychain-sourced API credential as a Compose secret. Do not mount other services' secret contents or the entire state root. Existing frozen manifests can contain bind-source names; the executor uses them solely after source-container/config comparison.

## Authenticated control protocol

All payloads are coire-core Pydantic models. The executor's credential grants only the internal upgrade role, project and executor identity, with no human-admin or general workload authority.

| Route | Request / response | Semantics |
|---|---|---|
| `POST /api/v1/internal/upgrades/claim` | `UpgradeClaimRequest` / `UpgradeExecutionPlan` or idle response | Claim one accepted authorized operation; bind executor ID, project and observed source; no caller-selected work. |
| `POST /api/v1/internal/upgrades/{job_id}/receipts` | `UpgradeReceiptBatch` / `UpgradeReceiptAck` | Deduplicate ordered receipts; reject another job/generation or conflicting sequence. |
| `POST /api/v1/internal/upgrades/{job_id}/smoke` | `UpgradeSmokeRequest` / `UpgradeSmokeResult` | Only the accepted post-rollout smoke; resolve registry asset server-side and account it normally under the operation's maintenance exemption. |
| `POST /api/v1/internal/upgrades/{job_id}/ready-to-activate` | `UpgradeActivationRequest` / `UpgradeActivationGrant` | Revalidate live initiating actor, complete drain, source digest and fence before each irreversible boundary: migration and first runtime replacement use separate single-use, phase-bound grants that expire in 30 seconds. |

Claims expire before first activation if not renewed/used within their captured deadline. The atomic authorization-and-intent transaction is the logical activation boundary; it rejects an actor revoked before that boundary. A journaled activation grant authorizes only the fixed bounded execution/recovery path; revocation or cancellation after activation causes restoration. Executor loss never automatically transfers ownership based only on API lease expiry. Reconcile container identity and local journal before granting a replacement owner.

The API may be down after activation. The executor uses its accepted journal to finish checks or restore only the recorded source. It cannot expand the cohort, renew authority to start a new operation, or invent a target offline. Events and audits are uploaded after API recovery; receipt acknowledgment requires durable persistence.

## Fixed Docker boundary

Permit only exact versioned Engine API operations needed by the accepted plan:

- `GET /containers/{id}/json`, bounded `GET /containers/{id}/logs`, and filtered container listing scoped and revalidated by project/service labels.
- `GET /images/{digest}/json` for already present images.
- `POST /containers/create` from validated immutable service metadata; deterministic job/generation/service name and exact labels.
- `POST /containers/{id}/start` and `/stop`, with bounded graceful timeout after positive drain.
- `DELETE /containers/{id}` only for owned candidates or committed retired sources, without force or volume deletion.

Explicitly deny exec creation/start, arbitrary API paths, images/create/pull/build, prune, volume/network CRUD, arbitrary mounts, privileges, host networking/PID mode, environment overrides, other projects and unknown service roles. The proxy's broad prefix permission is not sufficient authorization.

The supported Compose subset includes environment, entrypoint/command arrays, explicit user, restart/stop policies, resource limits, read-only rootfs, tmpfs, dropped capabilities, no-new-privileges, logging, healthcheck, exact published ports, known network attachments/aliases, verified existing named volumes and frozen file-secret binds. Reject unsupported keys before stopping a service. Resolve `depends_on` explicitly; never silently ignore it. `coire-migrate` must exit zero, not merely report running/healthy.

Preserve existing mounts by comparing actual source containers and prepared metadata. Named volumes use no-copy and exact pre-existing identities; retain source containers while candidates are created to prevent reference loss. Bind mounts use structured no-create semantics. A missing source mount, network or image blocks. No topology-changing release is eligible for this rollout protocol.

## Deployment ownership and selection

Host `coire-up/down`, bootstrap, cleanup and executor use the typed ownership protocol in data-model.md. Host commands check actual executor container identity through existing administrative Docker access, not host PID rules. Executor mutations and host commands exclude each other. Owner death initiates reconciliation, not blind stale-lock deletion.

Executor commits a fsynced selected-release receipt in its narrow writable coordination directory. Before any later host deploy/down/prune action, the host reconciles that receipt and updates `current` atomically. Frozen source and selected manifests remain source-independent even if the source checkout is removed. Secrets are neither regenerated nor pruned while referenced by current, retained or active operations.

## Bootstrap and recovery

The initial installer creates the executor's private volume and required service through the trusted deployment path, verifies it, and only then advertises managed-upgrade readiness. Interruption before readiness leaves this feature disabled. No source executor exists to restore on first installation.

For a later executor update, the installer records a typed handover while the incumbent stays alive with its own restart policy. Candidate starts passive, verifies the journal/manifest protocol, then receives the sole-writer epoch through the durable handover. Incumbent supervises the 120-second stability interval; on failure it restores source ownership/image, including after host reboot. At no point may the process being replaced be its only recovery owner. Handover blocks application upgrades until committed; the retiring incumbent is stopped only after candidate recovery ownership has been proved.

A corrupt journal, missing source, API/schema uncertainty or failed restoration keeps maintenance active and readiness false. Operator acknowledgment does not itself permit releasing the fence. Recovery tooling exposes fixed inspect/reconcile/restore-source operations, never a remote shell.
