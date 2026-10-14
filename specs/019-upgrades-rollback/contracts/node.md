# Core-to-Node Upgrade Contract

Use the existing authenticated Core-to-node transport; commands are strict Pydantic models shared through coire-core. The node resolves registered internal artifact keys and registry model IDs locally. Callers cannot submit paths, commands, interpreter names or arbitrary model strings.

| Method and route | Wire shape | Effect |
|---|---|---|
| `GET /node/upgrades/status` | `NodeUpgradeStatus` | Full installed/source digest, bootstrap readiness, maintenance and drift. |
| `PUT /node/maintenance/{job_id}` | `NodeMaintenanceCommand` → `NodeMaintenanceAck` | Apply/reconcile/release one monotonic operation fence; release requires authoritative terminal reconciliation and matching generation. |
| `GET /node/maintenance/{job_id}/inventory` | `NodeUpgradeInventory` | Strict owner inventory under acknowledged generation; any missing owner yields unknown/blocking. |
| `PUT /node/upgrades/{job_id}` | `NodeUpgradeCommand` → `NodeUpgradeStatus` | Idempotently journal and execute a fixed accepted plan using the bootstrap recovery owner. |
| `GET /node/upgrades/{job_id}` | `NodeUpgradeStatus` | Durable stage/receipt cursor and observed identities, independent of original response delivery. |
| `POST /node/upgrades/{job_id}/cancel` | `NodeUpgradeCancel` → `NodeUpgradeStatus` | Safe pre-activation unwind or restoration; never forced user-work termination. |

Command fields: job and node IDs, protocol, generation, source/target release IDs/full digests, immutable preparation receipt ID, allowed action enum, authorization expiry, smoke registry variant and manifest digest, paired-smoke requirement, expected peer runtime, and captured deadlines. Validate source, platform, manifest, compatibility and bootstrap readiness before accepting. A matching replay observes the same operation; different contents under the same job/generation conflict. A stale owner cannot activate or release maintenance. New activation requires a fresh Core authorization bound to the current human administrator; post-activation recovery can restore the recorded source while Core is unavailable.

An activating node command includes both the phase-bound grant and the exact intent receipt
already consumed/persisted by Core. Both bind the same job/generation/runtime boundary and
observed source; either field alone is invalid. The independent native guard retains this
receipt and its source/candidate/process identities before any selection or stop effect.
Candidate smoke must prove positive process/resource cleanup as well as ten generated
tokens. Activation and restoration deadlines are captured once and survive replay/restart;
replaying a consumed command observes its existing intent and does not renew permission.

## Admission and inventory matrix

Every row needs both race testing and startup/resumption coverage. Fence checks occur in owner admission critical sections, before acquiring new process/resource obligations; inventory collection must not use best-effort metrics arrays.

| Work and ownership | Existing integration points |
|---|---|
| Serving engines, orphan processes, request leases and memory holds | `coire_node/engines.py`, `reservations.py`, engine routes; Core placement/gateway admission |
| Shard ranks, component transfers, paired collectives | sharding routes/controllers, training component routes, `link_probes.py` |
| Image workers, jobs and terminal cleanup | image worker/job routes and supervisors; Core image admission |
| Native training, prepared attempts, measurements and analysis | training routes, journal, supervisors, process inventory and reservations; Core training admission |
| Checkpoint/artifact extraction, imports and rank transfer | training extraction/artifact/component routes, importer and analysis supervisor |
| Harness/evaluation containers, workspace and tokenizer work | run/evaluation/workspace routes and owner controllers; Core run/evaluation admission |
| Acquisition, conversion, validation, replication and disk holds | models/jobs/export routes and acquisition workers; Core acquisition workflows |
| Benchmarks, probes and failover participation | benchmark/link/failover routes; standing reservations reported explicitly |
| Core-only obligations for a Core rollout | feedback exports/comparisons/purges, chat private erasure/file work, scheduler renewals and pending callbacks |

An owner with an unreadable journal or uncertain process stop reports unknown. Keep its holds and block activation. Inactive standing reservations are retained and distinguished from runnable work. Stop idle residents only through their normal lifecycle owner, after request/lease drain. Do not cancel training, measurement, image, agent or evaluation work to reach idle.

## Bootstrap, smoke and recovery

Install the coire-node recovery helper through `deploy/launchd/` with a known-good retained interpreter and private journal; it is a node operational helper, not a new Studio service role. Normal node startup reconciles maintenance before resuming acquisition/training/runs or admitting requests. Recovery after reboot must not depend on a functioning candidate environment.

The helper journals intent, verifies artifact hashes, permits only controlled interpreter symlinks, stages atomically, reserves disk and accounts smoke PID/port/memory before returning. Candidate smoke invokes coire-node-owned bare-engine lifecycle on the registry-resolved tiny asset, generates at least ten tokens and proves cleanup. Post-switch checks exact runtime digest and fresh generation, then required paired collective. Smoke exemptions are scoped to the job/model/check and cannot admit general requests through maintenance.

Before switching, revalidate source identity, current authorization, drain and peer readiness. Use PID plus creation identity for restart; never signal by recycled PID alone. All stage deadlines are captured in the plan; no agent health heartbeat extends them indefinitely. Source restoration selects the exact retained environment, proves node health/serving, and reconciles qualifications before releasing the fence. Failed restoration or uncertain inventory keeps placement closed and emits recovery-required receipts.

Interrupted collective cleanup (T072) uses `NodeUpgradeCollectiveCleanup` with
its exact original `NodeUpgradeCollectiveScope`; `cleanup_only` can only be true.
The separate `NodeUpgradeCollectiveCleanupReceipt` binds job/generation, command,
full scope SHA and both namespace inventory SHAs. It reports interrupted cleanup,
not a LinkObservation or successful measurement. The producer must authenticate
through existing node/operation authority, resolve only that retained scope,
verify complete physical owner inventories on both nodes (excluding only its own
exact unresolved logical entry), and retain the observation evidence before
closure. Unknown owners, active workers, changed authority and another dispatcher
holding the scope lock refuse cleanup. An expired original probe deadline permits
no launch, serving work or qualification; cleanup does not renew it. Cleanup alone
cannot release maintenance or finalize an operation. Exact replay returns the
same receipt; altered scope/command/generation conflicts. Installed Sources and
journals must not be hand-patched to add this support.

The node producer is `POST /node/upgrades/{job_id}/collective-cleanup`, authenticated
on the control listener. Its private peer read is
`POST /api/v1/internal/upgrades/{job_id}/collective-cleanup-peer`, authenticated as
rank-zero Studio A and pinned to the current accepted paired plan and maintenance
before/after I/O. This separate read does not widen qualification's peer-read
policy. Canonical local/peer snapshots use one five-second observation budget;
the captured probe deadline and scope remain unchanged. For restored Studio B,
only its captured source or scoped runtime is accepted, never an arbitrary digest.
The producer holds maintenance and collective filesystem locks, persists
`cleanup-local.json` and `cleanup-peer.json` before immutable `cleanup.json`, and
validates cached receipts from historical evidence without treating it as fresh
qualification. Partial preimage writes are not closure. Canonical inventory
recognizes closure only with valid retained proof and matching scope path; physical
owner collection remains independent. Conflicting worker/cleanup results refuse.
The outcome counter `coire_upgrade_collective_cleanup_total` labels only node and
outcome; scoped job/generation/command fields belong in structured logs.

A successful single-node recovery with a failed source-pair probe retains paired-placement blocking explicitly. If a source lacks compatible journals/privacy behavior, the command is refused before activation; do not archive or rewrite user history to make rollback pass.

A paired activation command also carries the fresh Core-authenticated peer inventory
under the same fenced job/generation and exact peer digest. This observation is mutable
execution evidence rather than immutable plan authority, but the consumed activation
command retains it unchanged. Missing, wrong-node, wrong-generation or wrong-digest peer
observations are rejected. The guard requires all owner inventories positively idle and
no older than five seconds at the effect boundary; expiry never permits forced drain.

A pre-activation abort must resume the verified unchanged source without terminating
known pre-existing requests or jobs. A release command may carry `source_unchanged=true`
and the captured `source_runtime_sha256` only with terminal reconciliation. The node
independently verifies its physical source/guard and, for a local selection operation,
a `failed_unchanged` or `cancelled_unchanged` terminal with no activation and positive
operation-resource cleanup. For a peer with no local selection operation, Core's
operation-scoped authenticated release remains the authority. Every required inventory
owner must be complete and every entry reconciled; unknown owners/holds still block.
This exception preserves existing work and resumes admission; it grants no permission
to select another runtime or stop a workload. Activated release keeps the full idle gate.
