# Quickstart: Coire-Ops Confirmed Mutations

## Prerequisites

- Disposable compose integration stack with the tiny model and fake engine images.
- Admin browser identity and an ops-service secret supplied through compose secrets.
- `coire-ops` image built and digest-pinned; no shell/debug tooling in the image.

## Validate the normal confirmation flow

1. Start an ops conversation as an admin and ask to unload a known idle instance.
2. Confirm the response contains the exact operation, target, parameters, precondition, expiry, and
   a confirmation card; verify the instance is still ready.
3. Approve from the console. Verify the proposal becomes executed/stops the instance and the audit
   row names the human admin as actor and ops session as proposer.
4. Repeat with decline and verify no mutation occurs and the decline is audited.

## Validate token safety

1. Create a fresh proposal and capture its token only in test memory.
2. Change the echoed target or parameters and confirm; expect RFC 9457 conflict and no mutation.
3. Submit the exact request concurrently from two admin clients; expect one accepted execution and
   one single-use refusal.
4. Attempt replay after success and after configured expiry; expect refusal and audit rows.
5. Attempt confirmation using the ops-service credential and a non-admin identity; expect 403.

## Validate restart invalidation

1. Create a pending proposal and record its non-secret proposal id.
2. Restart only `coire-ops`.
3. Attempt confirmation with the prior token; expect restarted-session refusal.
4. Ask again and confirm the new-session proposal succeeds.

## Validate degraded mode and core isolation

1. Make the pinned admin model unavailable while leaving `coire-api` and `coire-ops` running.
2. Ask for cluster status; expect a factual response marked degraded and sourced from the control
   snapshot, with no inference request.
3. Ask for an action; expect an explicit degraded refusal and no proposal.
4. Restore the model; verify model-backed proposals resume without restarting the ops container.
5. Inspect core containers/images and the ops image: no engine process, weights, Metal library,
   filesystem/shell/git tools, Docker socket, Studio network, or user harness entrypoint is present.

## Automated gates

```bash
uv run ruff format --check && uv run ruff check
uv run mypy apps packages
uv run pytest -q -m 'not integration'
COIRE_INTEGRATION=1 uv run pytest -q tests/integration/test_ops_confirmations.py
pnpm -C apps/coire-web test && pnpm -C apps/coire-web lint && pnpm -C apps/coire-web build
uv run python -m coire_api.openapi --check
```

Run image policy, critical-CVE scan, and SBOM generation for `coire-agent-ops`; record non-secret
proposal ids/timestamps only in the feature review.
