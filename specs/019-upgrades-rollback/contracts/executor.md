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
| `POST /api/v1/internal/upgrades/{job_id}/smoke` | `UpgradeSmokeRequest` / `UpgradeSmokeResult` | `gateway` verifies accepted Target; `source` verifies restored Source. Resolve the fixed registry asset server-side and account it normally under the operation's maintenance exemption; never infer the selected release from process-local client state. |
| `GET /api/v1/internal/upgrades/{job_id}/schema` | `UpgradeSchemaObservation` | Read-only current Alembic head and bounded structural digest for the executor's accepted operation; no row data or definitions. Observations older than five seconds are refused. |
| `POST /api/v1/internal/upgrades/{job_id}/ready-to-activate` | `UpgradeActivationRequest` / `UpgradeActivationGrant` | Revalidate live initiating actor, complete drain, source digest and fence before each irreversible boundary: migration and first runtime replacement use separate single-use, phase-bound grants that expire in 30 seconds. |
| `POST /api/v1/internal/upgrades/{job_id}/consume-activation` | `UpgradeActivationConsume` / `UpgradeReceiptAck` | Atomically consume the matching grant and persist its exact migration/runtime intent after another live actor/fence/source check. Ordinary receipt ingestion cannot authorize activation. |

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

Target publication follows an acknowledged `committing` receipt with `outcome=complete`, bound to the prior verified serving evidence. The API serializes this decision with cancellation and revalidates the initiating actor. Before that decision cancellation may win; afterward `cancellation_allowed=false` disables cancellation while finalization continues. The executor publishes matching runtime, host-selection and committed ownership receipts before reporting success. The API independently requires these matching durable receipts before accepting `succeeded`. After the acknowledged commit decision, failed or partially published selection remains fenced for publication reconciliation; a missing API response cannot authorize Source rollback.

Runtime activation after migration rechecks both the actual Alembic head and structural schema digest against the exact completed migration proof. The head alone is insufficient. Read observations retain their original transport/SQL start time, and reconnection does not renew readiness, outage or recovery deadlines.

## Bootstrap and recovery

The initial installer creates the executor's private volume and required service through the trusted deployment path, verifies it, and only then advertises managed-upgrade readiness. Interruption before readiness leaves this feature disabled. No source executor exists to restore on first installation.

For a later executor update, the installer records a typed handover while the incumbent stays alive with its own restart policy. Candidate starts passive, verifies the journal/manifest protocol, then receives the sole-writer epoch through the durable handover. Incumbent supervises the 120-second stability interval; on failure it restores source ownership/image, including after host reboot. At no point may the process being replaced be its only recovery owner. Handover blocks application upgrades until committed; the retiring incumbent is stopped only after candidate recovery ownership has been proved.

The authenticated executor service identity remains stable across image handover.
`DeploymentOwnership.owner_identity` and the kernel lease identify that service;
`executor_runtime_id` and `executor_container_id` separately bind its bootstrapped
incarnation. Handover intent fixes both independent full container IDs before the
first observation. A consumed application operation cannot replace or drop that
incarnation binding. Changing images is the supervised handover protocol, and
does not rotate the executor credential or reinterpret its API principal.

Initial Source verification uses the trusted prepared release directly, without
inventing an accepted application plan. `BootstrapSourceObservation` records all
declared Source container/image identities after exact frozen-configuration and
health checks, using the oldest original read time with a five-second bound.
This observation alone does not grant sole-writer ownership or advertise readiness.

The incumbent retains original physical read times in `BootstrapStabilityObservation`.
Each observation binds both full container IDs, exact prepared image/configuration,
candidate process start time, current kernel boot identity and the held candidate
writer lease. The observation transport allows only GETs for these two containers
and their pinned images. Missing intervals longer than ten seconds, replaced
containers, restarted candidates and changed kernels enter restoration without
renewing the captured interval. Both stability and commit require a fresh
observation; wall-clock elapsed time alone cannot authorize either transition.

A corrupt journal, missing source, API/schema uncertainty or failed restoration keeps maintenance active and readiness false. Operator acknowledgment does not itself permit releasing the fence. Recovery tooling exposes fixed inspect/reconcile/restore-source operations, never a remote shell.

## Prepared artifact consistency

A Core prepared catalog contains a private `compose.json` artifact whose full content hash
must equal both its artifact-map digest and the manifest's `compose_sha256`. Its project
name and every present fixed-cohort service must exactly match the registered service map;
each service configuration and immutable image ID must agree. Hash-valid but mismatched
service metadata is refused before publication/registration. Docker platform, actual source
container/mount identity, supported configuration and bootstrap checks remain additional
activation prerequisites; filesystem consistency alone is not activation authority.

The restricted `POST /api/v1/internal/upgrades/bootstrap/handover-claim` accepts the
incumbent `CoreBootstrapSeed` and returns the exact accepted
`CoreHandoverPreparation` or `UpgradeClaimIdle`. It rechecks fresh retained bootstrap
qualification and live initiating actor state before passive preparation. It is
separate from application-plan claims and grants no writer transfer. The original
preparation deadline is retained. Passive creation requires the matching durable
`DeploymentHandoverHold`; host deployment refuses that hold across kernel-writer
gaps. Orphan hold closure requires serialized SQL authority absence and no creation
intent; it cannot stand in for restoration of an accepted/dispatched handover.

`POST /api/v1/internal/upgrades/bootstrap/handover-transfer` consumes one
`CoreHandoverTransferRequest` after live initiating-actor revalidation and fresh
held-incumbent/passive-candidate verification. Its immutable
`CoreHandoverTransferIntent` retains the original at-most-thirty-second transfer
window, clipped to the passive preparation deadline. Exact replay returns that
original intent, including after expiry; an expired reply permits reconciliation
only. Actual kernel ownership remains a separate boundary.

Independent Source restoration captures one `CoreHandoverRecoveryIntent` with
the original failure-detection time and five-minute deadline. The sole permitted
Docker effect stops its exact recorded candidate. A lost response is reconciled
through full sealed-configuration inspection and positive stopped state; it never
permits blind redispatch or treats absence as cleanup. Incumbent reclamation
requires the genuinely free kernel, another positive stopped observation, fresh
whole-Source health and a new ownership generation. Maintenance remains held.

`POST /api/v1/internal/upgrades/bootstrap/handover-restored` accepts
`CoreHandoverRestoreRequest` and returns the bounded `CoreHandoverSummary`.
It verifies the exact private preparation, candidate seed and recovery intent,
fresh original stopped observation, and held incumbent writer/lease before
atomically retaining its fresh bootstrap heartbeat and closing the SQL handover.
Necessary restoration survives initiating-human revocation. Closing SQL does
not itself release the filesystem deployment hold or select an image.

`POST /api/v1/internal/upgrades/bootstrap/handover-commit` accepts a
`CoreHandoverCommitRequest` with original independent stability evidence and
returns one immutable `CoreHandoverCommitIntent`. The private handover must be
stable, its exact candidate process unchanged, its last physical observation
fresh, and the exact candidate kernel writer held. Its retained kernel lease
must have been captured inside the original consumed transfer window and before
stability began. Continuous stability alone leaves the local journal stable;
it does not imply a durable API commit decision. The SQL decision retains the
active slot in `committing`; changing selected incarnation, accepting its fresh
bootstrap heartbeat, final acknowledgment and incumbent retirement remain
separate steps. Exact retries preserve the original decision and audit entry.
Superseded/restored decisions cannot be replayed as active selection permission.

Selected-incarnation bootstrap qualification is separate from application Source
qualification. It requires the accepted pending commit, exact local decision/proof,
selected candidate seed, original process and original still-held transfer lease.
It cannot change the application Source or cohort identities. The ordinary
bootstrap route cannot restore another executor incarnation; only the internal
restoration acknowledgment may request that transition, with independently
verified positive candidate stop and recovered Source writer.

`POST /api/v1/internal/upgrades/bootstrap/handover-committed` accepts
`CoreHandoverCommittedRequest`, rechecks the fresh held selected candidate and
fresh healthy retained incumbent, atomically qualifies its bootstrap incarnation,
and closes SQL authority with one audit entry. Exact terminal replay retains its
original bounded summary. Local finalization retains this acknowledgment before
recording committed state and releasing the exact handover hold under the held
candidate kernel. Interrupted hold release keeps acknowledgment/selection for
replay. This step does not stop or delete the retained incumbent; its retirement
is separately supervised after acknowledged readiness.

Before any recovery stop, `POST /api/v1/internal/upgrades/bootstrap/handover-restoring`
accepts the retained `CoreHandoverRecoveryRequest` and serializes a restoration
decision against final commit under the same SQL operation lock. A committed
selection refuses restoration; an unavailable response grants no stop permission.
Exact retries keep the private intent's original five-minute deadline and one
audit entry. This prevents a status-read/final-commit race from stopping an
already committed candidate. Restricted status reads at
`GET /api/v1/internal/upgrades/bootstrap/handover/{preparation_sha256}` are scoped
to the configured project and one bound preparation; they grant no effects.

Candidate startup exposes only passive protocol until the fsynced consumed
transfer exists. The incumbent then completes its real kernel release; candidate
capture is bounded by the original transfer deadline. Incumbent stability probes
preserve the captured 120-second epoch and refuse a restarted incumbent. Commit
proof is fsynced before API dispatch and replayed unchanged. A candidate already
committed in SQL may capture a fresh bootstrap generation after restart, but
must qualify that generation through the API before reconciling an interrupted
private hold release. This does not resume transfer or stability.

Retirement verifies SQL/private acknowledgment, the released exact handover hold,
the selected candidate's actual held idle writer, and authenticated `/ready` at
its exact Docker-attested private address. It rechecks process/address and writer
before dispatch. Only the full retained predecessor container ID may be stopped;
no container/image deletion occurs. `CoreHandoverRetirementIntent` is fsynced before
the one stop dispatch. Lost/uncertain responses never authorize blind redispatch.
The stopped container and its image remain available for explicit rollback.

The restricted `POST /bootstrap/handover-recovery-claim` reads original pending
no-transfer preparation metadata for unchanged Source cleanup. It requires a fresh
qualified exact incumbent, actual held idle writer and the matching private
admission hold. It does not reauthorize passive preparation, start, transfer or
renew any deadline; it does not mutate SQL phase. Metadata may remain readable
after original expiry or initiating-actor revocation so necessary cleanup is not
stranded. Before using it, the executor durably prohibits all candidate effects
and proves no create/start was dispatched, then uses the existing unchanged
acknowledgment. Dispatched candidate history uses the independent restoration
path instead.

A restart before an accepted operation or handover was claimed may rebuild only
the same effect-free Source's bootstrap proof under that exact original hold.
The admission verifier runs before and after actual kernel capture. A new writer
generation invalidates the old candidate transfer epoch; pre-create handover
failure is then closed as unchanged rather than starting a candidate under a
renewed epoch. Prior completed generation acknowledgments remain retained.
