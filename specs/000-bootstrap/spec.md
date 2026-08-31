# Feature Specification: Bootstrap Control Plane Skeleton

**Feature Branch**: `000-bootstrap`

**Roadmap ID**: 000 · bootstrap (Phase 0 — Foundation)

**Created**: 2026-08-29

**Status**: Draft

**Input**: User description: "uv workspace with `coire-core`, `coire-api`, `coire-node`, `coire-agent`, `coire-web` stubs, distroless multi-stage Dockerfiles for api/mcp/scheduler/migrate/web/agent, `deploy/compose/` bringing up every service as its own container on per-concern networks with the docker-socket-proxy, CI building, scanning, and SBOM-ing images and running lint + tests."

## Overview

This feature establishes the skeleton every later feature builds on: a `uv` workspace with the five packages, one minimal container image per control-plane service, a compose project on core that brings them all up on per-concern networks, and a CI pipeline that builds, scans, and SBOMs each image while running lint and tests. No inference, no models, no auth beyond a health endpoint — the deliverable is a running, observable, independently-restartable skeleton that proves the containerisation rules of Principle II-a hold before any real functionality lands.

## Clarifications

### Session 2026-08-29

- Q: Does the bootstrap include a working `coire-node` on a Studio, or only the package stub? → A: A minimal but genuinely running node agent — a launchd-managed FastAPI process serving `/health` and reporting node identity, memory total/free, and disk free. It spawns no engines in this feature; engine lifecycle arrives in 001.
- Q: What authenticates the control plane in this feature, given auth is feature 007? → A: Nothing (confirmed 2026-08-29: no auth until people are ready to use it). `/health`, `/ready` and node registration are the only control-plane routes (the node agent's own `/node/health` is the one bearer-authenticated surface — FR-013, ADR-0001). The application MUST be built so auth wires in without restructuring — one declared auth dependency that every route uses and that 007 replaces — but MUST NOT ship a placeholder that looks like protection. `coire-node` registration uses a static per-node token from the Studio's System keychain, which 005 replaces with issued registration tokens. Recorded as a time-boxed exception to Principle IV in ADR-0001.
- Q: Must the observability stack be part of bootstrap, or does it wait for feature 009? → A: Only the OTel Collector container plus OTLP export wiring from `coire-api` ships here, so every later feature has somewhere to send spans. Prometheus/Loki/Tempo/Grafana/Alertmanager and dashboards are feature 009.
- Q: Does `cloudflared` and public exposure belong in bootstrap? → A: No, and there is no tunnel at all yet (confirmed 2026-08-29). `cloudflared` is absent from the compose project until the platform is ready for external traffic; `coire-web` publishes only on core's loopback. Access policies, WAF, and any public route land with feature 007. Verification is LAN/mesh-only. Recorded in ADR-0001.
- Q: How is "no production image contains `/bin/sh`" verified? → A: A CI job runs `docker run --rm --entrypoint /bin/sh <image> -c true` per image and requires a non-zero exit, plus an image-filesystem scan asserting absence of `/bin/sh`, `/bin/bash`, and any package manager. The check is a required status on every image tag.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Operator brings the whole control plane up on core (Priority: P1)

An operator on core runs a single command and gets every control-plane service running as its own container, with a health endpoint reachable through the same nginx ingress that will later face Cloudflare. This is the foundation: nothing else in the roadmap can be developed or demonstrated until the compose project comes up cleanly.

**Why this priority**: Every subsequent feature is delivered as a change to this compose project. Without it there is no place to put code.

**Independent Test**: On core, bring the project up from a clean state and request `/health` through the nginx ingress; a healthy JSON response with per-service status is proof of value on its own.

**Acceptance Scenarios**:

1. **Given** a clean core with OrbStack running and secrets present in Keychain, **When** the operator runs the documented bring-up command, **Then** every service reaches a healthy state and `/health` through nginx returns 200 with each service reporting healthy.
2. **Given** a running control plane, **When** the operator inspects the container list, **Then** each service is a separate container running exactly one process, and no two services share an image.
3. **Given** a running control plane, **When** the operator inspects networks, **Then** each container is attached only to the networks its role requires, and `coire-web` has no route to Postgres.

---

### User Story 2 - Operator restarts one service without disturbing the others (Priority: P1)

An operator restarts a single service and observes that no other service fails, and that dependents reconnect on their own. This is the observable proof of Principle II-a.

**Why this priority**: The one-service-one-container rule is a constitutional guarantee that is cheap to hold now and very expensive to retrofit later.

**Independent Test**: Restart each service in turn while polling `/health`, and confirm no unrelated service ever reports unhealthy.

**Acceptance Scenarios**:

1. **Given** a healthy control plane, **When** the operator restarts `coire-api`, **Then** `coire-web`, Postgres, and the collector stay healthy throughout and `coire-api` returns to healthy without manual intervention.
2. **Given** a healthy control plane, **When** the operator restarts Postgres, **Then** `coire-api` reconnects on its own within its healthcheck window rather than requiring a restart.
3. **Given** a healthy control plane, **When** the operator stops `coire-mcp` entirely, **Then** chat-path health is unaffected.

---

### User Story 3 - CI rejects an image that violates the container rules (Priority: P2)

A contributor opens a pull request whose image gains a shell, runs as root, or carries a critical CVE, and CI fails with a message naming the specific rule broken.

**Why this priority**: The image rules only hold if they are enforced mechanically; a documented rule with no gate decays within weeks.

**Independent Test**: Push a branch that deliberately adds a shell to one image and confirm CI fails on the shell check specifically.

**Acceptance Scenarios**:

1. **Given** a pull request, **When** CI runs, **Then** every image is built, scanned, and SBOM-published, and lint plus tests run.
2. **Given** an image modified to include `/bin/sh`, **When** CI runs, **Then** the build fails with a message identifying the shell check.
3. **Given** an image with a critical CVE in a dependency, **When** CI runs, **Then** the scan job fails and names the CVE and package.

---

### User Story 4 - Node agent on a Studio reports in (Priority: P2)

An operator installs the node agent on a Studio, and it starts under launchd, survives a reboot, and answers a health probe with its identity and resource totals.

**Why this priority**: Feature 001 needs a live node to attach engine lifecycle to; standing the agent up now separates "the process runs and survives reboot" from "the process manages engines".

**Independent Test**: Install on one Studio, reboot the machine, and confirm the agent answers `/health` unprompted afterwards.

**Acceptance Scenarios**:

1. **Given** a Studio with the node agent installed, **When** the operator queries its health endpoint from core by mesh name with the per-node token, **Then** it returns 200 with node id, live CPU and GPU utilisation, total and free memory, and free disk; without the token it returns 401.
2. **Given** a running node agent, **When** the Studio is rebooted, **Then** the agent is running again without manual intervention.
3. **Given** a peer reachable over the mesh, **When** a platform component contacts it, **Then** the mesh path is used.
4. **Given** the mesh path to a peer is unavailable, **When** a platform component contacts it, **Then** the egress path is used, the fallback is recorded, and an alert is raised.

---

### Edge Cases

- The bring-up command runs when a required secret is missing from Keychain: bring-up MUST abort with a message naming the missing secret, and MUST NOT start a partially-configured control plane.
- Postgres is slow to accept connections on first boot: dependent services MUST wait on the health condition rather than crash-looping, and MUST reach healthy once Postgres is ready.
- Bring-up MUST NOT write any secret to host disk at any point, including on failure; a `file:`-sourced compose secret is a defect (verified to break `docker compose restart` on OrbStack — research R4).
- Two operators run bring-up concurrently: the second MUST fail cleanly rather than produce a half-migrated database.
- A migration fails: the one-shot migrate service MUST exit non-zero, and the API MUST NOT start against a partially-migrated schema.
- The node agent starts before the network is up after a reboot: it MUST retry registration rather than exit.
- The middle node of the mesh chain fails: the two outer hosts MUST reach each other over the egress path rather than treating each other as down, with the degradation alerted. This is the accepted cost of a chain topology; a triangle would need spanning tree to avoid a broadcast loop.
- An image builds for the wrong architecture: CI MUST reject any image whose arm64 variant is missing, since arm64 is what actually runs.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The repository MUST be a single `uv` workspace containing `packages/coire-core`, `apps/coire-api`, `apps/coire-node`, `apps/coire-agent`, and `apps/coire-web`, with a committed lockfile.
- **FR-002**: `coire-core` MUST define the shared settings and health-response models used by the API and node agent, and MUST be the only place those shapes are declared.
- **FR-003**: Each control-plane service — API, MCP, scheduler, migrate, web — MUST build to its own image with its own tag; no image may serve two roles.
- **FR-004**: Every first-party image (`coire-api`, `coire-mcp`, `coire-scheduler`, `coire-migrate`, `coire-web`, `coire-agent`) MUST run as a non-root user, with a read-only root filesystem, all capabilities dropped, and no shell or package manager present. Third-party images (Postgres, socket proxy, collector) MUST be pinned by digest, CVE-scanned, and hardened through compose to the extent the upstream supports; their upstream shells are accepted because the constitution pins Postgres 17 and no shell-less, version-pinnable Postgres 17 image is available.
- **FR-005**: Every long-lived service MUST declare a healthcheck, and dependents MUST start only once their dependencies report healthy.
- **FR-006**: The compose project MUST place each container only on the networks its role requires, with separate networks for edge, database, internal, docker-socket, and telemetry concerns.
- **FR-007**: The Docker socket MUST be reachable only through a socket proxy with an explicit allowlist, and only by the scheduler.
- **FR-008**: nginx MUST be the sole ingress, serving the built SPA and reverse-proxying API routes, with response buffering disabled on streaming paths.
- **FR-009**: The system MUST expose an unauthenticated `/health` reporting per-service status and a `/ready` distinguishing "process up" from "dependencies ready".
- **FR-010**: Database migrations MUST run in a one-shot service that exits zero on success, and MUST NOT run inside a long-lived service.
- **FR-011**: Secrets MUST be read from the macOS Keychain at bring-up and written into a mode-0700 directory outside the repository, from which compose file-mounts them into containers at `/run/secrets/`. They MUST NOT be written into the repository or into any image, MUST be removed by bring-down, and `docker compose restart` of any service MUST work while the stack is up. The files themselves MUST be readable by the container user: a compose file-secret is a bind mount that preserves host ownership, so a 0600 file is unreadable to a service running as a non-root uid on Linux. Confidentiality is provided by the directory, which no other host user can traverse. (Amended twice during implementation: environment-sourced secrets are rejected outright for read-only services, and 0600 files break every secret-consuming service on Linux — see research R4.)
- **FR-012**: `coire-node` MUST run under launchd on each Studio, survive reboot, and serve a health endpoint reporting node identity, live CPU and GPU utilisation, memory total and free, and disk free.
- **FR-012a**: Node preparation MUST install only what the node agent and engines require, into a self-contained versioned location, and MUST NOT install general-purpose developer tooling, background services, or anything not required to serve inference. The Studios' compute is reserved for inference.
- **FR-012b**: Anything installed on a Studio MUST be enumerable and removable, so the node's footprint can be audited against what the platform actually needs.
- **FR-012c**: The node agent's own steady-state resource use MUST stay within a configured budget and MUST be reported alongside the node's metrics.

> **Superseded by feature 022:** FR-013 through FR-013c below describe the topology delivered by
> bootstrap and remain its historical acceptance record. Feature 022 replaces mesh-first control
> traffic and alerted Wi-Fi fallback with a primary isolated-VLAN control fabric and a separate
> Studio-only Thunderbolt data fabric.

- **FR-013**: `coire-node` MUST authenticate callers of its own endpoints by requiring the per-node token as a bearer credential (the same static token it presents to core at registration), and MUST serve platform traffic on the Thunderbolt mesh interface, rejecting platform requests arriving on the internet-egress interface unless they carry the explicit fallback marker (FR-013b).
- **FR-013a**: Platform components MUST prefer the Thunderbolt mesh for all node-to-node traffic and MUST use it whenever the mesh path to a peer is available.
- **FR-013b**: When a peer is unreachable over the mesh, a component MUST be able to fall back to the egress interface rather than treat the peer as lost. The mesh is a chain, so a middle-node failure partitions it, and an absolute prohibition would convert a survivable partition into total loss.
- **FR-013c**: Any fallback to the egress path MUST be recorded as a metric (`coire_fallback_requests_total`) and a WARNING log line, and MUST NOT be silent, because sustained operation on the slow path would otherwise go unnoticed. The alert rule on that metric is delivered by feature 009, which owns the alerting backend (ADR-0003).
- **FR-014**: `coire-api` MUST export OpenTelemetry traces via OTLP to a local collector container.
- **FR-015**: CI MUST build every image, scan it and fail on critical findings, publish an SBOM per tag, verify absence of a shell, verify non-root, and run lint and tests.
- **FR-016**: CI MUST build the arm64 image variant and MUST fail if it is absent.
- **FR-017**: `coire-agent` MUST build as an image in this feature but MUST NOT be started by the compose project on core, since core runs no user harness.

### Key Entities

- **Node**: A declared Studio. Identity, DNS name, role, memory total, disk total, registration token, last-seen timestamp, reachability state.
- **Service Health**: Per-service status reported by `/health` — name, healthy flag, dependency states, version tag.
- **Image Tag**: A built artefact — service name, git tag, architecture, digest, SBOM reference, scan verdict.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A clean bring-up on core reaches all-services-healthy within 3 minutes, unattended, from a single documented command.
- **SC-002**: Restarting any one service leaves every other service healthy throughout, verified for each service in turn.
- **SC-003**: `coire-web` restarts and returns to serving in under 5 seconds.
- **SC-004**: No first-party image contains `/bin/sh`, `/bin/bash`, or a package manager, verified mechanically in CI for 100% of first-party images; 100% of all images are digest-pinned and CVE-scanned.
- **SC-005**: Every image runs as a non-root user with a read-only root filesystem, verified in CI for 100% of images.
- **SC-006**: A node agent on a Studio answers a health probe from core by DNS name, and does so again unprompted within 2 minutes of a reboot.
- **SC-007**: CI completes build, scan, SBOM, lint, and tests on a pull request, and fails closed on any critical CVE.
- **SC-008**: A deliberately-introduced shell in any image causes CI to fail with a message naming the shell check.

## Assumptions

### Verified environment (probed 2026-08-29)

| Fact | Value | Consequence |
|---|---|---|
| core | `coire-core`, Mac16,11 M4 Pro, 24 GB, 460 GB disk | Matches the architecture's control-node budget |
| Studio A | `coire-edge-a`, Mac15,14 M3 Ultra, 256 GB, 80 GPU cores, 1.8 TB disk | Rank 0, largest single-node models |
| Studio B | `coire-edge-b`, Mac15,14 M3 Ultra, 256 GB, 60 GPU cores, 1.8 TB disk | Rank 1, pinned admin model, image worker |
| macOS | 26.6.2 (25G83) on all three | Above the 26.2 floor JACCL RDMA requires |
| Thunderbolt | Chain `core — edge-a — edge-b`, managed bridge, flat 192.168.100.0/24 | Measured 12.6 Gb/s / 0.85 ms direct; deliberately not a triangle (L2 loop) |
| Already on core | OrbStack, Docker 29.4.0, uv 0.12.7, Python 3.14.7 | Bring-up prerequisites met on core |
| Already on both Studios | OrbStack 2.2.3 running with Docker 29.4.0 and a live socket; Homebrew 6.0.20; Python 3.14.7 | Container runtime for feature 011 is already in place |
| Absent on both Studios | `uv`, the MLX toolchain, `mflux`, `/opt/coire` | Must be installed by this feature's node preparation |
| Python on Studios | 3.14.7 via Homebrew; no 3.13 | The node agent MUST provision its own pinned 3.13 runtime rather than relying on the system one |

- Hosts are addressed by the names above, which are the deployed machine names and are used consistently across the architecture doc, the roadmap, and these specs.
- Network preparation (roadmap 000a) is only partly complete. Done: the Thunderbolt mesh is cabled and the three nodes resolve each other by their `.local` names. Not done: all three sit on a flat 192.168.4.0/24 rather than the planned `lab` VLAN, and RDMA enablement plus the JACCL hostfile are outstanding. This feature depends on none of those; feature 006 depends on the RDMA work.
- OrbStack is installed and running on core and on both Studios (verified 2026-08-29). This feature therefore does not install it; feature 011's container runtime prerequisite is already satisfied. This feature starts no containers on a Studio.
- There is no Cloudflare tunnel and no authentication in this feature (user decision, 2026-08-29). Both are deferred until the platform is ready for external traffic and real users, and are recorded in `docs/adr/0001-defer-auth-and-edge-until-external-traffic.md`. The application declares the auth seam so feature 007 wires in without restructuring.
- Name resolution on the Thunderbolt mesh uses a managed `/etc/hosts` block generated from `deploy/cluster/hosts` (`<host>.mesh` names), because the mesh is unrouted and mDNS was measured to be non-deterministic. Recorded in `docs/adr/0002-mesh-name-resolution-via-managed-hosts-file.md`.
- No authentication exists on control-plane routes in this feature; only `/health` and `/ready` are served. This is a deliberate, time-boxed exception to Principle IV to be recorded as an ADR.
- Postgres runs as a container on core with a named volume. Backups are out of scope until operations work lands.
- The observability stack beyond the OTel Collector is feature 009; this feature only guarantees spans have somewhere to go.
- No model, engine, or inference code is in scope. `coire-node` spawns no engines here.
- The Studios are kept deliberately bare so their compute is fully dedicated to inference (normative statement: FR-012a/b). This is a standing constraint on every feature that touches a Studio, not a one-off preference for this feature.
