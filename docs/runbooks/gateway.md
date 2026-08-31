# Runbook: compatible inference gateway

The gateway is the only control-plane component permitted to request model generation. Bare engines
bind only to Studio loopback; the authenticated coire-node route carries gateway traffic to them. It serves
OpenAI-compatible `/v1/models` and `/v1/chat/completions`, plus Anthropic-compatible `/v1/messages`.
Callers use registry UUIDs; repository names, slugs, paths, and adapter suffixes are refused.

## See it

```bash
curl -sS http://127.0.0.1:8080/v1/models -H "authorization: Bearer $COIRE_TOKEN" | jq
curl -N http://127.0.0.1:8080/v1/chat/completions \
  -H "authorization: Bearer $COIRE_TOKEN" \
  -H 'content-type: application/json' \
  -d "{\"model\":\"$MODEL_ID\",\"stream\":true,\"messages\":[{\"role\":\"user\",\"content\":\"hello\"}]}"
```

Use the `Coire Gateway` Grafana dashboard. Correlate `request_id`, `model_id`, `engine_id`, and
`user_id` across gateway logs and traces. The important metrics are
`coire_gateway_request_duration_ms`, `coire_gateway_first_token_duration_ms`,
`coire_gateway_overhead_duration_ms`, `coire_gateway_queue_duration_ms`, `coire_gateway_inflight`, and
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
- `401`: the bearer is absent or invalid. Until feature 007 lands, the Keychain-backed admin
  bearer is the only credential the platform can verify.

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

Studio PF rules expose node control port 9400 to core but no longer expose 9500–9599. After an
upgrade, verify `sudo pfctl -a coire -sr` contains no pass rule for the engine range and that an
engine command line contains `--host 127.0.0.1`. Roll back by restoring the previous node/API
images together; never reopen the engine range as a one-sided workaround.

Claude Code and its VS Code/JetBrains integrations share Claude Code settings. Point them at Coire
with `ANTHROPIC_BASE_URL`, set the bearer through `ANTHROPIC_API_KEY`, and map a recognized Claude
model key to the registry UUID so the CLI sends Coire's safe identifier:

```json
{"modelOverrides":{"claude-sonnet-4-6":"<registry-model-uuid>"}}
```

This is client configuration, not a gateway alias: Coire continues to reject model names and only
resolves registry UUIDs.

## Roll back

Roll back the `coire-api` image, then downgrade Alembic revision `0003_gateway_usage` only if usage
history may be discarded. The migration touches no registry, copy, node, or engine rows. The legacy
`/api/v1/models` route remains available throughout rollback.
