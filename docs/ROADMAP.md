# Coire — Spec Kit feature roadmap

Each entry is one `/speckit.specify` run producing `specs/NNN-<slug>/`. The one-line prompt is a starting point for the specify command; the "done when" is the acceptance bar. Ordering is chosen so every feature is testable on a single Mac with a tiny model before the cluster-only features arrive.

## Spec directory mapping

Specs were generated 2026-08-29. Spec directories are numbered sequentially in build
order, because the tooling requires an integer `NNN-` prefix and cannot express the
letter-suffixed roadmap labels below. Each spec records its roadmap ID in its header.

| Spec directory | Roadmap ID | Phase |
|---|---|---|
| `specs/000-bootstrap/` | 000 | 0 |
| `specs/001-model-registry-node-agent/` | 001 | 1 |
| `specs/002-acquisition-pipeline/` | 001b | 1 |
| `specs/003-gateway-openai-v1/` | 002 | 1 |
| `specs/004-placement-scheduler/` | 003 | 1 |
| `specs/005-instances-cluster-state/` | 003a | 1 |
| `specs/006-sharded-serving-jaccl/` | 004 | 1 |
| `specs/007-auth-users-keys-audit/` | 005 | 2 |
| `specs/008-admin-console/` | 006 | 2 |
| `specs/009-observability-stack/` | 007 | 2 |
| `specs/010-agent-harness-profiles/` | 008 | 3 |
| `specs/011-container-run-orchestration/` | 009 | 3 |
| `specs/012-coire-ops-confirmed-mutations/` | 010 | 3 |
| `specs/013-mcp-server/` | 011 | 3 |
| `specs/014-chat-web-ui/` | 012 | 4 |
| `specs/015-image-generation/` | 013 | 4 |
| `specs/016-sft-training-jobs/` | 014 | 4 |
| `specs/017-evaluation-verbs/` | 014a | 4 |
| `specs/018-preference-optimisation/` | 014b | 4 |
| `specs/019-upgrades-rollback/` | 015 | 4 |
| `specs/020-control-plane-failover/` | 016 | 5 |
| `specs/021-node-self-healing/` | 017 | 5 |
| `specs/022-separate-control-data-fabrics/` | 000b | 1 (topology retrofit) |

Roadmap 000a (network prep) remains manual UDM/RDMA work. Its software and contract migration is
roadmap 000b / `specs/022-separate-control-data-fabrics/`.

Features 016 and 017 were added on 2026-08-29 after the original roadmap was written.
016 is **blocked on a constitutional amendment** — see that spec's Constitutional Conflict
section. 017 is not blocked but should be re-checked against Principle II during planning.
Feature 022 was added on 2026-08-30 as a topology retrofit and is ordered before further cluster
features despite its later directory number.

## Phase 0 — Foundation

**000 · bootstrap** — `specify init coire`, `/speckit.constitution` from `.specify/memory/constitution.md`, uv workspace with `coire-core`, `coire-api`, `coire-node`, `coire-agent`, `coire-web` stubs, distroless multi-stage Dockerfiles for api/mcp/scheduler/migrate/web/agent, `deploy/compose/` bringing up every service as its own container on per-concern networks with the docker-socket-proxy, CI building, scanning, and SBOM-ing images and running lint + tests.
*Done when:* `docker compose up` on core serves `/health` through nginx, each service can be `docker compose restart`ed alone without others failing, no production image contains `/bin/sh`, `coire-node` on a Studio returns 200, CI green.

## Phase 1 — Single-node inference works end to end

**000a · network prep (manual, no spec needed)** — The three hosts share the existing isolated Wi-Fi `lab` VLAN for control-plane traffic and internet egress. UniFi supplies stable DNS and firewall rules that prevent `lab → other VLANs`; Cloudflare Tunnel remains outbound-only and VPN/Tailscale remains the break-glass path. The only Thunderbolt connection is the direct `coire-edge-a ↔ coire-edge-b` data fabric, measured at 12.6 Gb/s / 0.85 ms and reserved for model replication and JACCL. Core does not participate in Thunderbolt or RDMA. **Remaining:** enable RDMA on both Studios, generate the two-rank JACCL hostfile with `mlx.distributed_config --backend jaccl --auto-setup`, and migrate the implemented `mesh_address`/`.mesh` control contracts to VLAN DNS names under a spec amendment (ADR-0006).
*Done when:* all three nodes reach one another by stable UniFi DNS names on the isolated VLAN; required service ports are restricted to their minimum peer sets; losing the Studio Thunderbolt link leaves control-plane membership and single-node serving intact; model replication verifiably takes the data fabric; prompt-to-first-token and multi-tool agent benchmarks meet their latency objectives over Wi-Fi; and a 2-rank JACCL all-reduce test passes.

**000b · separate control and data fabrics** — "Migrate node endpoint contracts, listeners, routing,
observability, deployment, and runbooks so the isolated VLAN is the three-host control fabric and the
direct Studio Thunderbolt link is exclusively the replication/JACCL data fabric; preserve mixed-version
rollback and remove core from Thunderbolt only after measured preflight passes."
*Done when:* core has no Thunderbolt address or cable; both Studios cold-start, register, and serve
single-node inference directly over the VLAN; model replication and JACCL remain Studio-only and fail
closed on link loss; firewall and observability checks pass; and the real-cluster cutover and rollback
procedure is recorded without state loss.

**001 · model registry & node agent** — "Admin-only model registry with placement policy, memory estimate, idle TTL, visibility/entitlement, and capability profile; download job that pulls once from HF, verifies, and peer-replicates so the model is `ready` only when both Studios hold it; node agent that can load (`mlx_lm.server`), health-check, report memory and disk, and unload."
*Done when:* `coire model add` (admin key) works and a user key gets 403; `ready` implies two verified copies; `load`/`unload` work; the registry reflects true process state after a node-agent restart.

**001b · acquisition pipeline (inspect / pull / convert / validate / replicate)** — "DBOS workflow that inspects an HF repo's metadata (architecture support, MLX vs raw vs GGUF, size, fit-by-precision), pulls once, runs `mlx_lm.convert` with chosen bits/group size/mode/mixed recipe as a ledger-reserving job, validates (generation smoke test, perplexity vs reference, tool-call template check), replicates, and records variants on the model row."
*Done when:* adding a raw PyTorch repo and an `mlx-community` repo both end `ready` on both Studios; a GGUF-only repo is rejected with guidance; the console shows per-variant validation results.

**002 · gateway & OpenAI-compatible /v1** — "FastAPI gateway proxies `/v1/chat/completions` (streaming) and an Anthropic-compatible `/v1/messages` adapter to the right instance and serves `/v1/models` filtered to published, ready, entitled models with Coire extensions (load state, tags, description); rejects unknown model ids; usage capture, keep-alive while loading, Retry-After semantics."
*Done when:* the OpenAI Python SDK, Cursor/Continue, and Claude Code (via `ANTHROPIC_BASE_URL`) work against Coire unchanged; an unpublished model id returns 404 to users and works for admins.

**003 · placement scheduler & auto-unload** — "Memory ledger per node, LRU eviction, pinning, idle-TTL control loop, `single:auto` and pinned placements."
*Done when:* loading a model that doesn't fit evicts the right one; idle models unload on schedule; pinned models never do (the admin model on Studio B is the first pinned entry); the ledger includes each Studio's agent-sandbox slice; dashboard shows the ledger.

**003a · instances & cluster state** — "`ModelInstance` state machine (`requested → reserving → launching → warming → ready → draining → stopped|failed`) as a DBOS workflow; `/api/v1/instances` (`/state`) and per-instance SSE events (create + await); gateway routes to instances, multiple instances per model; declared node inventory with per-node registration tokens (no discovery)."
*Done when:* a scheduler restart during a launch resumes and ends `ready`; two instances of one model coexist; an unregistered node on the VLAN cannot join.

**004 · sharded serving over JACCL** — "`sharded:tp` and `sharded:pp` placements launched via `mlx.launch` with a generated hostfile; both-node reservation; coordinated teardown; link probe (JACCL all-reduce + ring fallback) stored on the link record and gating TP; rank-failure semantics (instance `failed`, group teardown, `503 Retry-After`, node `degraded`, re-place as smaller single-node variant if it fits); benchmark harness recording tokens/s per placement."
*Done when:* a >250 GB model serves through the gateway; killing rank 1 mid-stream yields a clean 503 and a re-placed instance within the TTL; benchmark report compares single-A vs TP vs PP for a mid-size model; dashboard shows link bandwidth/latency.

## Phase 2 — Identity, users, admin

**005 · auth, users, API keys, audit** — "Cloudflare Access JWT validation, user/role model, scoped API keys with rate limits and budgets, audit log, admin API for users and keys."
*Done when:* unauthenticated requests fail at every route; keys can be created, scoped, rotated, revoked; every admin mutation has an audit row.

**006 · admin console** — "React admin routes: nodes & memory ledger, models (add from HF / publish / unpublish / retire / pin / load / unload / convert, with download & replication progress, disk per Studio, per-task defaults), users & keys, runs & jobs with kill, upgrades, audit viewer, 'ask Coire' box wired to the ops agent (read-only until 010)."
*Done when:* every operation in Principle I/II is reachable without a terminal.

**007 · observability stack** — "OTel/Prometheus/Loki/Tempo/Grafana compose, instrumentation across api/node, three seed dashboards, alert rules from the architecture doc."
*Done when:* a deliberately slow request is attributable to a span; alerts fire on node-down and tunnel-down.

## Phase 3 — Agents

**008 · agent harness & capability profiles** — "Single `coire-agent` image; Pydantic AI agents `coding`, `general`, `image`, `ops`; capability-profile-driven tool-calling/structured-output strategies; context budgeting; harness evaluation suite (`coire eval harness`) that marks models `verified`."
*Done when:* the suite runs against at least three open-weights models and produces a scorecard; `apply`-class tasks are refused for unverified models.

**009 · container run orchestration on the Studios** — "OrbStack on each Studio with a fixed memory slice in the ledger; coire-node `POST /runs` API brokering create/start/stream/wait/collect/remove against the local Docker socket; DBOS workflow in coire-scheduler choosing the Studio (co-located with the run's model, else free slots), run tokens, limits, kill switch, restart-resume. Core runs no user harness."
*Done when:* killing coire-scheduler mid-run and restarting resumes the run; `kill` from the admin UI stops the container within 5 s and invalidates the token; a run request while core's OrbStack has no `coire-agent` image at all still succeeds (proving runs never land on core).

**010 · `coire-ops` with confirmed mutations** — "Long-lived `coire-ops` container on core (the only harness core runs) using the pinned admin model on Studio B via the gateway; ops tools for admin actions with `confirm_token` flow; admin UI approval prompt; read-only degraded mode when the admin model is unreachable."
*Done when:* "unload the idle 400B model" via chat results in a confirmation card and, on approval, an audited unload.

**011 · MCP server (research / plan / apply)** — "Streamable-HTTP MCP endpoint with exactly three coding tools backed by agent runs on cloned workspaces; `apply` produces a branch + diff + test summary."
*Done when:* Claude Code / Cursor can add Coire as an MCP server and complete a research→plan→apply loop on a sample repo.

## Phase 4 — Chat UI, images, training

**012 · chat web UI** — "Claude-style conversation UI: streaming, task-grouped model picker showing only published models with descriptions, tags, and load state (with warm-up estimate for cold models), file upload, code mode using the coding agent, conversation history, thinking-block display."
*Done when:* a non-technical user can chat, switch models, and see when a model is loading.

**013 · image generation** — "Typed `ImageSpec` (txt2img, img2img, fill, control, LoRA stack, upscale, n, seed) with per-model bounds; resident `mflux` image worker per Studio managed by coire-node with ledger reservation and idle TTL; queued jobs with SSE events (`queued/started/progress/done/error`) and cancel; stage-level LRU (prompt encodings, LoRA-patched weights, control preprocessing); outputs streamed to a `coire-blobs` volume on core with expiring URLs; full spec embedded in PNG metadata and stored on the image row; registry kinds `image_model/image_lora/control_model/upscale_model` through the acquisition pipeline; admin presets; `/v1/images/generations` adapter; gallery with reuse-settings/regenerate; explicit entitlement enforcement and NSFW tagging for gallery filtering."
*Done when:* entitled users can generate through the UI and the OpenAI endpoint; an image dragged back into the UI reproduces itself from embedded metadata; changing only the seed skips prompt encoding (visible in trace spans); non-entitled requests naming an explicit preset are refused and audited; image jobs don't starve decoding on the same node; Studios retain no output bytes after completion.

**014 · SFT training jobs (LoRA/QLoRA/DoRA)** — "`TrainingSpec` schema (model/data/objective/parameterization/optim/eval/output) accepted as YAML and as a console form and stored verbatim on the job; `Conversation` canonical type shared with the gateway's chat-template rendering; dataset registry (`sft` type, loaders, schema check, split, mixtures with proportions, `coire data analyze` on upload); `objective=sft` via `mlx_lm.lora` behind an objective registry; checkpoints + resume through DBOS; reservation through the scheduler (evict → train → reload); loss metrics; adapter row selectable as `model@adapter`; `placement: single | data_parallel`; seed recipes in `recipes/`."
*Done when:* an SFT LoRA trains from a recipe YAML and from the UI, survives a mid-run node restart by resuming from checkpoint, and is usable as `model@adapter` in `/v1/chat/completions` with its loss curve on the Jobs dashboard.

**014a · evaluation verbs** — "`coire eval` with harness, task, and judge suites (judge = platform model via the gateway); `TrainingSpec.eval` checkpoint scheduling; before/after scores on adapter rows; console comparison view."
*Done when:* an adapter row shows base vs. adapter scores for at least one task suite and one judge suite, produced automatically at the end of a training run.

**014b · preference optimisation (DPO/ORPO) & feedback capture** — "`preference` dataset type; `objective=dpo|orpo` jobs via pinned `mlx-lm-lora` (in-house DPO trainer as fallback) with `init_adapter` chaining from an SFT adapter and 2× base memory reservation; chat UI thumbs, regenerate-and-compare, and admin pairwise review queue writing feedback rows; admin export of feedback → preference dataset with filters; per-user feedback opt-out and disclosure."
*Done when:* comparisons collected in chat export to a dataset, a DPO run continues from an SFT adapter, and the resulting adapter must pass the harness eval before `apply` may use it.

**015 · upgrades & rollback** — "Versioned engine envs on nodes, smoke test, symlink flip, rollback; control-plane image/`uv sync` upgrade job; admin UI trigger."
*Done when:* an intentionally broken mlx-lm pin rolls back automatically and leaves the node serving.

## Phase 5 — Resilience

**016 · control-plane failover** — "Poller on all three hosts electing a frontend host when core is unavailable; priority core → coire-edge-a → coire-edge-b; quorum-gated promotion; stateless inference-only degraded tier with no database on a Studio; capability tiers degrading with healthy membership; automatic demotion when core returns."
*Done when:* powering off core yields inference service from a Studio within the failover threshold; a minority partition never promotes; no database or admin surface ever runs on a Studio; core returning restores full service with no operator action.
*Blocked on:* an amendment to Principle II, which currently forbids a web tier on a Studio.

**017 · node self-healing** — "Two-layer health agent per node: a minimal native supervisor running as an OS service outside the container runtime, plus a containerised diagnostic agent. Its entire mandate is restoring cluster membership — node-agent liveness, network reachability, registration — and nothing else; capacity concerns are escalated, and engines and engine environments are excluded because 004/005/019 own them. Deterministic symptom-to-action remediation, quorum-aware conservatism, circuit breaker with backoff, escalation for anything unlisted. Built on the agent framework's slim distribution with the model unbound, so the disconnected path needs no model."
*Done when:* killing the node agent on a Studio rejoins it automatically; every autonomous action maps to membership restoration and nothing else; a crashed engine produces zero supervisor actions; a minority-partitioned node takes only local non-destructive actions; a persistent fault opens the circuit breaker.

## Later / backlog

* `objective=grpo` (needs a reward function/verifier framework) and `parameterization=full` for models small enough to fit optimizer state in 256 GB.

* Embedding model + retrieval for the `general` agent's file Q&A.
* Speculative decoding (draft model per spec) and prompt-cache-aware routing.
* Image LoRA training on the platform; a second image worker type (possibly headless ComfyUI via API-format JSON) behind the same `ImageSpec` for models mflux lacks.
* Quantisation recipe search: automatically try a few `mlx_lm.convert` recipes and rank by perplexity-per-GB.
* DGX Spark heterogeneous prefill/decode disaggregation (if that hardware arrives).
* Multi-tenant spend reports and per-user monthly statements.
