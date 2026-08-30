# Research: Bootstrap Control Plane Skeleton

**Feature**: `000-bootstrap` · **Date**: 2026-08-29

Every decision below was resolved by measurement on the real cluster or the real registries,
not by assumption. Where a probe changed a decision, the probe result is recorded.

## R1. Runtime base image for first-party Python services

**Decision**: Multi-stage build. Builder is the official `python:3.13-slim-bookworm` image with
the `uv` binary copied in from `ghcr.io/astral-sh/uv:0.12.7`; the runtime is
`gcr.io/distroless/base-debian12:nonroot` with the builder's `/usr/local` (interpreter) and a
`--relocatable` venv copied across. Both images pinned by digest. Entry point is the venv's
`python3`; healthchecks use that interpreter (`urllib.request`), since there is no shell or curl.

**Rationale**: The constitution pins Python 3.13. Neither off-the-shelf distroless Python base
satisfies that: `gcr.io/distroless/python3-debian12` ships **Python 3.11.2** (probed), and
Chainguard's free tier is `latest`-only at **3.14.7** (probed) with no pinnable 3.13 tag. Copying
a Debian-bookworm-built 3.13 onto `base-debian12` (same glibc) was built and run on core:
`py 3.13.11 arch aarch64 uid 65532`, `/bin/sh` absent, 40 MB.

**Alternatives considered**: `python3-debian12` — wrong Python version. Chainguard `python` —
unpinnable on the free tier; a paid tier was not considered worth it for one constraint.
`cc-debian12` — lacks libraries the interpreter needs; `base-debian12` is the correct minimum.
Alpine/musl — rejected by the constitution (distroless/Chainguard/scratch only).

## R2. Web image (nginx, sole ingress)

**Decision**: `cgr.dev/chainguard/nginx` pinned by digest, serving the built SPA and proxying
`/api`, `/v1`, `/mcp`, `/health`, `/ready` to `coire-api` / `coire-mcp` with
`proxy_buffering off` and long read timeouts on streaming paths. Healthcheck via a tiny
statically-linked probe binary built in a Go build stage (`CGO_ENABLED=0`) and copied in.

**Rationale**: `nginxinc/nginx-unprivileged:alpine-slim` contains a shell, which FR-004 forbids
for first-party images. Chainguard nginx is shell-less. It therefore has no curl either, so the
healthcheck needs a binary of its own; a 20-line static Go probe is smaller and more auditable
than pulling a third-party curl build.

**Alternatives considered**: nginx-unprivileged alpine — has `/bin/sh`. Copying a curl binary
from `curlimages/curl` — its static-ness is not guaranteed across versions.

## R3. Third-party images and the scope of FR-004

**Decision**: FR-004 (no shell, non-root, read-only rootfs, cap_drop) applies in full to
**first-party** images (`coire-api`, `coire-mcp`, `coire-scheduler`, `coire-migrate`,
`coire-web`, `coire-agent`). Third-party images (`postgres:17`, `tecnativa/docker-socket-proxy`,
`otel/opentelemetry-collector-contrib`) are pinned by digest, CVE-scanned, and hardened through
compose (non-root where the upstream supports it, `read_only` with explicit `tmpfs`,
`cap_drop: [ALL]`, `no-new-privileges`), but their upstream shells are accepted. The spec is
amended to say so (FR-004, SC-004).

**Rationale**: The constitution pins **Postgres 17**. Chainguard's free `postgres` is
`latest`-only and cannot be pinned to 17; the official `postgres:17` image contains a shell. A
literal "every production image" reading would force either an unpinned major version or a
custom Postgres build, both worse than accepting an upstream shell in a container that is
non-root, read-only, capability-dropped and reachable only on `coire-db`.

**Alternatives considered**: Chainguard postgres — unpinnable major. Building Postgres from
scratch — disproportionate. Waiving hardening for third-party images entirely — rejected; the
compose-level controls still apply.

## R4. Secrets: Keychain → compose, without touching disk

**Decision**: `coire-up` reads each secret from the macOS Keychain (`security
find-generic-password -w -s <name>`) into **its own process environment** and runs
`docker compose up -d`. Compose secrets are declared with `environment:` sources and appear in
containers as files under `/run/secrets/`. No secret file is ever written to the host. FR-011 is
amended to match.

**Rationale**: FR-011 as written ("write a 0600 file, delete after bring-up") was tested on core
and **breaks SC-002**: with Compose 5.1.2 on OrbStack, a `file:`-sourced secret is a bind mount,
and deleting the source makes `/run/secrets/<name>` vanish from the *running* container
immediately; `docker compose restart` then fails (`service is not running`) and `up -d` fails
(`bind source path does not exist`). An `environment:`-sourced secret was tested next: readable
after the variable is gone from the shell, **still readable after `docker compose restart`**, and
present nowhere on host disk. Same intent, strictly better letter.

**Amended twice during implementation.** Two further constraints were measured only by running
this on both platforms:

1. Compose **rejects `environment:` secret sources for any `read_only` service** — "`file` is
   the sole supported option" — and every first-party service is read-only. The
   environment-sourced design above is therefore unbuildable, and the original file-based
   design was right; what was wrong was *deleting* the file after bring-up. The files now live
   for the lifetime of the stack in a 0700 directory outside the repository and are removed by
   `coire-down`.
2. A compose file-secret is a **bind mount that preserves host ownership**, so a 0600 file
   written by the invoking user is unreadable to a container running as uid 65532. macOS hides
   this behind uid mapping; on Linux every secret-consuming service failed to start with
   `Permission denied: /run/secrets/postgres_password`. The files are therefore 0644 and the
   *directory* is 0700 — host confidentiality comes from the directory no other user can
   traverse, and the file mode governs only what the container can read once the daemon has
   mounted it.

**Alternatives considered**: RAM disk for the secret file — works, but adds a mount to manage
and still leaves a file. Environment variables in containers — forbidden by the constitution
(file-mounted only), and rejected by compose for read-only services anyway. `docker cp` at
start — fragile across recreates.

## R5. Node agent Python and install footprint

**Decision**: `uv` installed to `/opt/coire/bin` (`UV_INSTALL_DIR`), a pinned CPython 3.13
provisioned by uv into `/opt/coire/python` (`UV_PYTHON_INSTALL_DIR`), the node agent's venv at
`/opt/coire/envs/<version>/` with `/opt/coire/envs/current` as the active symlink. `/opt/coire`
is created once by the operator with sudo and owned by the service user; everything beneath it is
managed by `deploy/` scripts without sudo. Nothing is installed outside `/opt/coire` except the
launchd plist and a Keychain item.

**Rationale**: Studios have Homebrew Python **3.14.7** and no 3.13 (probed); the constitution
pins 3.13, so the agent must bring its own. Confining everything to `/opt/coire` satisfies
FR-012a/b (enumerable, removable, nothing general-purpose) and matches the constitution's
`/opt/coire/envs` with symlink flip. `uv python list` confirms `cpython-3.13.15-macos-aarch64`
is available for download.

**Alternatives considered**: Homebrew `python@3.13` — installs into `/opt/homebrew`, not
enumerable as Coire's, and adds Homebrew to the platform's dependency surface on the Studios.
System Python — 3.14, wrong version.

## R6. Node agent as a launchd service, and its token

**Decision**: A **LaunchDaemon** (`/Library/LaunchDaemons/com.coire.node.plist`, `RunAtLoad`,
`KeepAlive`) with `UserName` set to the operator account, so it starts at boot without a login
session. The per-node token is stored in the **System keychain**
(`/Library/Keychains/System.keychain`), which is unlocked at boot; the login keychain is not.

**Rationale**: SC-006 requires the agent to answer within 2 minutes of a reboot, unprompted.
A LaunchAgent needs a logged-in user; a LaunchDaemon does not. The login keychain is locked
before login, so a daemon reading a per-node token from it would fail exactly at boot.

**Open for feature 011**: OrbStack's Docker socket is per-user (`~/.orbstack/run/docker.sock`)
and OrbStack is a user-session app; whether it is available at boot without login is 011's
problem, and is why the daemon runs as the operator account rather than a dedicated service
user for now.

## R7. Node metrics without privileged helpers

**Decision**: CPU, memory and disk via `psutil`. GPU utilisation via IOKit's `IOAccelerator`
performance statistics (`ioreg -r -c IOAccelerator`, "Device Utilization %"), which is readable
without root. Thermal state via `ProcessInfo.thermalState` semantics exposed through IOKit.
Sampling interval defaults to 5 s, tuned to a budget of ≤2 % of one core and ≤150 MB RSS,
which the agent reports about itself (FR-012c).

**Rationale**: `powermetrics` gives the best GPU numbers but requires root and would mean a
continuously running privileged helper, which spec 009's FR-006b forbids. IOAccelerator is
unprivileged and sufficient for utilisation. The budget honours the standing constraint that the
Studios' compute is reserved for inference.

**Alternatives considered**: `powermetrics` via sudoers — privileged helper, rejected.
Metal performance counters — needs a process on the GPU; overkill.

## R8. Name resolution on the Thunderbolt mesh

**Decision**: A managed block in `/etc/hosts` on all three hosts, generated from
`deploy/cluster/hosts` by the node-prep and core bring-up scripts, mapping `<host>.mesh` names to
`192.168.100.x` addresses. All platform configuration references `*.mesh` names; no service
config contains an IP. Recorded as ADR-0002 because the constitution says "never raw IPs in any
config", and this file is the one place the mesh's addresses live.

**Rationale**: The mesh is unrouted and never reaches the UDM, so UniFi DNS cannot serve it.
mDNS was tested and is **non-deterministic**: from core, `coire-edge-a.local` currently resolves
to a `169.254.x` link-local address, and from edge-a `coire-core.local` does the same — neither
the mesh address nor Wi-Fi, and it changes as interfaces come and go. A hosts file applied from
`deploy/` is deterministic, is not "hand-edited config on a node", and keeps IPs out of every
service config.

**Alternatives considered**: mDNS — measured non-deterministic. Registration-time discovery
(node tells core its mesh address) — good for core→node, but the node still needs core's mesh
name to register in the first place. A DNS container on core — the mesh must work when core is
down (feature 020).

## R9. Mesh-first with alerted Wi-Fi fallback (FR-013a–c)

**Decision**: The node agent binds two listeners — the mesh address and the egress address.
Requests on the egress listener are accepted only when they carry `X-Coire-Path: fallback`;
each one increments a `coire_node_fallback_requests_total` counter and logs at WARNING. The
control plane's node client tries `<host>.mesh` first and, on connect failure, retries
`<host>.local` with the fallback header. Alerting on the counter is feature 009.

**Rationale**: Measured on the final chain topology: mesh 12.0–12.6 Gb/s at 0.85–1.37 ms;
Wi-Fi 0.4 Gb/s at 23–29 ms. The mesh is a chain, so losing the middle node partitions it; an
absolute prohibition on egress would turn that into total loss. Making the fallback explicit,
counted and logged is what keeps it from becoming the silent steady state.

## R10. CI: runners, scanning, SBOM, image-policy checks

**Decision**: GitHub Actions on `ubuntu-24.04-arm` (GA, free for public repos, 4 vCPU).
Images built natively for `linux/arm64` only — no QEMU, no amd64. Trivy for CVE scanning
(fail on CRITICAL), Syft for SBOM (SPDX JSON, attached to the tag). Image-policy job per
first-party image: `docker run --entrypoint /bin/sh <img> -c true` must fail; the exported
filesystem must contain none of `/bin/sh`, `/bin/bash`, `/bin/ash`, `apt`, `apk`, `pip`;
`docker inspect` must show a non-root `User`. Lint/type/test: `ruff`, `mypy --strict`, `pytest`;
web `eslint`, `tsc`, `vitest`.

**Rationale**: Native arm64 runners remove the constitution's amd64-for-CI clause entirely — the
image that runs is the image that is tested. The policy checks are what make FR-004 and SC-004
mechanical rather than aspirational (SC-008 requires a deliberately-introduced shell to fail CI
with a message naming the check).

**Alternatives considered**: Grype — equivalent; Trivy chosen for its single-binary filesystem
mode. Building amd64 too — no consumer for it now that arm64 runners exist.

## R11. Health model

**Decision**: Two endpoints on every long-lived first-party service. `/ready` is a liveness
probe — process up, returns 200 with no dependency checks; used by compose healthchecks.
`/health` on `coire-api` is the aggregate: it probes Postgres (`SELECT 1`), `coire-mcp` and
`coire-scheduler` (`/ready` over the networks api shares with them), the collector's health
extension on `:13133`, and reports last-seen for each registered node. `coire-web` proxies
`/health` and `/ready` to `coire-api`. Shapes are Pydantic models in `coire-core`.

**Rationale**: FR-009 distinguishes "process up" from "dependencies ready"; collapsing them makes
a healthy-but-isolated service look down and blocks compose dependency gating on transient
faults. Spec 009 later consumes the same shapes.

## R12. Deferred to later features (user decisions, 2026-08-29)

- **Cloudflare tunnel**: not stood up. The spec's assumption that a tunnel exists was wrong and
  is corrected. `cloudflared` is absent from the compose project until the platform is ready for
  external traffic (feature 007 at the earliest). Verification is LAN-only, over the mesh.
- **Authentication**: none on any route. The application is built so auth **wires in without
  restructuring**: a single FastAPI dependency (`require_principal`) is declared and returns an
  anonymous principal; every route depends on it; feature 007 replaces its body. Node
  registration uses a static per-node token until feature 005 issues registration tokens.
  Both recorded in ADR-0001 as a time-boxed exception to Principle IV.

## R13. Constitution VI sequencing

**Decision**: `coire-api` and `coire-node` export OTLP traces and metrics to the collector
(FR-014); no dashboard panel or alert rule ships in 000, because the backends that would hold
them are feature 009. Recorded in Complexity Tracking as a sequencing deviation, not a waiver:
009's acceptance includes a bootstrap-health panel and the node-down alert.
