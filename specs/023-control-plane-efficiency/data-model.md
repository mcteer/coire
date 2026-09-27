# Data model and ownership

No database migration or new persisted entity is planned. Existing commands, run states and audit rows remain authoritative.

## Console cache (per API process)

Common snapshot, monotonic expiry, semantic comparison and one in-flight projection. Two-second TTL; no success caching on failure or background projection without consumers. Admin authorization precedes access. No credentials/user-specific entitlement results enter shared data. Existing ConsoleSnapshot/ConsoleEvent/CoreHostCapacity shapes remain; observation metadata is separated from meaningful freshness/state changes.

## Polling state (per worker/event loop)

Outcome, idle/failure delay, stop signal and rate-limited failure summary. Success resets; empty ordinary scans back off to five seconds; failures to 30 seconds. Fixed-cadence kill lane is independent. In-flight durable rows survive local cancellation. Sessions/events/connections belong to their owning event loop, including the DBOS boundary.

## Run state guard (existing database rows)

Kill acceptance atomically sets KILL_REQUESTED, revokes credentials and writes acceptance audit. Normal progress cannot leave that state or restore a token. Confirmed/idempotent stop permits KILLED and one completion audit. Failed stop remains pending/retryable. Never-placed kill requires proof that concurrent CREATE cannot occur. Existing deterministic command IDs and node identifiers suffice.

## Release and credential generations (host files)

Manifest: validated project/release IDs, source revision, exact image references/digests, profile set, runtime config hashes and external credential-generation paths; no secret values. Stable volume/project identity survives updates. Release states: staged → validated → active → previous. Active config bytes are immutable; conflicting reinstall fails. Credential generations are complete before activation, retained while mounted, and cleaned only through verified ownership after stop.
