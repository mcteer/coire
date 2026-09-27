# Contracts: Container Runs

All objects are strict `coire-core` Pydantic wire models. Problem responses use RFC 9457.

## Control-plane API

- `POST /api/v1/runs` — authenticated user creates a queued run from profile, primary registry
  model, opaque workspace reference, profile-compatible tool/model scope, and bounded limits.
  Returns `202 AgentRun` and never returns the bearer run token.
- `GET /api/v1/runs/{run_id}` — owner or admin reads state/result metadata.
- `GET /api/v1/runs/{run_id}/events` — owner or admin receives state/log SSE with keep-alives and
  bounded replay.
- `DELETE /api/v1/runs/{run_id}` — admin kill; revokes first, audits, returns `202 AgentRun`.
- `GET /api/v1/admin/runs` — scoped admin listing/filtering.

The scheduler obtains the one-time plaintext run token through an internal process boundary when
creating the container; no read API can recover it.

## Node API

All routes require the existing node-control bearer and accept only scheduler-authored identifiers.

- `POST /node/runs` — idempotently create the hardened container and internal network; returns
  container identity and observed hardening summary.
- `POST /node/runs/{run_id}/start` — idempotently start.
- `GET /node/runs/{run_id}/logs` — framed bounded stream from an offset.
- `POST /node/runs/{run_id}/wait` — bounded wait/inspect result.
- `GET /node/runs/{run_id}/result` — bounded strict result document or typed missing/unreadable error.
- `DELETE /node/runs/{run_id}` — kill if requested, remove container and network idempotently.
- `GET /node/runs` — labeled run-container observations for reconciliation.

Every create request contains an allowlisted image digest, exact argv, run token, gateway URL,
workspace source, and limits. It cannot supply Docker host configuration, mounts, networks,
capabilities, users, entrypoints, or arbitrary environment keys.

## Gateway run-token authorization

The existing `/v1` routes accept a run bearer only when the server-side token row is active. Model
selection is intersected with `permitted_model_ids`; token usage is charged atomically before a
request can exceed its ceiling. Admin and non-inference routes always reject run tokens.

## Error codes

`run_capacity_exhausted`, `run_node_unreachable`, `run_runtime_unavailable`,
`run_result_missing`, `run_result_unreadable`, `run_log_limit_exceeded`, `run_timed_out`,
`run_token_expired`, `run_token_revoked`, `run_scope_denied`, and `run_spend_exhausted` are stable
problem-detail codes and never include Docker output or bearer material.
