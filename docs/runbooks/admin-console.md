# Admin console operations

## What runs

The console is static React content in `coire-web`; nginx proxies authenticated API and SSE requests to `coire-api`. It never talks to a Studio or engine directly. `/api/v1/admin/console/events` sends reconciliable snapshots and `/api/v1/admin/ops/ask` is read-only. Agent-run and upgrade controls remain absent until their owning features ship.

## Observe

- Confirm `/api/v1/me` reports the expected active `admin` role, then inspect `/api/v1/admin/console` for the exact snapshot used by the browser.
- Watch `coire_console_snapshots_total`, `coire_console_stream_connections_total`, `coire_console_activity_pages_total`, and `coire_console_ask_total`. Trace names are `coire.api.console.snapshot`, `coire.api.console.activity`, and `coire.api.console.ask`.
- A stale heartbeat is deliberately displayed as stale. Compare its timestamp with `coire-node` health and the node prober logs before restarting anything.
- Browser developer tools should show one long-lived console event request. Repeated requests indicate reconnects; check nginx has buffering disabled and adequate read timeout.

## Stop work safely

Use only named, server-audited controls exposed for shipped resource kinds. Instance drain and engine unload remain the canonical API operations. If the UI cannot reconcile after a successful action, verify the resource through the corresponding admin API before repeating it; terminal operations are designed to be idempotent.

Ask Coire exposes no mutation tools in this feature. Treat an action claim in its prose as a defect and verify the audit trail; the response contract cannot carry a tool call or confirmation token.

## Access failures

A hidden Admin dock item is convenience only. Direct routes are protected by `CurrentAdmin` on every request. For 401/403 behavior follow `docs/runbooks/identity.md`; never enable the legacy administrator bearer merely to make the browser work.

## Rollback

Roll `coire-web` back first if rendering is broken; API operation remains available. Roll `coire-api` back with its matching OpenAPI-compatible web image if the snapshot contract changed. No Feature 008 data migration is required. Do not widen CORS, ingress networks, or node exposure during rollback.
