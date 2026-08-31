# Runbook: compatible inference gateway

The gateway is the only control-plane component permitted to contact a model engine. It serves
OpenAI-compatible `/v1/models` and `/v1/chat/completions`, plus Anthropic-compatible `/v1/messages`.
Callers use registry UUIDs; repository names, slugs, paths, and adapter suffixes are refused.

## See it

```bash
curl -sS http://127.0.0.1:8080/v1/models | jq
curl -N http://127.0.0.1:8080/v1/chat/completions \
  -H 'content-type: application/json' \
  -d "{\"model\":\"$MODEL_ID\",\"stream\":true,\"messages\":[{\"role\":\"user\",\"content\":\"hello\"}]}"
```

Use the `Coire Gateway` Grafana dashboard. Correlate `request_id`, `model_id`, `engine_id`, and
`user_id` across gateway logs and traces. The important metrics are
`coire_gateway_request_duration_ms`, `coire_gateway_first_token_duration_ms`,
`coire_gateway_overhead_duration_ms`, `coire_gateway_inflight`, and
`coire_gateway_failures_total`. The overhead histogram excludes the measured upstream engine
interval and is the metric used for the 20 ms p95 gateway SLO.

## Diagnose

- `404`: the UUID is absent, unpublished, unready, or unentitled. These cases deliberately look
  identical to a non-admin. An admin can inspect `/api/v1/admin/models/<uuid>`.
- `503` with `Retry-After`: the caller disabled cold waiting, the wait ceiling expired, or loading
  failed. Inspect the model and engine state through the admin API.
- `429` with `Retry-After`: the selected engine reached its configured in-flight cap.
- A stream ending with an error event means the engine failed after response headers were sent.
  It is accounted as failed, never as a successful truncated response.

## Stop or kill work

Client disconnect cancels the upstream stream. To stop the underlying engine as an admin:

```bash
scripts/coire engines | jq '.[] | {id, model_id, node, state}'
scripts/coire engine unload "$ENGINE_ID"
```

Do not kill the MLX process manually unless the node agent itself is unavailable. A manual kill is
detected and recorded as an engine failure, but the admin unload path preserves lifecycle state.

## Configuration

The API accepts `COIRE_GATEWAY_WAIT_CEILING_S`, `COIRE_GATEWAY_KEEPALIVE_INTERVAL_S`,
`COIRE_GATEWAY_MAX_INFLIGHT_PER_ENGINE`, `COIRE_GATEWAY_RETRY_AFTER_S`, and
`COIRE_GATEWAY_ENGINE_REQUEST_TIMEOUT_S`. Keep-alive must remain below the ingress idle timeout.

## Roll back

Roll back the `coire-api` image, then downgrade Alembic revision `0003_gateway_usage` only if usage
history may be discarded. The migration touches no registry, copy, node, or engine rows. The legacy
`/api/v1/models` route remains available throughout rollback.
