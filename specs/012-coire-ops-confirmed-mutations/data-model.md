# Data Model: Coire-Ops with Confirmed Mutations

## OpsSession

- `id: UUID` — random generation created by the ops container at boot
- `service_instance: str` — bounded non-secret container identity
- `state: active | superseded | expired`
- `started_at`, `last_seen_at`, `ended_at`
- At most one active session. Registering a new session supersedes the prior one and expires all of
  its pending proposals/tokens in the same transaction.

## OpsConversation

- `id: UUID`
- `admin_user_id: UUID`
- `ops_session_id: UUID | null` — null for fully degraded conversations
- `state: active | closed`
- `degraded: bool`
- `created_at`, `updated_at`
- An admin can read only conversations allowed by admin routes; all participants are admins.

## OpsMessage

- `id: UUID`, `conversation_id: UUID`
- `role: admin | ops | system`
- `content: str` — bounded; excluded from ordinary structured logs
- `degraded: bool`
- `created_at`
- Append-only.

## ResolvedOpsAction

Strict discriminated union with common fields:

- `operation: instance.unload | run.kill | model.pin | model.unpin | instance.load`
- `target_type`, `target_id: UUID`
- `parameters: operation-specific strict object`
- `precondition: {resource_version, expected_state}`

No string route, engine path, repository id, shell command, user id mutation, or arbitrary payload is
representable.

## OpsProposal

- `id: UUID`, `conversation_id: UUID`, `ops_session_id: UUID`
- `proposer: str` — fixed ops service subject
- `action: JSONB` validated as `ResolvedOpsAction`
- `action_digest: char(64)`
- `rationale: str`
- `state: pending | confirmed | executing | executed | declined | expired | stale | failed`
- `created_at`, `expires_at`, `decided_at`, `executed_at`
- `confirmed_by_user_id: UUID | null`
- `result: JSONB | null`, `failure_code: str | null`

Transitions:

```text
pending → confirmed → executing → executed
   ├──→ declined
   ├──→ expired
   └──→ stale
executing → failed
```

Only `pending` can be confirmed or declined. Confirmation performs `pending → confirmed` while
holding a row lock; exactly one contender succeeds.

## OpsConfirmationToken

- `id: UUID`, `proposal_id: UUID` (unique)
- `prefix: char(12)` (unique), `secret_hash: varchar(255)`
- `action_digest: char(64)`
- `issued_to_user_id: UUID`
- `expires_at`, `used_at`, `revoked_at`, `created_at`
- Plaintext is returned once and never logged or persisted.

## Audit relationships

Proposal creation, malformed/refused proposals, declines, confirmation refusals, token consumption,
stale actions, and terminal execution outcomes append existing audit rows. Execution records the
human admin as actor and `coire-ops:<session-id>` as proposer in bounded detail.
