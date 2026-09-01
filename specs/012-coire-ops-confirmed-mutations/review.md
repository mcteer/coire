# Feature 012 Review Evidence

## Dependency evidence

- `fastapi==0.135.1` — MIT — internal typed HTTP and health boundary for the long-lived ops-only
  distribution. It is an optional `coire-agent[ops]` dependency and is absent from the Studio user
  harness image.
- `uvicorn==0.41.0` — BSD-3-Clause — single-process ASGI runtime for that boundary. It is likewise
  ops-only. Both exact versions were already locked for `coire-api`; no new transitive package or
  licence was introduced.

## Validation evidence

Pending implementation completion.
