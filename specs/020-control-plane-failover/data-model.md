# Data Model: Control-Plane Failover

**Reconciled**: 2026-10-14. These are design changes for strict Pydantic models in
`packages/coire-core/src/coire_core/models/failover.py`; no schema implementation occurred here.
Existing names are retained where possible. New names below are proposed contract names.

| Entity | Required information | Validation/lifetime |
|---|---|---|
| FailoverMembershipConfig | protocol version, epoch, three names, fixed priorities, public keys, signing key ID, declared control endpoints | Core-signed; exactly core/edge-a/edge-b; reject unsupported protocol, rollback, data-fabric URLs, duplicates and key/identity mismatch. |
| FailoverHeartbeat | member, epoch, challenge, issue/expiry, signature | Response binds the fresh request challenge and responder; bounded age prevents replayed liveness. |
| ElectionVoteRequest / ElectionVoteGrant | version, epoch, term, candidate, voter where applicable, request ID, issue/expiry, signature | Lifetime <= configured maximum; distinct known identities; signed canonical representation; no conflicting live grant across terms. |
| ElectionSafetyRecord (local) | member, protocol/epoch, highest term, outstanding grants including self-grant, prior maximum lifetime, trusted snapshot high-water mark, replay IDs, clock/boot guard | Atomic durable write before external grant/activation; corruption/missing state triggers conservative quarantine; no models/users/jobs stored. |
| PromotionProof | epoch, term, candidate, two distinct grants | Matching candidate/term/epoch, valid signatures and bounded expiry; holder must match local host. |
| FailoverLease | holder, protocol/epoch, exactly one proof or break-glass authority, phase, drain deadline | `serving` or `draining`; draining authorises only existing requests. No authority outlives snapshot, identity, grant or drain bound. |
| FailoverSnapshot | ID, version, issue/expiry, membership, approved roster/capabilities/limits, Access verifier, signature | Trusted core key outside snapshot; monotonic version/epoch acceptance; atomic replace; default maximum age 120 s. |
| FailoverAccessSubject | approved Access issuer/subject binding, eligibility, conservative limits, policy version | Core-authorised cache only; disabled/unknown subjects and mutable-budget-dependent accounts refused; no raw bearer or API-key secrets. |
| FailoverModel | canonical registry ID/slug, display name, publication/entitlement, capability/placement eligibility, prompt/output/rate/concurrency limits | Only exact supported targets; no paths, acquisition data, or caller-selected engine arguments. |
| FailoverResidentEngine | engine ID, canonical model identity, exact target, placement/capability evidence, instance/group generation, rank identities/serving rank, authenticated rank/link observations, state, observation time | Must prove FR-013 scope and current readiness; bounded age; reject identity/placement ambiguity. |
| FailoverRelayRequest / RelayAuthority | request ID, target engine/model, typed text request, holder, epoch, proof/override binding, snapshot digest, signed request/identity binding, earliest expiry | Relay credential alone is insufficient; node independently verifies intended target, authority and residency, applies maintenance admission and bounds, cancels at expiry. Never log raw identity tokens. |
| FailoverOverride | unique ID, kind, human actor, single target host, epoch, issue/expiry, reason, fencing evidence, signature | `inhibit` or `break_glass_promote`; actor matches authenticated principal; no cross-host reuse/replay; explicit target for inhibit too; normal core/admin path audits before delivery. |
| HandbackNotice / HandbackAcknowledgement | epoch, term, holder, successor, request ID, issue/expiry, fence status, signature | Successor is higher priority and authoritative-ready; acknowledgement proves predecessor no longer admits; otherwise wait conservative expiry. |
| FailoverStatus | tier, elected host or null, admission available, reason, reachable members, snapshot expiry, unavailable capabilities, in-flight count for internal scope | `minimal` without authority must report unavailable, not suggest usable inference; browser projection excludes internal-only details. |
| FailoverEvent | event ID, epoch/term, kind, host, time, before/after tier, proof/override identity, actor/reason for administrative/security events | Content-free; persist before admission transition; no prompt, response, key, bearer token or user content. |
| FailoverEventBatch / FailoverEventAcknowledgement | host, signed batch identity, bounded event list / explicit committed event IDs | Core transaction commits audit and receipt atomically; concurrent duplicate replay is idempotent; only acknowledged entries may be compacted. |
| FailoverProblem / FailoverMonitorStatus | stable typed refusal reason or minimal readiness result | RFC 9457 errors where applicable; no engine stack trace, secret, local path or unbounded field. |

## State and authority transitions

- `unarmed → quarantined → standby`: validate configuration and recover safety state. Missing trusted
  prior state requires the maximum supported old grant lifetime plus the 1 s clock uncertainty bound.
- `standby → candidate → elected`: durable grant safety, two coherent grants, fresh snapshot and
  eligible priority. Serving readiness also requires reservation, approved image, valid route
  identities, and available audit capacity.
- `elected → draining → standby`: stop admission, retain bounded authority for existing requests,
  acknowledge fencing, then stop the container. Keep the standing reservation.
- Any invalid proof, lost authority, journal failure, or unbounded clock uncertainty fences immediately;
  it does not wait for graceful drain. Always remove serving authority even if event persistence fails.
- A fenced break-glass activation may bypass quorum only; it cannot bypass scope, identity, audit,
  maintenance, freshness, or expiry. Normal Core recovery fences/reconciles it before re-admission.

## Storage and bounds

Safety records, snapshots and journals are separate files, owned by the participant and atomically
replaced with fsync/parent-directory durability as appropriate. Frontend reads only the signed data
it needs. Keep secrets in Keychain-backed secret paths. Retain the existing 256-event ordinary
journal budget initially, plus reserved terminal-event capacity. Acknowledged compaction restores
capacity; unacknowledged exhaustion prevents further admission. No silent truncation or user-content
spool is permitted. Detailed failures are local structured telemetry without sensitive material.

## Compatibility and migration

Use explicit protocol version and epoch to fence incompatible participants; fail closed on old
unbound overrides. Coordinate drain/disarm/update/re-arm through 019, retaining the prior source
artifacts for rollback. Generated OpenAPI and TypeScript must change with model implementations.
Audit remains core-owned. Reuse `failover_event_receipts` if it supports the new acknowledgements;
otherwise one new reversible migration is required. Never edit the shipped 0013 migration or discard
old audit/receipt rows during rollback.


### Signed recovery readiness (implementation closure)

Protocol-v2 `FailoverHeartbeat.recovery_ready` is false by default and covered by the signature.
Peer liveness classification remains independent. Production Core publishes true only after a
bounded database/cohort probe and, when 019 is enabled, the existing retained-writer qualification.
Readiness expires after two polling intervals plus one second. Studio recovery damping starts
when both healthy liveness and signed recovery readiness are continuously observed; older/missing
readiness never starts hand-back. Coordinated disarm/update/re-arm remains mandatory.
