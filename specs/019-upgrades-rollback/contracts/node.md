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

A successful single-node recovery with a failed source-pair probe retains paired-placement blocking explicitly. If a source lacks compatible journals/privacy behavior, the command is refused before activation; do not archive or rewrite user history to make rollback pass.
