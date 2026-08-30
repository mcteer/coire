# Contract: Compose Topology on core

**Feature**: `000-bootstrap` · Enforced by `deploy/compose/compose.yaml` and checked by
`tests/integration/test_topology.py` (reads `docker compose config --format json` and asserts
this matrix).

## Services × networks

| Service | Image (digest-pinned) | `coire-edge` | `coire-db` | `coire-internal` | `coire-docker` | `coire-telemetry` | Long-lived | Healthcheck |
|---|---|:-:|:-:|:-:|:-:|:-:|:-:|---|
| `coire-web` | `coire-web` (Chainguard nginx) | ● | | | | ● | yes | static probe → `:8080/nginx-health` (local `return 200`, **not** a proxied path — a proxied probe would flip web unhealthy on every api restart) |
| `coire-api` | `coire-api` | ● | ● | ● | | ● | yes | python → `:8000/ready` |
| `coire-mcp` | `coire-mcp` (stub) | ● | | | | ● | yes | python → `:8001/ready` |
| `coire-scheduler` | `coire-scheduler` (stub) | | ● | | ● | ● | yes | python → `:8002/ready` |
| `coire-migrate` | `coire-migrate` | | ● | | | ● | **one-shot, exit 0** | none (completion gates api) |
| `postgres` | `postgres:17` | | ● | | | | yes | `pg_isready` |
| `docker-socket-proxy` | `tecnativa/docker-socket-proxy` | | | | ● | | yes | upstream |
| `otel-collector` | `otel/opentelemetry-collector-contrib` | | | | | ● | yes | derived image + static probe → `:13133` |

Absent by design: `cloudflared` (R12), `coire-ops` (feature 012), `coire-agent` (built, never
run on core — FR-017; the topology test asserts no service uses that image),
Prometheus/Loki/Tempo/Grafana/Alertmanager (feature 009).

Images come from `ghcr.io/mcteer/coire/<name>@sha256:…` as pushed by CI on `v*` tags and
recorded in `images.lock`; core needs `docker login ghcr.io` (via `gh auth token`) unless the
packages are public. `compose.override.dev.yaml` adds `build:` blocks for local iteration on
core and is never used for the production bring-up.

## Invariants the test asserts

- `coire-web` has **no** route to `postgres` (not on `coire-db`).
- `docker-socket-proxy` is reachable **only** from `coire-scheduler` (`coire-docker` has exactly
  those two members) and has `/var/run/docker.sock` mounted read-only with an explicit
  allowlist (`CONTAINERS=1`, `IMAGES=1`, `POST=1`; everything else `0`).
- Every long-lived service declares `healthcheck`.
- `coire-api` `depends_on`: `postgres: service_healthy`, `coire-migrate: service_completed_successfully`.
- `coire-web` `depends_on`: `coire-api: service_healthy`.
- No service `depends_on` `otel-collector` — collector loss must not fail requests.
- Every first-party service: `user` non-root, `read_only: true`, `tmpfs: [/tmp]`,
  `cap_drop: [ALL]`, `security_opt: [no-new-privileges:true]`, `restart: unless-stopped`.
- Third-party services: same hardening where the upstream supports it (`postgres` needs
  writable `PGDATA` volume and `tmpfs` for `/run/postgresql`; socket-proxy runs as root by
  upstream design and is confined to `coire-docker`).
- All `image:` values carry `@sha256:` digests; a bare tag fails the test.
- Secrets are declared with `environment:` sources (R4); a `file:` source fails the test.
- Exactly five networks exist, all `internal: true` except `coire-edge` (which is where
  `cloudflared` will attach in 007) — and even `coire-edge` publishes only `127.0.0.1:8080`
  from `coire-web` in 000, so nothing on core is reachable from the LAN except the mesh-side
  bring-up verification path.

## Secrets

| Secret name | Keychain service (`security find-generic-password -s`) | Consumed by |
|---|---|---|
| `postgres_password` | `coire-postgres-password` | `postgres`, `coire-api`, `coire-migrate`, `coire-scheduler` |
| `key_signing_secret` | `coire-key-signing-secret` | `coire-api` (declared; used from 007) |
| `node_tokens` | `coire-node-tokens` (JSON `{name: token}`) | `coire-api` (registration check) |

`coire-up` refuses to start if any Keychain item is absent, naming it (spec edge case). It reads
each into its own environment, runs `docker compose up -d`, and exits; nothing is written to
disk. `docker compose restart <svc>` works afterwards (verified, R4); a fresh `docker compose up`
must go through `coire-up` again.
