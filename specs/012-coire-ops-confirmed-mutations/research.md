# Research: Coire-Ops with Confirmed Mutations

## Decision: separate proposing identity from confirming identity

The ops container authenticates with a dedicated internal credential whose only authorities are
read operational facts, register its current session, and create allowlisted proposals. Public
confirmation and decline routes accept only a human `admin` principal; API keys and the ops service
principal are rejected.

**Rationale**: prompt or model compromise cannot turn proposal capability into execution authority.

**Alternatives considered**: an admin API key in the container was rejected as implicit trust; a
signed model response was rejected because model output is not an identity.

## Decision: random opaque tokens plus a canonical action digest

The API generates `coire_confirm_<prefix>_<secret>`, stores only its Argon2id hash, and stores a
SHA-256 digest of canonical JSON containing proposal id, conversation id, ops session id, operation,
target, parameters, and state preconditions. Confirmation echoes the resolved action, which must
produce the same digest. The row is locked and marked used before dispatch.

**Rationale**: this matches existing key/run-token handling, prevents parameter substitution, and
provides atomic single use under concurrent admins.

**Alternatives considered**: JWTs expose action content and complicate immediate revocation; a
generic confirmation token is vulnerable to confused-deputy redirection.

## Decision: persisted proposals bound to a volatile ops session generation

Each container start registers a new random session id and invalidates pending proposals for earlier
sessions. Proposal confirmation requires the proposal's session to equal the current healthy session.
Conversations and history may remain for audit and display, but old pending proposals cannot execute.

**Rationale**: durable audit/history and restart invalidation are both required.

**Alternatives considered**: in-memory proposals lose audit/history; durable proposals without a
generation remain approvable after restart.

## Decision: fixed typed action registry

The initial registry contains `instance.unload`, `run.kill`, `model.pin`, `model.unpin`, and
`instance.load`. Every entry defines its Pydantic parameter type, target lookup, optimistic
precondition, executor, audit action, and reversibility. Delete, retire, acquire, user, entitlement,
upgrade, shell, and arbitrary route actions are absent.

**Rationale**: absence by construction is stronger than prompt policy and keeps schemas flat.

**Alternatives considered**: arbitrary admin-route proxying and configurable URLs were rejected as
privilege expansion and contract bypasses.

## Decision: deterministic degraded responder remains in the API boundary

`coire-api` supplies a bounded snapshot to the ops service and uses the existing deterministic
snapshot answer when the service reports the pinned model unavailable or the service cannot be
reached. Action-like questions return an explicit degraded refusal and never create a proposal.

**Rationale**: status remains useful without inference, and no fallback model can land on core.

**Alternatives considered**: a local tiny model violates Principle II; returning only 503 fails the
feature's recovery requirement.

## Decision: confirmed execution reuses existing domain services

After atomic token consumption and stale-state validation, the API calls the same run kill,
instance drain/load, and model pin/unpin services used by existing admin routes. Long operations
return their normal accepted resource and are not performed inside the token transaction.

**Rationale**: one mutation implementation preserves ledger, workflow, audit, and reconciliation
semantics.

**Alternatives considered**: direct database updates or node calls would bypass existing invariants.
