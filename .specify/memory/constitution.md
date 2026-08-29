<!--
SYNC IMPACT REPORT
Version change: 0.1.0 → 0.1.1 (PATCH — normative-keyword normalisation, no semantic change)

Modified principles: none (all seven principles and II-a unchanged in name and substance)
Added sections:      none
Removed sections:    none

Changes in this amendment:
- Quality and Operations Standards → Correctness bullet: lowercase "must"/"must never"
  raised to RFC-2119 "MUST"/"MUST NOT" to match the register used in Principles I-VII.
- This Sync Impact Report added as required by the constitution workflow.

Template conformance (checked against constitution-template, core layer):
- Heading hierarchy matches exactly: H1 title, ## Core Principles, ## <section 2>,
  ## <section 3>, ## Governance, version footer.
- Section 2 = "Technology Constraints"; Section 3 = "Quality and Operations Standards".
- Seven principles plus sub-principle II-a; the template's five slots are a floor, not a cap.
- No unreplaced [ALL_CAPS] placeholders, no residual template comments, no TODO markers.
- Dates ISO-8601; no trailing whitespace; single blank line between sections.

Deferred items: none.
-->

# Coire Constitution

## Core Principles

### I. Bare engines, owned lifecycle
Coire drives `mlx_lm.server`, `mlx.launch`, `mlx_lm.lora`, and `mflux` directly. No inference wrappers (Ollama, LM Studio, exo, LocalAI) are introduced. Process lifecycle, placement, memory accounting, and unloading are Coire's responsibility and MUST be observable and reversible from the admin API. Engines are never network-reachable except from the node agent and the gateway.

### II. Control node is disposable, workers are sacred
The Mac Mini ("core") hosts every stateful and orchestrating service as OrbStack containers in one compose project (gateway/API, MCP server, scheduler, web frontend, Postgres, observability). Core MUST NOT host a language model or any inference/generation engine, and the only agent harness core may run is `ops` — the harness that calls models hosted elsewhere in order to perform the platform's own operational duties. All user-facing harnesses (`coding`, `general`, `image`) run as containers on the Studios, brokered by the node agent within a fixed memory slice. Mac Studios run the node agent, model/training/image processes, and those agent sandboxes — nothing else: no databases, no web tier. Nothing on a Studio may be a single source of truth; a Studio reboot MUST be recoverable by the scheduler with no manual steps.

### II-a. One service, one container, bare image
Every control-plane service runs as its own container with exactly one process and can be stopped, upgraded, or rolled back without affecting any other service; no image is shared between services and no container hosts two roles (API, MCP, scheduler, web, migrate are all separate). Production images are minimal: distroless/Chainguard or `scratch` bases, multi-stage builds, only the runtime and the service's own dependencies, no shell, package manager, or debug tooling. Non-root, read-only root filesystem, all capabilities dropped, per-concern networks only, healthcheck required, CVE scan and SBOM in CI. Debugging uses ephemeral containers, never baked-in tools.

### III. Contracts first, typed end to end
Every boundary — REST, MCP, node agent, agent-harness result, job payload — is a Pydantic model in `coire-core` and is the only accepted wire shape. OpenAPI is generated, not hand-written. Changes to a contract require a spec amendment and a migration/compatibility note. The public `/v1` surface MUST remain OpenAI-compatible; Coire-specific fields are additive and prefixed `coire_`.

### IV. Public by design, therefore zero implicit trust
The platform is internet-reachable through Cloudflare Tunnel + Access. Every request is authenticated (edge identity or Coire API key); every key is scoped, rate-limited, and budgeted; every admin mutation and every explicit-content entitlement change writes an audit row. Agent runs execute in containers with no egress except the gateway, hold short-lived run tokens, and can always be killed from the admin API. Secrets live in Keychain, never in the repo or images.

### V. Models are data, capability is measured
Only an admin, via the admin API/console, may add a model or trigger a download from Hugging Face; no user request, agent, or automation may. Every roster model is stored on both Studios before it is `ready`. Users see and select only `published` models they are entitled to, through a picker; the gateway rejects any model identifier that is not a registry id and never passes caller-supplied model or adapter strings to an engine. A model enters the roster as a registry record with placement policy, memory estimate, idle TTL, chat template, visibility, and a capability profile (tool calling, structured output, context, reasoning). Harness behaviour is selected from the profile, never hard-coded to a model name. A model is `verified` for coding/`apply` only after passing the harness evaluation suite; the router MUST refuse unverified models for write-capable tasks.

### VI. Observable or it doesn't ship
Every service emits OpenTelemetry traces, Prometheus metrics, and structured logs to the local stack. A feature is not complete until its dashboard panel and at least one alert rule exist, and until a slow or failed request can be attributed to a span (gateway, queue, load, prefill, decode, node, network).

### VII. Spec-driven, test-gated, incremental
Work flows `/speckit.specify → clarify → plan → tasks → implement` per feature, one feature branch per spec directory. Every feature ships with contract tests for its API surface and an integration test that runs against a tiny model (≤1 GB) so CI can run on a single Mac. Sharded, training, and image features additionally require a documented manual verification on the real cluster before merge. Prefer the smallest change that satisfies the spec; defer generality until a second concrete use appears.

## Technology Constraints

* Python 3.13, `uv` workspace, FastAPI, Pydantic v2, Pydantic AI, SQLAlchemy 2 + asyncpg, Alembic, DBOS for durable workflows, Postgres 17.
* Node runtime: macOS 26.2+, MLX / mlx-lm / JACCL / mflux pinned in a lockfile; versioned envs under `/opt/coire/envs` with symlink flip and rollback.
* Containers: OrbStack on core; control plane deployed only via `deploy/compose/` with CI-built, tag-pinned images (`coire-api`, `coire-mcp`, `coire-web`, `coire-agent`); Docker socket reachable only through a docker-socket-proxy with an explicit allowlist. `coire-agent` from a slim base, `uv sync --frozen`, non-root, read-only root filesystem, workspace volume only, run-network only.
* Web: React + TypeScript + Vite, single SPA, SSE for streaming, served by an nginx container that is the sole ingress for cloudflared; no Node runtime in production.
* Observability: OTel Collector, Prometheus, Loki, Tempo, Grafana, Alertmanager via compose on OrbStack; Logfire SDK used only as an OTel instrumentation layer exporting locally.
* Edge and LAN: Cloudflare Tunnel, Access (OIDC), WAF and rate-limit rules; no inbound port forwards on the UDM SE, ever. Lab nodes live on an isolated VLAN with deny-by-default rules to other VLANs; hosts are addressed by UniFi DNS names, never raw IPs, in any config. Break-glass admin via UDM VPN or Tailscale.
* Forbidden: inference wrappers (Principle I), long-lived static tokens for agents, direct Studio exposure, hand-edited production config on nodes (everything applied from `deploy/`).

## Quality and Operations Standards

* Latency: gateway overhead ≤ 20 ms p95 excluding model time; first-token for a loaded single-node model ≤ 1.5 s p95 for ≤ 4k-token prompts.
* Correctness: memory ledger vs measured RSS drift MUST trigger an alert above 10 %; scheduler MUST NOT admit a load that would push a node into swap.
* Safety: explicit image generation requires an admin-granted per-user entitlement, an authenticated human identity, and an audit row; never available to service tokens.
* Recovery: control-plane restart resumes in-flight agent runs and jobs (DBOS); node-agent restart re-adopts running model processes rather than killing them.
* Upgrades: every engine or control-plane upgrade runs a smoke test and rolls back automatically on failure.
* Documentation: each feature updates `docs/runbooks/` for the operational surface it adds (how to kill it, how to see it, how to roll it back).

## Governance

This constitution supersedes ad-hoc practice. Every `/speckit.plan` MUST include a "Constitution Check" listing each principle and stating compliance or a justified, time-boxed exception recorded as an ADR in `docs/adr/`. Amendments require a version bump below, a changelog line, and a review of open specs for conflicts. Principles are numbered so specs can cite them (e.g. "per Principle IV").

**Version**: 0.1.1 | **Ratified**: 2026-08-28 | **Last Amended**: 2026-08-29
