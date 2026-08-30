# ADR-0001: Defer authentication and public edge until the platform is ready for external traffic

- **Status**: Accepted
- **Date**: 2026-08-29
- **Deciders**: Dan McTeer
- **Constitution**: exception to **Principle IV** (Public by design, therefore zero implicit trust) and to the Technology Constraints line forbidding long-lived static tokens
- **Time-box**: closes with feature 007 (auth, users, keys, audit; Cloudflare Access) and feature 005 (issued node registration tokens)

## Context

Principle IV requires every request to be authenticated at the edge or by a Coire key, with
Cloudflare Tunnel + Access in front of the platform. Feature 000 builds the skeleton those
controls will attach to, but there is no Cloudflare tunnel yet, no identity provider configured,
and no users. Building authentication now would be building it against nothing.

## Decision

1. **No user-facing authentication** in feature 000. On the control plane only `/health`,
   `/ready` and `POST /api/v1/nodes/register` exist, and only registration checks a
   credential (the static node token). The one exception is machine-to-machine: the node
   agent's own `/node/health` requires that same token as a bearer (spec FR-013), so a Studio
   never answers an anonymous caller even on the unrouted mesh.
2. **No `cloudflared`** in the compose project. The platform is LAN/mesh-only. `coire-web`
   publishes `127.0.0.1:8080` on core and nothing else.
3. **Node registration uses a static per-node token**, stored in each Studio's System keychain
   and in core's Keychain (`coire-node-tokens`). It is a shared secret, not an issued credential.
4. **The application is built so auth wires in without restructuring**: a single FastAPI
   dependency `require_principal()` is declared in `apps/coire-api/src/coire_api/auth.py`,
   returns an anonymous principal, and every route declares it. Feature 007 replaces its body
   with edge-assertion and API-key validation; no route signature changes.
5. `key_signing_secret` is provisioned and mounted now so 007's key issuance needs no new
   secret plumbing.

## Consequences

- Until 007, nothing on core may be reachable from outside the mesh/LAN. The compose topology
  test asserts `coire-web` publishes only on loopback.
- Until 005, a leaked node token permits a rogue node to register. Mitigated by the mesh being
  physically unrouted and registration being refused for any name not in the declared
  inventory; accepted for a private, pre-user cluster.
- 007's Constitution Check must record this ADR as **closed**, and the topology test must be
  updated to require Access-fronted ingress.

## Alternatives rejected

- A placeholder auth that always succeeds — indistinguishable from protection; worse than none.
- Real auth now — blocks 000 on Cloudflare Access configuration that does not exist.

## Amendment: the control plane must be reachable on the mesh (2026-08-30, feature 001)

Decision 2 above said `coire-web` publishes `127.0.0.1:8080` on core "and nothing else". That
made node registration impossible: an agent posts to `coire-core:8080` over the Thunderbolt
mesh, and nothing on core listened there. The gap survived feature 000 because its T063 — the
step that installs an agent on a Studio — was never run, so no agent had ever attempted to
register.

`coire-web` now also publishes on core's **mesh address** (`192.168.100.10:8080`), resolved by
`coire-up` from the managed `/etc/hosts` block and defaulting to loopback when the host is not
on the mesh.

This does not weaken the intent of this ADR. The mesh is unrouted, reaches only the two
Studios, and is not connected to the lab VLAN or the internet — so the control plane is exposed
to strictly less than a LAN binding would expose it to, and to nothing that was not already
trusted to hold a node token. `tests/integration/test_topology.py` enforces that loopback and
that one mesh address are the only addresses anything publishes on.

The rest of this ADR is unchanged, and feature 007 still closes it.
