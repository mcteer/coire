# Node Training and Artifact Protocol

All payloads below become strict Pydantic types in
`packages/coire-core/src/coire_core/models/training_node.py`. Control-plane requests use the
existing node authentication and declared control DNS. Peer bytes use the data listener and
`DataFabricClient` only. No new broad CORS/firewall/network permissions are authorized.

## Command envelope and lifecycle

`TrainingCommand`: schema version, command UUID, job ULID, attempt ULID, generation/fence,
exact immutable request digest, declared node/rank/world size and renewable execution lease.
Persist command intent before side effects. Same command+digest returns the same receipt; changed
digest returns 409. Lower fences and expired leases cannot start, renew or publish. Lease expiry
causes local stop/checkpoint attempt within the remaining deadline, never indefinite orphan work.

| Method/path (node control listener) | Contract / behavior |
| --- | --- |
| `POST /node/training/analyses` | `NodeDatasetAnalysisRequest` -> `NodeAnalysisReceipt`; registered tokenizer/data IDs, pinned manifests and bounded input grant, local CPU reservation; no model weights |
| `GET /node/training/analyses/{id}` | `NodeDatasetAnalysisStatus`; progress/stats/safe row diagnostics; restart-safe idempotent analysis |
| `POST /node/training/analyses/{id}/cancel` | `NodeAnalysisCancelRequest` -> stop receipt; termination proof before local hold release |
| `POST /node/training/attempts/{id}/prepare` | `TrainingPrepareRequest` -> `TrainingPrepared`; check config/assets, compatible runtime/capability, disk and local reservation, generated directories and rank launch identity; no training start |
| `POST /node/training/attempts/{id}/start` | `TrainingStartRequest` -> `TrainingStartReceipt`; only after scheduler authorizes all prepared participants; fixed worker/launcher argv, persistent spawn intent/PID/create-time |
| `GET /node/training/attempts/{id}` | `NodeTrainingStatus`; attempt/fence, actual liveness/identity, global update, footprint, checkpoint staging and current lease; unknown is distinct from stopped |
| `GET /node/training/attempts/{id}/events` | Cursor -> `NodeTrainingEventPage`; bounded safe worker observations for scheduler persistence; not public SSE |
| `POST /node/training/attempts/{id}/lease` | `TrainingLeaseRenewal` -> receipt; same scope/fence, later bounded expiry, no authority expansion |
| `POST /node/training/attempts/{id}/pause` | `TrainingPauseRequest` -> receipt; checkpoint at completed-update boundary; reason admin/guard/lease and deadline |
| `POST /node/training/attempts/{id}/stop` | `TrainingStopRequest` -> `TrainingStopReceipt`; stop whole owned group, TERM/KILL within deadline, return PID/create-time/attempt-matched death evidence |
| `POST /node/training/attempts/{id}/checkpoint-commit` | `CheckpointCommitAcknowledgement` -> receipt; scheduler reports committed common manifest; duplicate idempotent, differing manifest conflicts |
| `POST /node/training/reconcile` | `TrainingReconcileRequest` -> `TrainingReconcileResult`; expected attempts/process markers -> adopted/dead/unknown/orphan; never blind respawn or arbitrary PID kill |

Resolved start payload includes registry model/variant IDs and expected local manifest, canonical
dataset/split manifests, optimizer/parameterization settings, target module IDs, sampler/mask
version, expected resource envelope, checkpoint identity if resuming and lease. Paths, shell,
arbitrary argv, import strings, external URLs and HF credentials are not wire inputs. Node alone
maps authorized IDs to contained directories and the versioned interpreter. Extra fields fail.

The worker has a private authenticated control channel to its node supervisor; its credential
cannot call admin/gateway/node management endpoints. It receives no database, SSH or Hub credential.
Rank launching reuses the existing declared Studio JACCL mechanism and data-fabric hostfile; the
API/scheduler never launch processes or SSH. No engine port is exposed to clients.

Prepare is not a lease-free promise of capacity: control-plane and node local holds remain until
start/death/expiry reconciliation. A partial start immediately enters coordinated teardown. Each
participant reports reservations before start returns; unavailable node status never frees a hold.

Measurement preparation rejected before any attempt/directory/config exists records a private,
immutable no-start rejection binding the complete prepare scope. That tombstone permanently
refuses subsequent prepare/start for the attempt. The existing status/stop contracts may then
report stopped with no PID, after verifying the pristine namespace and exact job/attempt/fence/
node/rank/world-size/request digest. Stop command replay remains immutable. Missing/corrupt or
conflicting local evidence is unknown, not no-start proof. The scheduler can rebind the original
prepare after a stop conflict, then retry the same stop command within its five-second lane;
neither HTTP refusal nor unreachable transport releases any hold by itself.

No-start proof additionally requires a complete current candidate-process inventory showing no
attempt marker and no attempt-owned artifact namespace. A surviving worker with missing local
state, inaccessible candidate process or unexpected bytes invalidates the proof, including on
replay after a previously successful receipt. Verified native platform OS processes use the same
kernel identity classification as admission; all other unobservable processes retain uncertainty.

Measurement checkpoint hooks serialize actual evaluated state for resource evidence only. They
cannot emit a durable staged checkpoint or manufacture a common-bundle commit, and cannot be
combined with normal durable checkpoint coordination or resume state. Ordinary training keeps its
fenced two-copy commit path. Core stores scope-matched stop receipts in measurement command metadata
and marks completed measurement commands succeeded/failed alongside the terminal measurement row;
the original idempotent submission receipt remains unchanged.

## Dataset input transfer

Scheduler mints dataset grants through shared transactional domain code; no grant-minting HTTP
endpoint or public admin credential is added. Node
downloads via `GET /api/v1/internal/training/datasets/{revision_id}/content` with a short-lived
node+analysis/attempt-bound header grant, exact digest and byte ceiling. API performs constant-scope
validation and streams from its generated private key; grants are renewable only while authorized
work is live. No query-string tokens, source URL fetching or arbitrary requested storage keys.

The same node-bound grant permits source/index manifests listed explicitly in its scope. Node
verifies size/digest before atomic cache commit. Read-only cached source revisions may serve
several authorized jobs; aggregate cache quota, reference counts and purging prevent unlimited
retention. No dataset content is passed through the existing public chat attachment routes.

## Artifact protocol (checkpoints and adapters)

| Listener and method/path | Contract / behavior |
| --- | --- |
| Control `POST /node/training/artifacts/grants` | `TrainingArtifactGrantRequest`; source/destination declared nodes, immutable artifact/manifest, file/byte bounds, attempt/fence, expiry; returns opaque grant once |
| Control `DELETE /node/training/artifacts/grants/{grant_id}` | Specific grant revoke; does not revoke every grant for a base slug |
| Data `GET /training-artifacts/{artifact_id}/manifest` | `TrainingArtifactManifest` under grant header, bound to immutable digest |
| Data `GET /training-artifacts/{artifact_id}/files/{file_id}` | Stream binary safetensors/JSON by manifest file ID; Range allowed, byte/digest limits; path resolved only from the trusted manifest |
| Control `POST /node/training/artifacts/imports` | `TrainingArtifactImportRequest` -> receipt; source declared node and grant, artifact/digest/attempt, never raw address/path |
| Control `GET /node/training/artifacts/imports/{id}` | `TrainingArtifactImportStatus`; progress plus independently computed verified manifest or safe failure |
| Control `POST /node/training/artifacts/imports/{id}/grant` | `TrainingArtifactGrantRefresh`; same artifact/digest/recipient/fence only, replaces expired/lost grant for the existing transfer |
| Control `POST /node/training/artifacts/{id}/verify` | `TrainingArtifactVerifyRequest` -> verification receipt; hash/structure validation of retained artifact |
| Control `DELETE /node/training/artifacts/{id}` | `TrainingArtifactDeleteRequest`; expected manifest/version and unreferenced proof, tombstone then bounded purge receipt |

Every mutation carries an idempotent command identity. Transfer grants expire after 60 s and can
renew during live authorized transfer; loss of the data fabric fails closed, never falls back to
Wi-Fi/control fabric. Source endpoints accept only grants for the target artifact/manifest/file
set and declared peer; they are not an unauthenticated file server. Node restarts require grants
to be reissued from core state; never recover a plaintext secret from logs. No new network path.

Checkpoint manifests validate safe generated relative names, bounded file count/bytes and expected
tensor key/shape/dtype metadata. Preserve original byte hashes, reject symlinks/escaping paths,
and reject conflicting reuploads. Destination stages privately, verifies every file, fsyncs and
atomically commits the whole directory. A verified destination receipt is not itself core's
durable checkpoint commit; both copies and all rank manifests must be current in one transaction.

## Progress, controls and races

### Retirement cleanup extension

`POST /node/training/attempts/{id}/cleanup` accepts a strict
`TrainingAttemptCleanupRequest` binding job, attempt, fence, declared node and the immutable
prepared command ID. It is available with new training disabled. Cleanup requires fresh whole-group
death plus released memory proof, no owned input delivery and prior checkpoint/component erasure.
It journals exact cleanup intent, hides/purges only the generated attempt directory, and returns
`TrainingAttemptCleanupReceipt`. Failed/uncertain purge retains disk holds; exact replay resumes it.
Serving adapter artifacts and historical core lineage are not removed by workspace cleanup.

Artifact erasure optionally carries `expected_manifest`, matched to the immutable digest, so an
authenticated retirement command can prove erasure/absence of interrupted imports. It never marks
those bytes verified or ready. Known partial import directories and checkpoint rank components
are included in erasure; links/unlisted files, active transfers and current native recovery/commit
references refuse it. `POST /node/training/artifacts/imports/{id}/cancel` drains the owned transfer
before reporting cancellation; the scheduler retires its files only after that observation.
Older live journals lacking artifact-reference tracking retain conservative protection.

The core retention lane issues workspace cleanup only for retired terminal jobs after all checkpoint
erasures and participant stop proofs. It releases the exact participant disk reservation only after
a scope-matched positive cleanup receipt. Retrying a changed command is a conflict, not new authority.
Existing unconstrained persistence state columns require no extra migration; compatible node builds
and regenerated shared contracts are required before new commands are dispatched.

`NodeTrainingEvent`: monotonic local sequence, job/attempt/fence, update, safe discriminated payload
(`progress`, `checkpoint_staged`, `pause_requested`, `stopped`, `failure`) and timestamp. Scheduler
deduplicates by attempt+sequence and rejects stale fences before writing public history. Raw stderr
never enters the payload; sanitized bounded diagnostic codes are separate.

- Restore only complete compatible committed checkpoints; steps and cursor cannot advance based
  on an uncommitted local adapter file.
- Pause commits at a complete optimizer update; callback does not expose in-flight accumulated
  gradients. Both ranks coordinate checkpoint identity and stop. <=60 s or stop using prior checkpoint.
- Cancellation is terminal intent and does not wait for checkpoint durability beyond its <=5 s
  healthy-node kill deadline. Retained prior complete checkpoints remain explicitly promotable.
- A node restart re-adopts by attempt marker plus PID/create time or reports dead/unknown. A
  process existing beyond its execution lease cannot continue training/publishing under old authority.
- A restart between process spawn and receipt must discover the unique spawn intent, not start a
  second process. Unknown or mismatched PID is never killed solely because its number was stored.
- Coordinator/rank loss stops all participants. A new generation starts only after old-group death
  or expired enforceable lease plus node fencing evidence; timeout alone on core is not proof.

## Required node contract tests

Auth/listener separation, declared peer validation, no paths/code/remote load, prepare/start
idempotency and content conflict, fence/lease expiry, half-spawn restart, re-adoption/PID reuse,
pause/cancel/partition deadlines, corrupt/incomplete multi-rank state, grant expiry/refresh/revoke,
data-fabric loss, Range/digest checks, disk quota and release-after-proof must all be covered.
Use actual Postgres + simulated nodes for controller races, offline tiny engine for numerical
state, and real Studios for physical replication/JACCL/coexistence acceptance.
