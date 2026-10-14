# ADR-0015: Independent upgrade recovery ownership

Date: 2026-10-10. Status: implementation design; executor network access authorized 2026-10-11, full acceptance pending. Spec: `specs/019-upgrades-rollback/`.

Replacing the gateway, scheduler or node runtime can remove the process responsible for observing failure. A recovery owner must outlive the candidate and recover accepted work after host restart.

Core uses a separate, hardened `coire-upgrade-executor` container with one Python process, no shell or Docker CLI, immutable manifests, a private fsynced execution journal and a narrowly writable coordination directory. PostgreSQL remains authoritative and DBOS runs only in the scheduler. The executor executes accepted fixed plans through an exact Docker method/path/target allowlist and the existing socket proxy. It does not receive the raw socket or gain pull/build/exec/prune/topology authority.

The memberships are only `coire-docker`, `coire-internal` and `coire-telemetry`, with no public listener, capability, firewall or proxy-flag expansion. The user explicitly approved the review-only executor topology proposal on 2026-10-11, satisfying the AGENTS.md access gate. The service is opt-in under the `upgrades` profile; managed upgrade behavior remains disabled by default. Initial bootstrap stays not ready until verified. Later executor replacement retains a restartable incumbent supervisor until passive candidate verification, fenced sole-writer handover and stability commit succeed.

Studio recovery is a coire-node operational helper launched from a retained bootstrap interpreter before ordinary node workload resumption. Its journal caches accepted authority and receipts; it is not a new source of truth. Full environment identity, positive workload drain and exact-source restoration govern admission.

The initial sealed Studio interpreter remains a fixed launchd launcher. After a
completed operation releases ownership, the idle helper qualifies current Source
from its prepared release, terminal cleanup history and exact live node PID/birth.
It persists a refresh receipt before changing its interpreter selection, releases
the writer lock and executes only the fixed Source guard module. The node continues
serving. Launcher restart requires committed Source history; an active operation
must name that delegated runtime as Source. A published handover replays without
requiring its originally captured node to survive. Neither the selected candidate
pointer nor an arbitrary command can choose the recovery owner. Retain the initial
launcher and operation history alongside current and compatible Source runtimes.

API background tasks, scheduler-only recovery, a persistent native Core daemon and detached non-rebootable node children were rejected. The container design complies with Principles II/II-a without an exception. Fixed commands, live admin authorization, audit and scoped recovery satisfy IV; coire-core contracts satisfy III; lifecycle and real smoke satisfy I/V; telemetry and adversarial acceptance satisfy VI/VII.

A binary rollback retains compatible additive schema and all protected history. It never restores a backup over newer data or bypasses 018 feedback privacy rules. Executor bootstrap, fencing, deployment ownership and restart-boundary acceptance are required release gates.

## Filesystem identities to verify during bootstrap

A host/executor writable coordination bind must retain verified private, non-root ownership
as observed in each namespace. The isolated macOS/OrbStack test maps operator UID 501
to container UID 65532 while retaining mode 0700 directories and mode 0600 files; writes
map back to the operator. Equal numeric UIDs across kernels are not required. Bootstrap
must verify this mapping with a private round trip before advertising readiness and preserve
it during handover. A host PID cannot substitute for ownership or runtime reconciliation.
Role-private prepared
stores may be staged as independently verified copies for API/executor filesystem identities;
the full manifest/content digests must agree. Bootstrap must validate private ownership and
access before advertising readiness, rather than broadening directory permissions or assuming
that macOS operator UID and the image's default UID are equal. Isolated container tests
exercise the mapping and show that a durable Studio operation hold blocks host deployment
commands across namespaces until verified release. Production bootstrap and handover
acceptance remain separate release gates.

## Cross-kernel ownership

An isolated acceptance probe on macOS/OrbStack demonstrated that a Linux container can
acquire `flock` on a bind-mounted file while the macOS host holds that file's lock.
Consequently a file descriptor lock alone cannot enforce sole-writer deployment ownership.
The coordination store combines the local kernel lock with an exclusive, atomically
published `deployment.lease`, containing project, owner identity, kernel platform and actual
kernel boot identity. Both host and executor must claim it before any runtime mutation.
The local lock still excludes concurrent owners inside one kernel; the shared atomic claim
excludes the other kernel. Child host commands require both the inherited locked descriptor
and the matching shared claim.

Normal committed completion removes the shared claim durably. Interrupted active effects
retain it. Same-namespace recovery must first acquire its local lock and supply the exact
reconciled prior intent; foreign-kernel claims cannot be stolen even with a matching journal
witness. Recovery in the original namespace must settle that owner before handover. PID
absence, lease age and expiry do not authorize takeover. Coordination generations advance
for host commands as well as executor owners; database operation generations are separately
bound and must never be treated as the same counter.

## Paired training identities

Real installed-runtime validation found that the two Studios can have different
sealed interpreter/environment graphs from identical wheels while successfully
running the real collective. The exact pair remains `(SHA_a, SHA_b)`; a version
string or shared hash does not replace either independently verified graph.

Training also needs a shared execution-code identity so both ranks can validate
the same immutable resolved recipe and checkpoint boundary. In managed mode,
this identity hashes actual installed coire-core, coire-node, MLX, mlx-lm,
transformers and tokenizers code/resources, with Python's exact patch version.
It excludes local distribution bookkeeping, generated launcher wrappers and
bytecode caches from this shared compatibility digest. Those bytes remain
included in the full sealed native graph. It reads installed bytes rather than
trusting wheel RECORD assertions. Missing inventories and escaped files fail.

Each node's existing measured hardware/runtime digest additionally includes its
full native graph SHA. Node capabilities, prepared probes and measured reports
therefore bind each rank's exact installed bytes. Core requires fresh native
observations and compares each measured per-node digest before granting new
training admission. Changing either graph invalidates the corresponding report,
even if shared code and all package versions match. Actual workers independently
verify the shared code digest, and paired admission still requires the real
collective for the exact pair. Historical analysis/reports and artifacts remain
unchanged. Wire fields retain their existing shapes and legacy mode retains its
historical digests; upgrades default off.

### Native graph verification and heartbeat latency

Real accepted paired-upgrade validation found that repeated full-byte graph reads
could exceed the five-second guard freshness window even though the independent
heartbeat thread remained live. Runtime verification now reuses a previously
measured file digest only for the same path, device, inode, size, mtime, ctime,
owner, group, mode and link count. Every graph walk still opens and checks every
file and revalidates all directories and controlled symlinks. A same-size edit
with restored mtime changes ctime and triggers a new byte measurement. The cache
is process-local, bounded and never persisted as runtime authority. Selection
receipts and full native graph digests remain unchanged. The heartbeat avoids
verifying an identical bootstrap and selected graph twice per publication.

A completed collective result is fsynced before the final readiness recheck.
A transient readiness failure therefore permits exact-result replay without
starting another collective; interrupted dispatch without a completed result
continues to retain its durable maintenance hold.

Collective qualification releases its read/authorization transaction before
running a remote worker and revalidates the accepted plan, maintenance ownership
and live initiating actor after every result. Each observation is retained only
under that fresh check. Account/key revocation and status reads therefore remain
possible during a collective; a returned result cannot override revocation.
Receipt-journal locks and initial native intent reads allow at most one second
for a concurrent short local write. This retains exclusive locking and all
identity checks. Lifetime/host/recovery ownership stays nonblocking, and neither
lock contention nor a repeated probe extends the original upgrade deadline.

The gateway's strict Core owner census also reads the file worker over the existing private `coire-file-processing` network using the existing `file_worker_service_token`. The user explicitly approved the API's additional membership and secret mount on 2026-10-11 after the proposed overlay passed Compose validation. The worker still has no published listener, database access or model access. Missing credentials, unreachable inventory and uncertain process ownership remain unknown and prevent activation; they are never treated as idle.
