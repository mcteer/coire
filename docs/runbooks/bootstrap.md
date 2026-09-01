# Runbook: Bootstrap Control Plane

> Network operations below that mention `.mesh` are the historical feature-000 rollback path.
> Current operation uses control DNS and the Studio-only `.fabric` mapping documented in
> `network-fabrics.md`.

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
curl -s http://127.0.0.1:8180/health | jq .status   # -> "healthy"
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
curl -s http://127.0.0.1:8180/health | jq   # aggregate + per-dependency latency
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
scripts/build-node-wheel.sh coire-edge-a     # builds and copies over control DNS
ssh mcteer@coire-edge-a.lab '~/coire-stage/apps/coire-node/install.sh --wheel-dir ~/coire-stage/dist'
```

Then the printed `sudo` steps to install and bootstrap the LaunchDaemon.

Check it:

```bash
TOKEN=$(security find-generic-password -w -s coire-node-tokens | jq -r '."coire-edge-a"')
curl -s -H "Authorization: Bearer $TOKEN" http://coire-edge-a.lab:9400/node/health | jq
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

### 2026-09-01 real-cluster evidence

Both Studios were checked over the Wi-Fi control fabric after reboot. The authenticated
`/node/health` response returned `200` on each node and the unauthenticated response returned
`401`; both reported `path: control` and `collection_budget_ok: true`:

| Node | Control address | `agent_cpu_percent` | `agent_rss_bytes` | `memory_committed_bytes` | `disk_free_bytes` |
| --- | --- | ---: | ---: | ---: | ---: |
| `coire-edge-a` | `192.168.4.11:9400` | 1.0 | 101,777,408 | 708,524,596 | 1,916,450,107,392 |
| `coire-edge-b` | `192.168.4.12:9400` | 1.5 | 99,155,968 | 0 | 1,908,641,757,184 |

The exact `apps/coire-node/uninstall.sh --dry-run` output listed only `/opt/coire/bin`,
`/opt/coire/python`, `/opt/coire/envs`, `/opt/coire/log`, `/opt/coire/models`, `/opt/coire/state`,
`/opt/coire/hf-cache`, and `/Library/LaunchDaemons/com.coire.node.plist`, with `/opt/coire` itself
left intact. Two hundred unauthenticated readiness probes per control address succeeded; measured
round-trip percentiles were edge-a `p50 20.609 ms / p95 108.807 ms` and edge-b `p50 22.039 ms /
p95 113.042 ms`. Latency is recorded evidence only; the former standalone sub-50 ms gate was
removed by the clarified architecture decision.

The Thunderbolt partition test remains pending because `sudo ifconfig bridge0 down` requires an
interactive operator password on the Studios; the attempted command made no network change.

The follow-up footprint check on 2026-09-01 confirmed the same allowlisted paths and running
LaunchDaemon on both hosts. `/opt/coire/envs/0.2.0/bin/python3` reported `mlx_lm 0.31.3` and
`huggingface_hub` imported successfully. Directory totals were edge-a:
`envs 400M`, `models 276M`, `python 70M`, `bin 35M`, `log 3.0M`, `hf-cache 148K`, `state 8.0K`;
edge-b: `envs 392M`, `models 291M`, `python 70M`, `bin 35M`, `log 4.0M`, `hf-cache 24K`,
`state 20K`. No files outside the uninstall allowlist were reported by the dry-run.

The control/data separation was also verified without changing routes: both Studios resolve the
core control address (`192.168.4.10`) through Wi‑Fi `en1`, while the Thunderbolt `bridge0` fabric is
active at `192.168.100.11` (edge-a) and `192.168.100.12` (edge-b). This confirms no wired path is
used for control or public egress; `bridge0` is reserved for Studio-to-Studio inference traffic.

The deployed v2 nodes intentionally run with `legacy_network_mode=false`. Consequently their
Wi‑Fi `.lab` listener is the authenticated control path (`path: control`), and no separate legacy
egress/fallback listener is started. The historical fallback-header checks in the pre-v2 quickstart
apply only to legacy-mode deployments; they are not a current control-fabric requirement.

The footprint command was rerun over SSH with `--keychain` on both Studios. Each output contained
only `/opt/coire/{bin,python,envs,log,models,state,hf-cache}`, the LaunchDaemon plist, and the two
System-keychain entries `coire-node-token` and `coire-hf-token`; `/opt/coire` itself remained.
The same check reported LaunchDaemon `state = running`, the expected FQDN/data hosts, and MLX
`0.31.3` on both nodes. A subsequent authenticated health sample reported
`collection_budget_ok: true` on edge-a; edge-b varied around the 2% CPU boundary while idle and
was observed returning to `true`, so a sustained pull-budget measurement remains open.

## CI: proving the shell check

`SC-008` requires that a deliberately-introduced shell fails CI with a message naming the
check. The fixture lives at `tests/fixtures/policy/bad.Dockerfile`:

```bash
docker build --platform linux/arm64 -f tests/fixtures/policy/bad.Dockerfile -t bad:test .
scripts/image-policy.sh bad:test tests/fixtures/policy/bad.Dockerfile
# -> policy: shell present in bad:test: /bin/sh   (exit 1)
```

The same script runs per-image in CI's `image-policy` job, over all seven built images.

### SC-008 execution evidence — 2026-09-01

Throwaway PR #22 (`spike/sc-008-shell-fixture`) added a pinned BusyBox `/bin/sh` copy to the
API image. The CI image-policy gate rejected it with the expected diagnostic:
`policy: shell present in coire-api:ci: /bin/sh` (exit 1). The fixture PR and branch were
closed and deleted after capture.

## See also

- [`models.md`](models.md) — adding, curating, retiring models; loading and unloading
  engines; clearing orphans; where model files live on a Studio.
