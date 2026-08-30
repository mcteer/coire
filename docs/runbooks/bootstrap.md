# Runbook: Bootstrap Control Plane

Operational surface added by feature 000. Full validation procedure:
`specs/000-bootstrap/quickstart.md`.

The control plane is eight containers on `coire-core`, published only on loopback. There is no
Cloudflare tunnel and no authentication yet — both are deferred until the platform faces
external traffic (`docs/adr/0001`).

## Bring up

One-time, on core:

```bash
scripts/coire-secrets-init.sh                     # creates 3 Keychain items
scripts/coire-secrets-init.sh --show-node-tokens  # per-node tokens for the Studios
sudo scripts/apply-mesh-hosts.sh                  # /etc/hosts mesh block (ADR-0002)
```

Every time:

```bash
deploy/compose/coire-up
curl -s http://127.0.0.1:8080/health | jq .status   # -> "healthy"
```

`coire-up` reads the three secrets from the Keychain, writes them 0600 into
`~/.coire/secrets`, and compose file-mounts them at `/run/secrets/`. Measured bring-up from
cold: **18 s** (SC-001 budget is 180 s).

Exit codes: `2` a Keychain item is missing (it names which, and starts nothing); `3` another
bring-up already holds the lock.

Local iteration builds images from source: `coire-up` (without `--no-build`) runs
`docker compose build` first. Production pulls digest-pinned images from ghcr; core needs
`docker login ghcr.io -u mcteer --password-stdin <<< "$(gh auth token)"` once if the packages
are private.

## Bring down

```bash
deploy/compose/coire-down            # stops everything, removes the secret files, keeps the DB
deploy/compose/coire-down --purge    # also destroys the coire-pgdata volume (asks first)
```

The secret files must outlive bring-up — a `file:` secret is a bind mount, so removing it
under a running container breaks `docker compose restart` — which is why `coire-down` is what
removes them, not `coire-up`.

## Restart one service

```bash
cd deploy/compose && docker compose restart <service>
```

Measured, restarting each service while polling `/health` for 25 s:

| Restarted | Other services disturbed | Notes |
|---|---|---|
| `coire-mcp` | none | `/health` goes `degraded`, api stays up |
| `coire-scheduler` | none | |
| `otel-collector` | none | nothing depends on it, by design |
| `docker-socket-proxy` | none | |
| `coire-api` | none | `/health` is *served by* api, so it 502s while it restarts |
| `postgres` | none | api reports `unhealthy` (503) then reconnects **without restarting** |

`coire-web` restart to serving: **0.53 s** (SC-003 budget 5 s), and it stays `healthy`
throughout an api restart because its healthcheck probes the local `/nginx-health`, not the
proxied `/ready`.

## Where to look

```bash
cd deploy/compose
docker compose ps                      # what is up
docker compose logs -f coire-api       # structured JSON logs
curl -s http://127.0.0.1:8080/health | jq   # aggregate + per-dependency latency
docker compose logs otel-collector | grep -c ResourceSpans   # telemetry arriving
```

`/health` semantics: `healthy` all good; `degraded` a non-critical dependency is down (HTTP
200 — the platform still works); `unhealthy` Postgres is unreachable (HTTP 503 — there is no
system of record). `/ready` is liveness only and checks nothing external.

## Roll back

Images are pinned in `deploy/compose/images.lock`. To roll back, put the previous digests back
and bring up again:

```bash
git diff HEAD~1 -- deploy/compose/images.lock   # what changed
$EDITOR deploy/compose/images.lock              # restore the previous digests
deploy/compose/coire-up --no-build
```

Alembic migrations are **not** automatically reversed. `0001_nodes` has a working
`downgrade()`, but check before assuming a rollback is safe:

```bash
docker compose run --rm coire-migrate -m alembic -c /app/alembic.ini downgrade -1
```

## Node agent

Installed on each Studio into `/opt/coire` and nothing else. One-time, on the Studio:

```bash
sudo mkdir -p /opt/coire && sudo chown "$USER" /opt/coire
sudo security add-generic-password -a coire -s coire-node-token \
     -w '<token>' /Library/Keychains/System.keychain
```

The **System** keychain, not the login keychain: the agent is a LaunchDaemon that starts at
boot with no login session, and the login keychain is locked then.

From core:

```bash
scripts/build-node-wheel.sh coire-edge-a     # builds and copies over the mesh
ssh mcteer@coire-edge-a.mesh 'apps/coire-node/install.sh --wheel-dir /opt/coire/dist'
```

Then the printed `sudo` steps to install and bootstrap the LaunchDaemon.

Check it:

```bash
TOKEN=$(security find-generic-password -w -s coire-node-tokens | jq -r '."coire-edge-a"')
curl -s -H "Authorization: Bearer $TOKEN" http://coire-edge-a.mesh:9400/node/health | jq
```

`401` without the token. The egress (Wi-Fi) listener returns `403` unless the request carries
`X-Coire-Path: fallback`; every fallback increments `coire_node_fallback_requests_total` and
logs at WARNING, because the egress path is ~30× slower and must never become the steady state
silently.

Footprint audit — what is actually on the node:

```bash
apps/coire-node/uninstall.sh --dry-run
```

Remove it entirely with `apps/coire-node/uninstall.sh --keychain`.

## CI: proving the shell check

`SC-008` requires that a deliberately-introduced shell fails CI with a message naming the
check. The fixture lives at `tests/fixtures/policy/bad.Dockerfile`:

```bash
docker build --platform linux/arm64 -f tests/fixtures/policy/bad.Dockerfile -t bad:test .
scripts/image-policy.sh bad:test tests/fixtures/policy/bad.Dockerfile
# -> policy: shell present in bad:test: /bin/sh   (exit 1)
```

The same script runs per-image in CI's `image-policy` job, over all seven built images.
