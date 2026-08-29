# Coire — Spec Kit feature roadmap

Each entry is one `/speckit.specify` run producing `specs/NNN-<slug>/`. The one-line prompt is a starting point for the specify command; the "done when" is the acceptance bar. Ordering is chosen so every feature is testable on a single Mac with a tiny model before the cluster-only features arrive.

## Phase 0 — Foundation

**000 · bootstrap** — `specify init coire`, `/speckit.constitution` from `.specify/memory/constitution.md`, uv workspace with `coire-core`, `coire-api`, `coire-node`, `coire-agent`, `coire-web` stubs, distroless multi-stage Dockerfiles for api/mcp/scheduler/migrate/web/agent, `deploy/compose/` bringing up every service as its own container on per-concern networks with the docker-socket-proxy, CI building, scanning, and SBOM-ing images and running lint + tests.
*Done when:* `docker compose up` on core serves `/health` through nginx, each service can be `docker compose restart`ed alone without others failing, no production image contains `/bin/sh`, `coire-node` on a Studio returns 200, CI green.

## Phase 1 — Single-node inference works end to end

**000a · network prep (manual, no spec needed)** — Create the `lab` VLAN and firewall rules on the UDM SE, static reservations + DNS names for core/studio-a/studio-b, split-horizon override for the public hostname, VPN or Tailscale break-glass, Thunderbolt mesh cabled and RDMA enabled on both Studios, `mlx.distributed_config --backend jaccl --auto-setup` producing the hostfile committed to `deploy/cluster/`. Decide on the 10GbE switch.
*Done when:* the three nodes resolve by name from each other, nothing in `lab` can reach other VLANs, and a 2-rank JACCL all-reduce test passes.

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

## Later / backlog

* `objective=grpo` (needs a reward function/verifier framework) and `parameterization=full` for models small enough to fit optimizer state in 256 GB.

* Embedding model + retrieval for the `general` agent's file Q&A.
* Speculative decoding (draft model per spec) and prompt-cache-aware routing.
* Image LoRA training on the platform; a second image worker type (possibly headless ComfyUI via API-format JSON) behind the same `ImageSpec` for models mflux lacks.
* Quantisation recipe search: automatically try a few `mlx_lm.convert` recipes and rank by perplexity-per-GB.
* DGX Spark heterogeneous prefill/decode disaggregation (if that hardware arrives).
* Multi-tenant spend reports and per-user monthly statements.
