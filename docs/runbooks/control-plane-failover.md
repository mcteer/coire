# Control-plane failover

Core remains the system of record. A Studio admits supported resident text chat only while its
signed snapshot, user identity and elected authority are current. Postgres, scheduler, MCP, admin
mutations and telemetry backends remain on Core. Cloudflare deployment/public-edge qualification
is deferred by the owner; use authenticated origin checks until that deployment is qualified.

## Observe and authenticate

Use Grafana `coire-failover` for readiness, snapshot age, participant last-seen, audit pressure,
drain deadline and frontend RSS. Alerts cover absent telemetry/participants, no ready authority,
split brain, stale snapshots, refusal rate, audit exhaustion, resource and drain limits.
Core's election monitor is `GET /failover/ready`; Studio's is `GET /ready` on the frontend.
Both require `X-Coire-Failover-Monitor` and return 200 only for current serving authority, with
no-cache headers. Core `/ready` remains process liveness, not election readiness.

Browser/API/assets require a valid approved Access JWT; local testing may use an isolated signed
identity fixture but production has no anonymous bypass. `X-Coire-Failover-Lifecycle` permits only
local in-flight accounting on `/api/v1/failover/status`. Relay credentials do not grant inference
by themselves: the node verifies a signed, request-bound authority and the original user JWT.
Peer control uses signed, challenge-bound messages and separate host keys. Do not print credentials.

## Arm and scope

1. Use matching protocol-v2 Core, node and frontend artifacts, trusted membership epoch and keys.
   Provision Keychain items and environment settings as listed in `deploy/compose/README.md`.
   Preserve the persistent safety/journal directory. First installation or missing/corrupt safety
   state imposes a conservative 301-second quarantine; lowering the lease TTL cannot bypass it.
2. Confirm all three hosts through authenticated status paths, no active 019 maintenance owner,
   valid snapshots and standing 256 MiB reservations. Core signs registry-verified exact resident
   placements; empty capabilities, old slug-only metadata, adapter/image/tool targets and subjects
   needing mutable policy/budget lookups are ineligible.
3. Precreate the digest-pinned Studio frontend using `deploy/compose/coire-studio-failover-create`.
   The node owns its activation under the same maintenance admission guard as engine lifecycle.
   Frontends have no peer private key. Node listeners retain existing control DNS/port bindings.
4. Confirm Core alone is ready while healthy and direct anonymous/stale-origin requests refuse.
   A provider monitor/pool template is not evidence of deployed ingress readiness.

Defaults: snapshot horizon 120 seconds, promotion 15 seconds, demotion 45 seconds, lease 15
seconds, drain 30 seconds, polling 2 seconds. Snapshot refresh stops with Core; the remaining
signed horizon bounds the outage service, including break-glass. There is no local renewal or
cached-user fallback after expiry. Prompt, output, rate and concurrency use the stricter signed
subject/model limits. No failover path loads a model, acquires an artifact or replaces placement.

Single-node residency binds instance, immutable artifact, PID and process creation time. Sharded
residency binds the exact group/command generation and both owned workers, with authenticated
all-rank and fabric observations. Rank, artifact, process or link loss refuses admission and
interrupts streams within three seconds. TP additionally requires working RDMA; PP requires the
existing IP fabric. A generic RDMA-enabled status cannot establish a working native group. Old
or adopted groups without verified worker ownership require a new normal admin placement before
they can qualify; failover never restarts them automatically.

## Partition and one-host break-glass

A minority never promotes automatically. Readiness and actual gateway/node admission independently
fence expired or conflicting authority even if ingress still routes there. Investigate split-brain
alerts immediately; compare signed epochs/terms and independently fence the unintended origin.
Do not delete durable election records to force promotion.

While Core is available, `POST /api/v1/admin/failover/overrides` requires an authenticated admin
whose UUID matches `actor_id`. The signed protocol-v2 body includes `override_id`, `target_host`,
`epoch`, `issued_at`, `expires_at`, `kind` (`inhibit` or `break_glass_promote`), `actor_id`, `reason`,
`fencing_evidence` and `signature`. Sign canonical model JSON excluding signature. Lifetime is at
most 300 seconds, with exactly one target and current epoch. Record independent fencing evidence
for every competing origin before activation. Core audits intent before delivery and validates
the target's signed acknowledgement. Replay remains rejected across restarts.

For Core-down activation, prepare and safeguard the bounded signed override while the authorized
signer is available, then deliver it through the documented signed node override route after the
independent fencing is established. Do not copy Core's private key to a Studio. A pre-authorized
override cannot outlive snapshot/JWT validity or create eligibility; an inhibit is not a serving
proof. Record actor, reason, fence evidence, override UUID and expiry in the execution record.

## Drain, kill and audit repair

Healthy Core must remain stable for the demotion window. Signed heartbeats carry a separate
recovery-readiness bit: PostgreSQL, enabled service probes and 019's retained qualified writer
must pass without an active Core maintenance fence. This observation expires locally within
five seconds at default polling; a live but unready Core does not trigger drain. Hand-back stops new admission before
drain, and active requests continue only until the earliest JWT/snapshot/lease/drain deadline.
Cancellation closes upstream work. Core requires a matching signed fence acknowledgement or
conservative authority expiry before taking over; a draining response is not a fence acknowledgement.
The frontend stops after fencing and the standing memory reservation stays held.

For an emergency stop, disarm the node failover controller through the 019-owned configuration
transition (`FAILOVER_MEMBER_NAME` empty), then stop its precreated frontend. Disarm both Studios
when disabling the feature, and stop Core's failover publisher/poller as part of the coordinated
transition. Removing proof.json alone is insufficient because a running poller can recreate it.
Keep the existing native engine ownership and user data intact. Verify direct origins refuse,
then wait out the maximum outstanding authority lifetime plus clock uncertainty before allowing
an incompatible old protocol to serve. Preserve the trusted epoch high-water mark.

Ordinary journal capacity is bounded with reserved terminal-event space. Full, corrupt or unwritable
evidence closes admission; fencing still proceeds. Preserve the journal and safety files, fix
storage permissions/capacity, and reconcile through signed committed-event acknowledgements.
Core receipt IDs deduplicate concurrent replays. Never truncate unacknowledged events or delete
`failover_event_receipts`/audit rows to regain readiness. Archive corrupted evidence for diagnosis;
restore only verified durable state through an operator recovery, then re-observe quarantine.

## Update, rollback and resources

Drain/disarm all participants before mixed-version updates. Use 019 to retain/restore compatible
artifacts and writer ownership, then install matching schemas/settings, obtain fresh snapshots,
wait out any quarantine and re-arm. Rollback also requires disarming first; it cannot roll back
the trusted epoch, erase outstanding grants or discard audit evidence. Cloudflare eligibility
must remain off until its separately qualified setup is applied.

Metrics use the existing Studio-to-Core OTLP endpoint; diagnostic traces have bounded queues and
are disabled by default. Collector loss must not block inference. Measure fifteen minutes of idle
baseline and armed operation per profile: incremental participant RSS <=32 MiB and CPU <=1%,
frontend <=256 MiB, no resident evictions and zero actual swap growth. Record native PID/create-time,
`vm.swapusage`, image/source identity, timings and cleanup in `specs/020-control-plane-failover/review.md`.
A real Core power-off requires a demonstrated independent recovery route. Process tests are not
physical power-loss evidence. See the feature quickstart for the full acceptance matrix.
