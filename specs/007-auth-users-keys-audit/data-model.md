# Data Model: Auth, Users, API Keys, and Audit

## User

| Field | Type | Constraints |
|---|---|---|
| id | UUID | primary key |
| email | case-normalized string | unique indexed; never changed implicitly |
| display_name | string | 1–120 characters |
| role | `admin \| user` | indexed |
| active | bool | false invalidates all credentials |
| created_at / updated_at | timestamptz | server timestamps |

An admin may deactivate (logical delete) a user. Under a transaction-level advisory lock, demotion
or deactivation is refused if it would leave zero active administrators.

## Entitlement

| Field | Type | Constraints |
|---|---|---|
| id | UUID | primary key |
| user_id | UUID | FK user, cascade |
| name | string | unique with `user_id` while active |
| granted_by | UUID | FK admin user |
| granted_at | timestamptz | required |
| revoked_at | timestamptz | nullable |

Grant and revoke are lifecycle transitions; rows are retained for auditability.

## API Key

| Field | Type | Constraints |
|---|---|---|
| id | UUID | primary key |
| user_id | UUID | FK user, cascade |
| name | string | 1–120 characters |
| prefix | string | indexed; collisions allowed by design |
| secret_hash | string | Argon2 encoded hash; never projected |
| credential_version | int | increments on rotation |
| scopes | string array | subset of declared scopes |
| requests_per_minute | int | 1–10000 |
| monthly_budget_tokens | bigint | positive |
| created_at / rotated_at / last_used_at | timestamptz | lifecycle metadata |
| revoked_at | timestamptz | nullable; active iff null and owner active |

The create/rotate response adds a write-only `secret`; all later projections contain only prefix and
metadata.

## Rate Window

Primary key `(api_key_id, window_start)`. Fields are `window_end` and `requests`. Admission uses an
atomic conditional upsert and returns the reset time. Old windows may be pruned operationally.

## Usage Accumulator

Primary key `(api_key_id, period_start)`. Fields are `period_end`, `requests`, `prompt_tokens`,
`completion_tokens`, and derived total. UTC calendar-month boundaries define reset without a job.
Existing append-only gateway usage records remain the request-level evidence.

## Audit Record

Existing rows gain `actor_type`, nullable `actor_user_id`, nullable `request_id`, `before`, `after`,
and safe `context`. Existing `actor`, `action`, `target_type`, `target_id`, `outcome`, `detail`, and
`at` remain compatible. The application exposes only ordered list/get operations.

## Principal (request-local, not persisted)

Contains kind (`user`, `api_key`, `service`), stable subject, user id, role, scopes, entitlements,
optional key id/version, and authentication method. It never contains the presented JWT or key.

## State transitions

- API key: `active -> rotated` (same row/version increment) or `active -> revoked`; revoked is final.
- User: active role changes are allowed except last-admin removal; `active -> inactive` invalidates
  every key immediately; reactivation is an explicit admin mutation.
- Entitlement: absent/revoked -> granted; granted -> revoked; each transition appends audit.
- Rate/usage periods roll by choosing the row for the current UTC boundary; historical rows remain.
