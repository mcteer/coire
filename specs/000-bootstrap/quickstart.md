# Quickstart: Validating the Bootstrap Skeleton

**Feature**: `000-bootstrap`. Each scenario maps to a success criterion in [spec.md](spec.md).
Shapes are in [data-model.md](data-model.md); routes in [contracts/health-api.yaml](contracts/health-api.yaml);
the topology in [contracts/compose-topology.md](contracts/compose-topology.md).

## Prerequisites

On `coire-core`: OrbStack running, `uv 0.12.7`, `gh` authenticated. Three Keychain items:

```bash
security add-generic-password -a coire -s coire-postgres-password    -w "$(openssl rand -base64 32)"
security add-generic-password -a coire -s coire-key-signing-secret   -w "$(openssl rand -base64 48)"
security add-generic-password -a coire -s coire-node-tokens \
  -w "$(python3 -c 'import json,secrets;print(json.dumps({n:secrets.token_urlsafe(32) for n in ["coire-edge-a","coire-edge-b"]}))')"
```

On each Studio: the operator has run the one-time `sudo mkdir -p /opt/coire && sudo chown $USER /opt/coire`,
and stored that node's token in the **System** keychain (value copied from the JSON above):

```bash
sudo security add-generic-password -a coire -s coire-node-token -w "<token>" /Library/Keychains/System.keychain
```

Mesh names resolve on all three hosts (`deploy/cluster/hosts` applied — ADR-0002):

```bash
getent hosts coire-core.mesh coire-edge-a.mesh coire-edge-b.mesh   # 192.168.100.10 / .11 / .12
```

## 1. Clean bring-up — SC-001

```bash
cd deploy/compose
time ./coire-up                        # reads Keychain → env → docker compose up -d
curl -s http://127.0.0.1:8080/health | jq .status
```

**Expected**: `coire-up` returns within 3 minutes with every service healthy; `/health` through
nginx returns 200 with `"status": "healthy"` and one `services[]` entry each for `api`,
`postgres`, `mcp`, `scheduler`, `otel-collector`. `docker compose ps` shows `coire-migrate`
exited 0. No file under `deploy/compose/` contains a secret: `git status` is clean and
`grep -r "$(security find-generic-password -w -s coire-postgres-password)" .` finds nothing.

**Negative**: delete one Keychain item and re-run `coire-up` — it must abort naming the missing
item and start nothing (`docker compose ps` shows the previous state unchanged). Then run two
`coire-up` invocations concurrently: the second must exit immediately with
`bring-up already running` and the database must end up migrated exactly once
(`alembic_version` has one row).

## 2. Independent restart — SC-002, SC-003

```bash
for s in coire-api postgres coire-mcp coire-scheduler otel-collector docker-socket-proxy; do
  docker compose restart $s
  # poll /health every second for 30 s; every service other than $s must stay healthy
done
time docker compose restart coire-web && curl -sf http://127.0.0.1:8080/ready
```

**Expected**: during each restart, `/health` never reports any *other* service unhealthy;
`coire-api` reconnects to Postgres on its own; stopping `coire-mcp` entirely (`docker compose
stop coire-mcp`) leaves `/ready` on api at 200. `coire-web` is serving again in under 5 s.
Restarts succeed even though no secret file exists on the host (research R4).

## 3. Topology invariants — FR-006, FR-007

```bash
uv run pytest tests/integration/test_topology.py -v
docker compose exec coire-web /healthcheck --tcp postgres:5432 ; echo "exit=$?"   # must fail: no route
docker compose exec coire-api  /app/.venv/bin/python3 -c 'import socket;socket.create_connection(("postgres",5432),2)'  # must succeed
```

**Expected**: all assertions in `contracts/compose-topology.md` pass; the TCP connect from
`coire-web` to `postgres` fails while the same connect from `coire-api` succeeds (a plain HTTP
probe cannot distinguish "no route" from "not HTTP", hence `--tcp`); only `coire-scheduler` can
reach `docker-socket-proxy`.

## 4. Image policy and CI — SC-004, SC-005, SC-007, SC-008

```bash
gh workflow run ci.yml --ref feat/000-bootstrap && gh run watch
```

**Expected**: jobs `build`, `scan`, `sbom`, `image-policy`, `lint`, `test`, `integration` all
green on `ubuntu-24.04-arm`; every first-party image passes rules 1–7 in
`contracts/image-policy.md`; an SBOM artefact exists per image.

**SC-008 fixture**: on a throwaway branch add `COPY --from=busybox@sha256:… /bin/sh /bin/sh` to
`apps/coire-api/docker/api.Dockerfile`, push, and confirm `image-policy` fails with
`policy: shell present in coire-api: bin/sh`. Delete the branch.

**Local equivalent** (no CI): `./scripts/image-policy.sh coire-api` runs the same checks.

## 5. Node agent on a Studio — SC-006 *(manual, real cluster)*

```bash
# from core
ssh coire-edge-a.local 'bash -s' < apps/coire-node/install.sh      # installs only under /opt/coire + plist
TOKEN=$(security find-generic-password -w -s coire-node-tokens | jq -r '."coire-edge-a"')
curl -s -o /dev/null -w '%{http_code}\n' http://coire-edge-a.mesh:9400/node/health            # 401 without the token
curl -s -H "Authorization: Bearer $TOKEN" http://coire-edge-a.mesh:9400/node/health | jq '{cpu_percent, gpu_percent, thermal_state, memory_free_bytes, disk_free_bytes, agent_cpu_percent, agent_rss_bytes, collection_budget_ok, path}'
curl -s http://127.0.0.1:8080/health | jq '.nodes[] | select(.name=="coire-edge-a")'
```

**Expected**: 401 without the token; 200 with it, live figures, `path: "mesh"`,
`collection_budget_ok: true`, `agent_cpu_percent ≤ 2`, `agent_rss_bytes ≤ 150 MiB`; the node
appears in api's `/health` with `healthy: true`. Then 200 authenticated probes in a loop:
p95 round-trip over the mesh must be ≤ 50 ms (plan Technical Context); record p50/p95.

**Reboot**: `ssh coire-edge-a.local sudo reboot`; poll the mesh health URL. It must answer
unprompted within 2 minutes of the machine returning, and re-register (api `/health` shows
`last_seen_at` advancing) with **no login session** on the Studio.

**Footprint** (FR-012a/b): `apps/coire-node/uninstall.sh --dry-run` lists exactly what was
installed — `/opt/coire/**`, `/Library/LaunchDaemons/com.coire.node.plist`, and (from feature
001) **two** Keychain items, `coire-node-token` and `coire-hf-token` — and nothing else.
Feature 001 adds three directories under the prefix: `models/` (the store), `state/` (engine
and job caches) and `hf-cache/` (metadata scratch). `brew list` and `ls /usr/local/bin` are unchanged from before install.

**Fallback path** (FR-013a–c): request the Wi-Fi address without the header, then with it:

```bash
curl -s -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $TOKEN" http://coire-edge-a.local:9400/node/health                              # 403: egress without marker
curl -s -H "Authorization: Bearer $TOKEN" -H 'X-Coire-Path: fallback' http://coire-edge-a.local:9400/node/health | jq .path                          # "fallback"
```

The second request must appear as a WARNING in the agent's log and increment
`coire_node_fallback_requests_total`. Then, with the mesh link deliberately down
(`sudo ifconfig bridge0 down` on edge-a for 30 s), api's `/health` must still show the node
`healthy` via fallback, and the counter on api must increment.

Repeat §5 on `coire-edge-b` — its `core ↔ edge-b` path crosses edge-a's bridge, so this also
proves the one-hop mesh route.

## 6. Observability wiring — FR-014

```bash
docker compose logs otel-collector | grep -c 'ResourceSpans'     # > 0 after a few /health calls
```

**Expected**: spans from `coire-api` arrive at the collector. Nothing further is asserted; the
panel and alert are feature 009.

## 7. Teardown

```bash
./coire-down                     # compose down; volumes kept
./coire-down --purge             # also removes the postgres volume
```
