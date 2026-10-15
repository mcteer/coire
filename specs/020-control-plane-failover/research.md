# Research: Control-Plane Failover Reconciliation

**Date**: 2026-10-14. Repository baseline: `cd355554` (019 merged). Read-only code research;
no runtime tests, live configuration changes, or acceptance claims in this pass.

## 1. Preserve the service; correct authority gaps

**Decision**: Keep the existing three-member participant and separate failover service. Extend
strict coire-core contracts and targeted safety logic, rather than replacing the control plane.

**Evidence**: `failover_election.py` holds terms/grants in memory and uses wall time.
`coire_node/failover/participant.py` has a restart hold, but it uses the current TTL and does not
recover an older grant. Core does not protect voting with the equivalent durable guard.
`FailoverLease` currently permits both proof and override. The frontend checks authority at request
start; its streaming iterator only updates a counter. Core readiness alone does not fence gateway
requests, and node relay trusts a static token without request-bound proof.

**Rationale**: Quorum intersection requires non-overlapping grant lifetimes, including restarts and
membership changes; probe routing alone cannot enforce that. A current proof must fence the actual
admission and relay paths. Bounded monotonic deadlines and a persisted guard prevent reissued votes
from defeating a live lease.

**Alternatives considered**: Reachability-only election, one-vote-per-term without cross-term/restart
guards, relying on load-balancer health, and replicating Postgres are rejected. The first three allow
conflicting authority; the last violates Principle II. Existing code is a baseline, not evidence of
completed safety acceptance.

## 2. Snapshot lifetime and limited inference

**Decision**: Retain the 120 s default freshness bound, Access browser identity, and no local renewal
or API-key fallback. Snapshot authority plus live exact residency must agree. Scope follows FR-013/FR-031; the original sharded-inference requirement remains in scope because
its deferral has not been authorised. Add exact group generation and authenticated rank/link proof,
with no engine lifecycle action or fallback on rank loss.

**Evidence**: `settings.py` sets the bound; `coire_failover/auth.py` fetches public JWKS using signed
snapshot verifier metadata. `FailoverResidentEngine` currently exposes only engine ID, slug, and
state; broad `ChatCompletionRequest` accepts content beyond text. Existing adapter refusal does not
prove placement, sharding, vision, or tool restrictions. Add explicit capability/placement evidence,
bounded limits, and independent relay verification.

**Rationale**: Serving stale identity/entitlement data indefinitely would change the security model.
An outage longer than the remaining snapshot life ends degraded inference. Trial records must show
that boundary and refresh authority between independent fault scenarios.

**Alternatives considered**: A larger freshness default, Studio-issued authority, copied API-key
hashes, and silent target fallback are rejected for this revision. A separate reviewed feature may
support longer offline authority.

## 3. Break-glass and audit completeness

**Decision**: Bind an override to one host, epoch, unique identity, human actor, short lifetime,
reason and independent fencing evidence. Require intent audit before delivery; persist replay guards.
Retain unacknowledged security/election evidence until an authenticated core acknowledgement.

**Evidence**: `routes/admin_failover.py` broadcasts the same unbound override to both Studios and
writes audit after delivery. `failover/journal.py` truncates entries to the last 256, while the
subscriber replays without acknowledgement/compaction. Current event fields omit actor/reason and
have incomplete tier/security event coverage. Concurrent duplicate reconciliation also needs a
transaction-safe idempotency test.

**Rationale**: An isolated survivor cannot determine whether another origin is serving. The operator
must independently fence other public origins before overriding quorum. Audit exhaustion cannot
silently lose security evidence; reserve terminal-fence capacity and stop admitting work first.
Minimal safety evidence is not a second application system of record.

**Alternatives considered**: Broadcast overrides, silent ring-buffer loss, delayed audit intent,
unbounded journals, and refusing to fence because audit storage failed are rejected.

## 4. Cloudflare routing and authenticated monitors

**Decision**: Use the existing outbound tunnel architecture with ordered pools (`Off - Failover`),
authenticated HTTPS readiness monitors, and an explicitly non-serving fallback. Verify a disabled
fallback pool on the actual proxied load balancer; retain origin-side proof checks even if routing
health is stale. Use a dedicated monitor Service Auth credential in existing Access policy scope.
No policy or firewall widening is authorised by this plan.

**Evidence**: The repository's `deploy/cloudflared/` templates and `test_topology.py` treat null/empty
fallback as sufficient. Cloudflare documents that fallback health is ignored, the last pool may be
the default fallback, and a disabled fallback on a proxied hostname yields HTTP 530/1016. A null
placeholder therefore does not establish fail-closed behavior. Monitors support custom headers,
intervals and consecutive thresholds; actual account settings must fit the 60/90 s objectives.

**Rationale**: Health monitors steer traffic; they are not an ownership lock. A fail-closed external
fallback reduces accidental routing, while application checks remain the security boundary.

**Alternatives considered**: Treating tunnel replicas as an election, `steering: priority` as a real
provider configuration, anonymous monitor bypass, and assuming null fallback means no traffic are
rejected.

**Primary sources checked 2026-10-14**:
- [Cloudflare pool health and fallback behavior](https://developers.cloudflare.com/load-balancing/understand-basics/health-details/)
- [Cloudflare standard steering and Off - Failover](https://developers.cloudflare.com/load-balancing/understand-basics/traffic-steering/steering-policies/standard-options/)
- [Cloudflare monitor configuration](https://developers.cloudflare.com/load-balancing/monitors/create-monitor/)
- [Cloudflare Access service tokens](https://developers.cloudflare.com/cloudflare-one/access-controls/service-credentials/service-tokens/)

Account entitlement, monitor permission, and credentials remain execution preflight facts; the docs
do not establish that this user's deployed account is configured or qualified.

## 5. Features 019, 022 and 023

**Decision**: Integrate with existing maintenance and control-address ownership. Keep signed liveness
and election exchanges available during user-work maintenance without allowing snapshot publication
or audit writes to bypass 019's writer guard. Hand-back requires the authoritative cohort ready.
Gate node relay admission and frontend activation against Studio environment maintenance, retaining
active-stream inventory until completion. Resolve declared VLAN control addresses, never data fabric.

**Evidence**: 019's `upgrades/inventory.py::failover_inventory` and node controller `upgrade_inventory`
already observe failover. Core `upgrades/admission.py` classifies POST peer exchanges as ordinary API
work and can return 503 during maintenance, falsely appearing as lost health. Node relay and
frontend start lack corresponding maintenance admission integration. `failover_poller.py::peer_url`
uses hard-coded bare host names instead of declared control-address contracts.

**Decision (observability)**: Initialize the shared telemetry bootstrap and prove export through the
existing permitted control-plane path in both 023 profiles. Add no observability backend on a Studio.

**Evidence**: The failover compose service specifies `otel-collector:4317` but joins `coire-edge`;
the frontend entry point does not initialize shared telemetry. Creating meters does not prove they
reach a running collector. A new network attachment would require user approval; first use the
already approved Studio telemetry path and test its real reachability.

**Alternatives considered**: Bypassing all maintenance checks, changing writer roles, forging health,
starting models during failover, widening networks automatically, or making diagnostics mandatory
are rejected.

## 6. Qualification and compatibility

**Decision**: Preserve T001–T040 historical completion markers; append corrective tasks and make T036
an aggregate release gate dependent on them. Run focused checks for each change and one complete
required gate set after integration. Record live evidence separately from isolated CI evidence.

**Evidence**: `test_control_plane_failover.py` includes injected ASGI apps, in-memory participants and
configuration assertions; these do not prove persisted election safety, real public ingress, live
stream cancellation, or measured container resource bounds. The old review explicitly lacks live
power/partition/hand-back acceptance.

**Compatibility**: New authority contracts use an explicit protocol version/epoch. Fail closed on
unsupported versions; never reinterpret a legacy unbound override as the new authority. Drain and
disarm failover during an incompatible rollout, keep core normal service authoritative, update all
participants through the existing 019 workflow, then re-arm with a fresh signed epoch. Rollback
restores a mutually compatible source cohort while failover stays disarmed. Preserve audit receipts;
if their storage must change, add one reversible migration without rewriting historical migration
0013. No new migration is assumed unless the final receipt model requires it.
