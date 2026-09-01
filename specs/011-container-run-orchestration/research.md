# Research: Container Run Orchestration

## Docker control boundary

**Decision**: `coire-node` talks directly to the local Docker Engine REST API over the local Unix
socket with `httpx.AsyncHTTPTransport(uds=...)`. No Docker CLI, shell command, SDK dependency,
remote socket, or SSH path is introduced.

**Rationale**: the Engine API exposes the exact create/start/log/wait/archive/remove primitives,
keeps configuration typed, and is independently fakeable in contract tests.

**Alternatives considered**: Docker CLI subprocesses add a shell/tool dependency; Docker SDK adds
an avoidable dependency; core-to-Studio sockets violate FR-002 and Constitution IV.

## Network confinement

**Decision**: create one Docker `internal` bridge per run, attach only the run and a hardened
node-owned gateway relay, publish no ports, and remove the network with the run. The relay accepts
only HTTP requests for the configured gateway `/v1` surface, strips hop-by-hop headers, and has no
Docker socket, workspace, admin credential, or node-agent route. It is the run's sole peer and sole
route beyond its network.

**Rationale**: Docker documents that internal networks have no default route to other networks and
firewall traffic to/from them. A dedicated relay makes the one permitted destination explicit
without host firewall widening or access to host services.

**Alternatives considered**: a normal bridge permits general egress; `network=none` cannot reach
the gateway; host networking destroys isolation; macOS/VM-specific packet-filter rules are brittle
and violate the no-widening rule.

## Runtime hardening

**Decision**: create containers with non-root UID/GID, read-only rootfs, `CapDrop=[ALL]`,
`no-new-privileges`, no devices, no privileged mode, bounded PIDs, memory and CPU, tmpfs scratch,
an explicit workspace mount, no published ports, and `RestartPolicy=no`.

**Rationale**: these are native OCI/Docker controls and are inspectable after creation.

**Alternatives considered**: trusting image defaults is insufficient; privileged or mutable
containers contradict Principle II-a.

## Run-token representation

**Decision**: mint 256-bit opaque bearer secrets, store only an Argon2id hash plus scope and
server-side expiry/revocation state, and cache no positive authorization beyond a single request.

**Rationale**: immediate kill revocation cannot be guaranteed by a self-contained JWT alone. The
existing key hashing and audit patterns can be reused without persisting bearer material.

**Alternatives considered**: JWT-only expiry leaves a killed token usable; long-lived API keys
cannot express run ownership or spend ceilings safely.

## Durable execution and exactly-once effects

**Decision**: one DBOS workflow ID equals the run UUID. Every external node effect uses that run ID
as an idempotency key and container label/name. Persist command intent before calls and reconcile
the observed container before retrying.

**Rationale**: DBOS recovers workflow execution, while idempotent node effects prevent a replay
from creating a second container.

**Alternatives considered**: an in-memory background task or polling-only executor can orphan or
duplicate work after restart.

## Logs and results

**Decision**: stream framed stdout/stderr through the node response with a per-run byte ceiling,
emit structured log records carrying `run_id`, and collect only `/workspace/.coire/result.json`
with a strict maximum size and shared result schema.

**Rationale**: bounded ingestion prevents backend exhaustion and a single declared result path
separates harness failure from collection failure.

**Alternatives considered**: unbounded Docker logs and arbitrary archive extraction create denial
of service and path traversal risks.

## Dependency and license conclusion

No new runtime dependency is required. Docker Engine and OrbStack are existing operator-provided
infrastructure; DBOS, httpx, Argon2, and OTel are already pinned and licensed in the repository.
