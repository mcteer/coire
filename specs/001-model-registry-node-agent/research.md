# Research: Model Registry and Node Agent

**Feature**: `001-model-registry-node-agent` · **Date**: 2026-08-30

Every unknown in the plan's Technical Context is resolved here. Versions were checked against
PyPI and upstream source on 2026-08-30; cluster facts were probed the same day (both Studios:
macOS 26.6.2, 256 GB, 1.78 TB free, Homebrew Python 3.14.7, no `/opt/coire` yet — feature 000's
T063 install is still pending and is a prerequisite of this feature's quickstart).

## R1. Engine invocation and what "ready" means

**Decision**: The node agent runs `<env>/bin/python3 -m mlx_lm.server --model /opt/coire/models/<slug> --host <mesh address> --port <allocated> --log-level INFO`
(mlx-lm **0.31.3**, mlx **0.32.2**; both ship cp313 arm64 wheels, so the pinned 3.13 environment
from 000 holds). An engine is reported `ready` only after the agent's own probe — a
`POST /v1/chat/completions` with `max_tokens: 1` against the engine's port — returns 200.

**Rationale**: `mlx_lm.server` serves `GET /health` → `{"status":"ok"}` from its HTTP thread
**before and independently of** the model load, which happens on the generator thread; there is
no "model loaded" log line either. So "the engine answers its own health probe" (FR-012) has to
mean a probe that exercises the loaded model, or it proves nothing. A one-token generation is
the smallest such probe; its latency is also the first measured warm-up figure the picker will
show (feature 003). `/health` is still polled first as a cheap "process is listening" gate.

**Two engine properties the design must contain rather than fix**:

- The server honours a per-request `model` field and will `mlx_lm.load()` *any path or repo id
  it is given*; there is no flag to disable this. It is why FR-017 and FR-018 are load-bearing:
  the engine binds the mesh address only, the mesh is unrouted, and feature 003's gateway
  always sets `model` to the registry-resolved path. The agent additionally starts the engine
  with `HF_HUB_OFFLINE=1` so a stray repo id cannot trigger a download from the engine.
- On start the server calls `mx.set_wired_limit(max_recommended_working_set_size)`; that is the
  engine wiring memory, not leaking it. It is included in the measured footprint (R6).

**Alternatives considered**: treating `/health` 200 as ready — measured to be true before the
weights are in memory; rejected. Parsing stderr for a load marker — none exists.

## R2. Inspecting a repo and deciding it is MLX-format

**Decision**: Inspection runs **on the node agent** (`POST /node/models/inspect`) using
`huggingface_hub` **1.29.0**: `HfApi.model_info(repo, files_metadata=True)` for the file list,
sizes and LFS digests, plus `hf_hub_download` of `config.json` and `tokenizer_config.json`
(metadata-sized files) into a scratch cache. A repo is MLX-format when **either** its Hub
`tags` contain `mlx` **or** `config.json` has a top-level `quantization` object with
`bits`/`group_size`; it is GGUF-only when it has `.gguf` files and no `.safetensors`. Anything
else is rejected at add time with `not_mlx_format` and a pointer to feature 002.

**Rationale**: FR-005 puts the Hugging Face credential on the node only, so the node is the only
place gated metadata can be read. `library_name` is `transformers` even on `mlx-community`
repos, so it cannot be the signal; the `mlx` tag is set on every mlx-community conversion and
`config.json.quantization` is what `mlx_lm.load_model` itself keys on. Unquantised MLX repos
(`-bf16`) carry no `quantization` key, hence the tag as a second signal. `mlx-community`
naming is a heuristic, not a guarantee, and is not used.

**Nested configs**: multimodal repos (Qwen3.8's `Qwen3_5ForConditionalGeneration`, R12) put the
language model's shape under `config["text_config"]`; inspection reads architecture and sizing
keys from `text_config` when present and from the top level otherwise, and records which.

**Gating**: `GatedRepoError` subclasses `RepositoryNotFoundError`, so it is caught first and
surfaced as `423`/`gated` — the spec's requirement that gating be named specifically (edge
case 5). `RepositoryNotFoundError` is `404`/`not_found`.

**Per-file digests**: Hugging Face publishes `lfs.sha256` for LFS files (every safetensors,
`tokenizer.json`) and only a git `blob_id` for small files. The manifest therefore records the
locally computed SHA-256 of every file and, where the Hub gave one, `upstream_sha256`; the pull
fails if the two disagree on any file. This is what makes the origin copy *verified* against
upstream rather than merely present.

## R3. Peer replication transport

**Decision**: HTTP over the mesh, node to node, authorised by a per-replication **transfer
grant**. The control plane generates 32 random bytes, registers it on the origin
(`POST /node/models/{slug}/export`), and hands it to the replica in the import request; the
replica fetches `/node/export/{grant}/manifest` then each file with Range requests from
`http://<origin>.mesh:9400/node/export/{grant}/files/<path>`, hashing as it writes. The export
routes are served **only on the mesh listener** and the replica's import client has **no egress
fallback** (a `MeshClient` variant with fallback disabled), so replication cannot traverse Wi-Fi
(FR-007, SC-004). Grants expire after 24 h and are revoked when the job ends.

**Rationale**: Each node holds only its own token, so a peer cannot present the origin's bearer;
shipping node A's token to node B would widen the static-token exposure ADR-0001 already
apologises for. A single-purpose, model-scoped, expiring grant is the smallest credential that
lets B read exactly one directory from A. Every other option was worse: `rsync`/`scp` over SSH
depends on the operator's personal keys being present in the daemon's home directory —
credentials Coire neither issues nor can revoke, and "hand-configured on nodes" is a forbidden
constraint; routing bytes through core doubles traffic through the disposable node.

**Throughput expectation**: the mesh measured 1.5–1.6 GB/s. A Python streaming path
(Starlette `FileResponse` → httpx streamed download in 8 MiB chunks, SHA-256 on the fly) is
expected to sustain 0.5–1 GB/s; the manual verification records the achieved rate for the 16 GB
Qwen3.8-27B copy (R12) so feature 002 can decide whether it needs a faster path.

**Alternatives considered**: a pull-through where the replica downloads from Hugging Face too —
violates the one-external-pull rule and SC-004. macOS file sharing / NFS between Studios —
introduces a service on a Studio that is not the node agent (Principle II).

## R4. Owning engine processes across an agent restart

**Decision**: Engines are spawned with `subprocess.Popen(..., start_new_session=True)` so each
runs in its own session and process group, **and** the LaunchDaemon plist sets
`AbandonProcessGroup: true`. The agent records `{engine_id, slug, port, pid, create_time}` in
`/opt/coire/state/engines.json` (atomic rewrite on every change). On start it reads that file,
confirms each `(pid, create_time)` still identifies a live process whose command line names the
expected store path, and adopts it; a pid that is gone is reported `dead`; any other
`mlx_lm.server` process it finds is reported `orphan`. The control plane calls
`POST /node/engines/reconcile` with what the registry expects after every registration, and the
result corrects the registry (FR-015).

**Rationale**: `launchd.plist(5)`: "When a job dies, launchd kills any remaining processes with
the same process group ID as the job" — without `setsid` or `AbandonProcessGroup` a `KeepAlive`
restart would kill every engine, which is exactly the "restart that orphans engines" the spec's
US4 forbids. Apple's TN2083 lists a new session (`setsid`) as the preferred remedy and
`AbandonProcessGroup` as the fallback; doing both costs nothing and covers `launchctl bootout`.
psutil ≥ 7.1 computes `create_time()` from a monotonic base, so `(pid, create_time)` survives
clock adjustments and is compared with a 1 s tolerance; PID reuse cannot fool it.

**Alternatives considered**: running each engine as its own launchd job — the cleanest per
TN2083, but it would mean the agent writes plists and calls `launchctl` with sudo for every load,
and plists are exactly the "hand-edited config on nodes" class the constitution forbids;
rejected. Keeping engine state only in the registry — the agent must find its processes
*before* it can reach the registry after a reboot; hence the cache, which is never authoritative.

## R5. Running acquisition work on the node without charging inference for it

**Decision**: Pull, import and verify jobs run in a **worker subprocess**
(`python -m coire_node.worker <state-file>`) started at `nice 10`, one per job, supervised by
the agent. The agent's own CPU/RSS budget (000 FR-012c) therefore measures only the agent; the
worker's CPU is reported separately on the job. Job state is written to
`/opt/coire/state/jobs/<job_id>.json` after every stage so an agent restart or node reboot
re-attaches to or restarts the job from its stage (edge case 3).

**Download**: `snapshot_download(repo, revision, local_dir=<store>/<slug>, allow_patterns=…, token=…)`
with `HF_HOME=/opt/coire/hf-cache`. huggingface_hub 1.29 **does not resume a partial file**:
it writes to a unique `.incomplete` and deletes it on failure (issue #4196, open; the fix is
deferred to `hf_xet`). Resume granularity is therefore **per file** — completed files are kept
and skipped on retry. That satisfies edge case 2 for any sharded repo (Hub shards are ≤ 5 GB, so
an interruption costs at most one shard) and is recorded as the accepted limit for single-file
repos. `hf_xet` is a default dependency on arm64 and is left enabled; it verifies reconstructed
files against the LFS SHA-256, and the manifest hash (R2) is the independent check on top.

**Hashing cost**: SHA-256 via `hashlib` (OpenSSL, ARMv8 crypto extensions) runs at ~1.5–2 GB/s
per core, so hashing a 16 GB copy is ~10 s and a 200 GB copy ~2 min — acceptable, and it runs
niced in the worker.

**Rationale**: The constitution's collection budget exists so Studios spend their cycles on
inference; a 100 GB pull hashed inside the agent process would blow that budget and make
`collection_budget_ok: false` meaningless. A subprocess also isolates a crashing download from
the agent's listeners.

**Alternatives considered**: threads in the agent — pollutes the budget, and CPU-bound hashing
would contend with the event loop. A separate long-lived job daemon — a second service on a
Studio, which Principle II forbids.

## R6. Memory: estimating before the load, measuring after

**Decision — estimate** (FR-010, computed at add time from inspection):

```
weights      = Σ bytes(*.safetensors)
kv_per_token = 2 × num_hidden_layers × num_key_value_heads × head_dim × 2 bytes   (fp16 cache)
estimate     = weights × overhead[precision] + kv_per_token × kv_headroom_tokens
overhead     = {4bit: 1.10, 5bit: 1.10, 6bit: 1.10, 8bit: 1.08, bf16: 1.05, other: 1.15}
kv_headroom_tokens = 32_768 (setting)
```

The multipliers are the platform's initial guess, exposed as a setting; feature 004's ledger
records `resident − estimate` on every load (FR-014) precisely so they can be corrected from
data. For Qwen3.8-27B-4bit (R12) the formula gives 16.05 GB × 1.10 + 262,144 B × 32,768 ≈
**26.2 GB** (weights 17.7 GB + 8.6 GB KV headroom), which fits either Studio's budget several
times over.

**Decision — fit**: a model fits a placement when `estimate ≤ memory_budget_bytes` of at least
one Studio, where `memory_budget_bytes = memory_total × node_memory_budget_fraction` (default
0.90 → 230 GB of 256 GB, the figure ARCHITECTURE §4 assumes). Disk fit is
`total_bytes + disk_reserve_bytes ≤ store_free_bytes` on **both** Studios (two copies rule).
Both checks run before any byte moves and return the figures in the 422.

**Decision — measure**: `resident_bytes` is the process's **physical footprint**
(`proc_pid_rusage(pid, RUSAGE_INFO_V4).ri_phys_footprint`) read through `ctypes` from `libproc`,
because that is the number the kernel accounts and jetsam acts on and it includes Metal/
IOAccelerator dirty memory; `rss` from released psutil (7.2.2) does not reliably include GPU
allocations (MLX issue #3896 shows `get_peak_memory` at 46 GB while `footprint` showed 110 GB).
On non-macOS (CI's Linux containers) the same function falls back to `rss`. psutil 8.0 will
expose `phys_footprint` natively; the ctypes shim is removed when it ships.

**Budget enforcement (FR-020)**: the node refuses a load when
`memory_committed_bytes + estimate > memory_budget_bytes`, where committed is the sum of
estimates of engines in `starting`/`ready`. Estimates, not measurements, because admission on a
number that moves under load is not reproducible — the same rule feature 004's ledger adopts.

## R7. Durable acquisition without DBOS (yet)

**Decision**: The control plane keeps one `DownloadJob` row whose `stage` is a cursor over a
fixed sequence (`inspect → pull → verify_origin → export → import → verify_replica → done`).
A **reconciler** task in `coire-api` (5 s interval) advances each unfinished job by issuing the
node verb for its current stage and reading the node job's status; every node verb is
idempotent on the control plane's `job_id`, so a restarted control plane simply re-issues the
current stage and re-attaches. Model state follows from copy verification on every pass
(`ready ⇔ two verified copies`) rather than being set once and trusted.

**Rationale**: The constitution names DBOS for durable workflows, and feature 002 is specified
as "a DBOS workflow" — it is where inspect/convert/validate multiply the stages and where the
restart-resume guarantees become hard to hand-roll. This feature has one linear job whose
long-running steps all execute on nodes that persist their own progress (R5); the control
plane's part is a cursor. Introducing DBOS here would add its schema and runtime to `coire-api`
for a workflow with no branching and no compensation. Feature 002 wraps **these same node verbs**
in DBOS steps; the node contract does not change, and the reconciler is replaced, not extended.
That is the smallest change that meets the spec's resumption edge cases (Principle VII).

Recorded as **ADR-0005**, time-boxed to feature 002.

**Alternatives considered**: DBOS now — heavier than the problem, and pre-empts 002's design.
Celery/RQ — a broker on core for one job type; rejected.

## R8. An admin credential before feature 007

**Decision**: An **interim static admin bearer token** — Keychain item `coire-admin-token` on
core, mounted by `coire-up` as `/run/secrets/admin_token`. `require_principal()` returns an
`ADMIN`-kind principal when the bearer matches (constant-time compare) and the anonymous
principal otherwise; `require_admin` raises 403 and writes an audit row with `outcome: refused`.
Recorded as **ADR-0004**, extending ADR-0001's time-box: closed by feature 007.

**Rationale**: US2 and SC-002 require that a non-admin caller be refused on every acquisition
route with no side effect, and FR-003 requires an audit row per admin mutation. Feature 000
shipped no user-facing credential at all (ADR-0001), so there is nothing to distinguish an
admin from anyone else. A shared static token is the minimum that gives the routes a real gate
rather than a placeholder that "always succeeds" (which ADR-0001 explicitly rejects), and the
seam is the one 000 declared: only the body of `require_principal` changes; 007 replaces it
again with roles and API keys and no route signature moves.

**Audit**: the `audit_log` table is created here, append-only from its first row; 007 adds real
actors. `actor` is the literal `admin-token` until then.

## R9. Testing on CI when the engine only runs on macOS

**Decision**: three layers.

1. **Unit and contract** (Linux and macOS, every PR): registry state machine, fit arithmetic,
   manifest canonicalisation and hashing, the admin guard enumerated over every
   `/api/v1/admin/*` path in the generated OpenAPI document, node verbs against a **fake
   engine** (`coire_node.testing.fake_engine`: an HTTP server speaking `/health`, `/v1/models`
   and a one-token `/v1/chat/completions`, with a configurable load delay and a
   `--fail-on-start` mode) and a **fake Hugging Face** (a local HTTP fixture serving the Hub
   API shapes and file blobs for a synthetic 3-file repo).
2. **Integration** (Linux, the existing compose job): the stack plus **two node-agent
   containers** built from a test-only `apps/coire-node/docker/node-test.Dockerfile`, attached
   to a `coire-mesh-sim` network with static addresses `192.168.100.11/.12` and `extra_hosts`
   for `<name>.mesh`, so registration's mesh-subnet validation, the mesh-only export listener
   and the reconcile flow all run unmodified. They run the fake engine. The acquisition test
   pulls the real `mlx-community/Qwen2.5-0.5B-Instruct-4bit` (~280 MB, ungated) from Hugging
   Face — Principle VII's tiny-model rule — and asserts one origin, one replica, mesh-only
   transfer (the replica container has no route to the internet).
3. **Engine** (`macos-15` arm64 GitHub runner, free for this public repo): one real node
   agent with real `mlx_lm.server` and the same model; asserts ready-only-after-generation,
   footprint measured, unload releases memory, external kill detected within the interval,
   and adoption across an agent restart. `sudo ifconfig lo0 alias 192.168.100.11` gives it a
   mesh-subnet address. Marked `-m engine`; skipped elsewhere.

Anything needing a cable, a reboot or `launchctl` stays manual on the real cluster (quickstart
§3, §6) and is recorded in the PR.

**Rationale**: mlx runs only on Apple Silicon, so the Linux job cannot load a model; but
everything except the load is file, HTTP and Postgres work that Linux runs faithfully. Splitting
by what each platform can prove keeps every PR gated on the acquisition path while the engine
path is still real, not mocked, on a platform that can run it.

## R10. Node footprint additions

**Decision**: The agent wheel grows by `mlx-lm` (and its `mlx` dependency), `huggingface_hub`
(already transitive) — installed into the same `/opt/coire/envs/<version>` venv by the existing
`install.sh`; nothing is added to the Studio outside `/opt/coire` beyond the existing plist
and Keychain items, plus **one** new System-keychain item `coire-hf-token`. New directories:
`/opt/coire/models` (the store), `/opt/coire/state`, `/opt/coire/hf-cache` (metadata scratch;
weights never live here because `local_dir` writes plain files into the store). `uninstall.sh
--dry-run` enumerates them. The agent version becomes `0.2.0` and the env directory follows.

**Rationale**: the user's standing rule — install only what inference needs on the Studios.
`mlx-lm` *is* what inference needs; nothing else in this feature touches the host.

## R11. Contract compatibility with feature 000

**Decision**: `NodeStatus` gains five additive fields (`engines`, `jobs`, `memory_budget_bytes`,
`memory_committed_bytes`, `store_free_bytes`). Feature 000's `contracts/health-api.yaml` is
amended in place with a compatibility note naming them and its `NodeStatus` schema updated so
the 000 contract test keeps passing; `contracts/node-api.yaml` in this feature is the full node
contract from here on. Registration, `/health` and `/ready` are untouched. Principle III's
"spec amendment and migration/compatibility note" is satisfied by that note and by this entry.

## R12. First roster entry: Qwen3.8-27B for coding and reasoning (user decision, 2026-08-30)

**Decision**: When this feature ships, the first production model added is
**`mlx-community/Qwen3.8-27B-8bit`** for coding and reasoning, with
`mlx-community/Qwen3.8-27B-4bit` as the fast variant; both tagged `coding` and `reasoning`,
placement `single:auto`. Recorded here so the choice survives; the add itself is an admin
action after merge, not a task in this feature.

**Facts checked** (Hub, 2026-08-30): `Qwen/Qwen3.8-27B` released 2026-08; `mlx-community`
publishes 4bit (16.05 GB weights, 109k downloads), 8bit (~30 GB), bf16 (~54 GB), plus MTP and
OptiQ variants; config is `model_type: qwen3_5`, `Qwen3_5ForConditionalGeneration`, 64 layers,
4 KV heads, head_dim 256, `max_position_embeddings` 262,144, ungated, apache-2.0. KV cache is
256 KiB per token, so a 128k-token context costs 32 GB on top of the weights — the estimate
formula's 32k headroom is deliberately conservative and the admin can pin a larger one. Every
variant fits single-node on either Studio with room to spare; sharding is not needed for it.
The model is tagged `image-text-to-text`, but `mlx-lm` ships `mlx_lm/models/qwen3_5.py`
(verified on `main`, 2026-08-30): `Model` builds `TextModel` from `config["text_config"]` and
`sanitize()` skips `vision_tower`/`model.visual` weights, so `mlx_lm.server` serves the text
path natively and no `mlx-vlm` is needed. Two consequences for this feature: inspection reads
`num_hidden_layers`, `num_key_value_heads`, `head_dim` and `max_position_embeddings` from
`text_config` when present, falling back to the top level (R2, R6); and the vision weights are
pulled and replicated with the repo — they are part of the manifest — even though the engine
ignores them, which is the honest cost of "every file in the snapshot" until 002 introduces
`allow_patterns` per variant.

## R13. Explicitly deferred

- **Eviction, LRU, pinning, idle-TTL enforcement, the ledger** — feature 004. This feature
  stores `idle_ttl_seconds` and `placement_policy`, enforces a flat budget, and records
  `resident_delta_bytes`.
- **Draining** on unload, multiple instances per model, the instance state machine, issued node
  tokens — feature 005. `EngineProcess` is named to become `ModelInstance`'s subset.
- **Routing user traffic** to a loaded engine, `/v1/models` — feature 003.
- **Inspect-and-convert for non-MLX repos, variants, validation** — feature 002, which also
  adopts DBOS (R7).
- **Real users, roles, API keys** — feature 007 (closes ADR-0004).
- **Dashboard panel and alert** for downloads and engines — feature 009 (ADR-0003 extended;
  this feature emits the metrics: `coire_model_state`, `coire_download_bytes_total`,
  `coire_engine_state`, `coire_engine_resident_bytes`, `coire_engine_load_seconds`).
