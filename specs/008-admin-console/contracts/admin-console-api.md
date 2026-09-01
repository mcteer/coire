# Admin Console API Contract

All routes authenticate; every `/api/v1/admin/*` route rechecks current administrator status.

## Live overview

- `GET /api/v1/admin/console` → `ConsoleSnapshot`
- `GET /api/v1/admin/console/events` → SSE `ConsoleEvent`; supports `Last-Event-ID`, keep-alives and reconcile.

## Paginated collections

- `GET /api/v1/admin/models?cursor=&limit=&state=&visibility=`
- `GET /api/v1/admin/activity?cursor=&limit=&kind=&state=`
- `GET /api/v1/admin/users?cursor=&limit=&role=&active=`
- `GET /api/v1/admin/audit?cursor=&limit=&actor=&action=&target_id=&outcome=`

Each returns a typed cursor page. Limit is 1–100, default 50. Invalid cursors return RFC 9457 `400`.

## Mutations

Existing acquire/load/unload/drain/retry and identity routes remain canonical. Mutable writes require `If-Match: "<version>"`; stale writes return RFC 9457 `409` with current version. Destructive requests carry exact `confirm_target`; mismatch is `422`. Attempts and outcomes are audited.

`POST /api/v1/admin/activity/{kind}/{id}/stop` dispatches only shipped allowlisted kinds. Unknown/unshipped kinds are `404`.

## Ask Coire

`POST /api/v1/admin/ops/ask` accepts a question and returns answered/unavailable status, answer, timestamp and grounding sources. No action/tool/confirm shape exists.

Responses never expose node/HF tokens, API-key hashes, engine traces or model filesystem paths.
