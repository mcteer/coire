# Data Model: Admin Console

## ConsoleCapabilities

Server-derived shipped/authorized surfaces: cluster, models, instances, jobs, identity, audit, ask. Agent-runs and upgrades are absent until their backend ships. Capabilities never replace route authorization.

## ConsoleSnapshot

Fields: `observed_at`, `cursor`, capabilities, nodes, ledgers, models, instances, jobs, alerts. Node projections carry reachability, reason, observed time/freshness, CPU/GPU/thermal, memory totals/free/budget/reservations/drift, and disk totals/free. Core is explicit with `model_capacity=false`. Bytes are non-negative, percentages 0–100, and unreachable instances cannot project ready.

## ConsoleEvent

Monotonic `id`, `kind`, `observed_at`, typed payload. Kinds: snapshot, cluster/model/activity/identity changed, reconcile. The first event is a snapshot; missing/expired cursors reconcile.

## CursorPage[T]

`items` plus opaque `next_cursor`, using stable timestamp/id ordering. Empty final pages return null.

## Mutable versions

Model, variant, user and key projections expose `version >= 1`. `If-Match` is required for writes; success increments and mismatch returns current state in a conflict.

## ActivityItem

Shipped work union: acquisition/download job, engine/model instance. Fields include id, kind, owner, target, state/reason, start/elapsed, stoppable. Agent runs are absent. Stop is idempotent for terminal items and audited.

## AskRequest / AskResponse

A bounded non-empty question; response status (`answered|unavailable`), answer, observation time, and snapshot sources. There is no action/tool/mutation field.

Existing User, ApiKey, entitlements, Model, Variant, AcquisitionWorkflow, Ledger, Instance, engine/job and AuditRecord entities remain sources of truth. Key secrets remain exclusive to `ApiKeyIssued`.
