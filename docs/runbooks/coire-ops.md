# Coire Ops

`coire-ops` is the sole harness on core. In administrator Chat it reads a bounded live snapshot
and can submit one exact reversible proposal. Set `COIRE_OPS_MODEL_SOURCE=anthropic` and
`COIRE_OPS_MODEL_ID` to the curated Sonnet registry UUID to use the pinned Sonnet 5.5 model.
The ops container stays on internal networks and receives only its scoped service token; coire-api
relays bounded model calls using its API-only Keychain credential. The relay shares the registered
model's daily token cap with ordinary Chat, reserves before provider I/O and settles only reported
usage. Set the source back to `studio`
to use the pinned verified Studio model through `/v1`. Only a human admin route can consume the
proposal token and execute the action.

## Observe

```bash
cd deploy/compose
docker compose ps coire-ops coire-api
docker compose logs --since 15m coire-ops coire-api
curl --fail http://127.0.0.1:8180/ready
```

Use the `Coire Container Runs` dashboard panels for proposal outcomes, bounded refusal reasons,
and degraded turns. Correlate logs by `conversation_id`, `proposal_id`, and `ops_session_id`.
Never copy a `confirm_token`, request body, container environment, or model output into logs or a
ticket.

## Stop or restart safely

Stopping or restarting `coire-ops` invalidates every pending proposal from its prior session:

```bash
docker compose stop coire-ops
docker compose start coire-ops
docker compose restart coire-ops
```

After restart, verify `/ready`, a new session registration log, and that an old confirmation is
refused with `session_restarted`, `revoked`, or `not_pending`. Do not revive the old proposal;
ask for a newly resolved action against current state.

## Rotate the service credential

Generate a new `coire_ops_` secret, replace the `coire-ops-service-token` login-Keychain item, and
run `deploy/compose/coire-up --no-build`. Both `coire-api` and `coire-ops` mount the same dedicated
secret and must be recreated together. This credential has read/propose/session scope only; it is
not an admin credential and cannot confirm.

## Degraded mode

If the configured model or relay is unavailable, status questions are answered deterministically
from the API snapshot and action-like questions are explicitly refused. Restore the Anthropic
credential/flag for Sonnet mode, or the verified pinned Studio instance for Studio mode; health
probing resumes model-backed turns without restarting `coire-ops`. Never configure a fallback
model on core. The relay only accepts `claude-sonnet-5-5`, at most 512 output tokens and the
read/propose/final-output tools. It does not accept caller-selected URLs or confirm a proposal.

## Roll back

Deploy the prior digest-pinned `coire-agent-ops` and matching API/web images, then recreate those
services. Pending proposals from the replaced session intentionally become unusable. Migration
0012 may be downgraded only after the old services are stopped and ops conversation/audit evidence
has been retained according to policy. A rollback must not add the Docker, database, edge, or
Studio networks to `coire-ops`.
