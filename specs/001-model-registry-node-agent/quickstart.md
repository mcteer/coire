# Quickstart: Validating the Model Registry and Node Agent

**Feature**: `001-model-registry-node-agent`. Each scenario maps to a success criterion in
[spec.md](spec.md). Shapes are in [data-model.md](data-model.md); routes in
[contracts/registry-api.yaml](contracts/registry-api.yaml) and
[contracts/node-api.yaml](contracts/node-api.yaml).

## Prerequisites

Feature 000's quickstart §0–§5 holds: the control plane is up on core, both Studios run the node
agent under launchd and appear `healthy` in `/health`. Additionally:

```bash
# core: the interim admin token (ADR-0004) — coire-up mounts it at /run/secrets/admin_token
security add-generic-password -a coire -s coire-admin-token -w "$(openssl rand -base64 32)"
export COIRE_ADMIN="$(security find-generic-password -w -s coire-admin-token)"

# each Studio: the Hugging Face token, System keychain (FR-005 — nowhere else)
sudo security add-generic-password -a coire -s coire-hf-token -w '<hf_...>' /Library/Keychains/System.keychain
```

The node agent wheel now includes `mlx-lm` and `huggingface_hub`; reinstall on both Studios
with `scripts/build-node-wheel.sh <node>` then `apps/coire-node/install.sh --wheel-dir /opt/coire/dist`.
`ls /opt/coire/models /opt/coire/state` exist and are empty.

The test model throughout is `mlx-community/Qwen2.5-0.5B-Instruct-4bit` (~280 MB, MLX-format,
ungated) so every step is fast and CI can repeat it (Principle VII).

`scripts/coire` is a thin `curl`+`jq` wrapper over the admin routes using `$COIRE_ADMIN`:
`coire model add|list|show|job|load|unload|retire`, `coire engines`, `coire nodes`, `coire audit`.

## 1. Admin adds a model; it reaches `ready` on both Studios — SC-001, SC-003, SC-004

```bash
scripts/coire model add mlx-community/Qwen2.5-0.5B-Instruct-4bit | jq '{id, state, memory_estimate_bytes}'
ID=$(scripts/coire model list | jq -r '.[] | select(.repo_id=="mlx-community/Qwen2.5-0.5B-Instruct-4bit") | .id')
watch -n2 "scripts/coire model job $ID | jq '{stage, percent, bytes_done, origin_node, replica_node}'"
```

**Expected**: 202 with `state: downloading`. The job walks `inspect → pull → verify_origin →
export → import → verify_replica → done`, unattended. `scripts/coire model show $ID` then shows
`state: ready`, `ready_at` set, two `copies[]` with `verified: true`, one `role: origin` and one
`role: replica`, both with the same `manifest_sha256` as the model. `ls /opt/coire/models/` on
both Studios shows `mlx-community--Qwen2.5-0.5B-Instruct-4bit/` with plain files (no symlinks)
and the manifest beside it.

**SC-004 — one external pull, mesh-only replication.** Before adding, on both Studios:
`netstat -ib | grep -E 'en0|bridge0'` and note the byte counters. After `ready`: the origin's
Wi-Fi RX grew by ≈ the model size and the replica's Wi-Fi RX did **not**, while the replica's
`bridge0` RX grew by ≈ the model size. `docker compose logs coire-api | grep 'stage=import'`
shows the source as `coire-edge-<x>.mesh`. Also assert on the registry: exactly one copy has
`role: origin`.

## 2. Progress is observable mid-download — US1 scenario 2

Add a larger ungated MLX repo (e.g. a ~4 GB 7B 4-bit) and poll the job. **Expected**:
`bytes_done` advances between polls, `percent` is monotonic, `stage` is `pull` with
`origin_node` naming the Studio that had the most free disk at add time.

## 3. Failure injection — SC-003

Each of these must leave the model in a truthful state and never `ready`:

| Injection | How | Expected |
|---|---|---|
| Checksum mismatch on the replica | while `stage: import`, on the **origin** Studio truncate one safetensors file by 1 byte (`truncate -s -1 …`) before the peer fetches it; restore afterwards | model `failed`, `copies[replica].mismatched_paths` names the file, `ls` on the replica shows the partial directory **removed** (FR-009) |
| Peer unreachable | `sudo ifconfig bridge0 down` on the replica for the whole import | model stays `replicating`, `state_reason` says the peer is unreachable; no egress fallback was used (`coire_fallback_requests_total` on api unchanged); bring the link back → job resumes and completes |
| Pull interrupted by network | `sudo ifconfig en0 down` on the origin for 20 s mid-pull | job stays in `pull`, `bytes_done` resumes from where it stopped rather than zero (edge case 2) |
| Pull interrupted by reboot | `sudo reboot` the origin mid-pull | after the node re-registers, the job resumes (`jobs/<id>.json` on the node) and completes without admin action (edge case 3) |
| Duplicate add | add the same `repo_id` again | 409, no second job, audit row `model.add outcome=refused` |
| Not MLX-format | add `meta-llama/Llama-3.2-1B-Instruct` (raw safetensors) | 422 `reason: not_mlx_format`, message names feature 002, **no** registry row, no bytes moved |
| Gated | add a gated MLX repo without licence acceptance | 422 `reason: gated` — not a generic download error (edge case 5) |
| Fits nowhere | temporarily set `DISK_RESERVE_BYTES` to 2 TB on api, add anything | 422 `reason: no_fit_disk` with `required_bytes` and `available_bytes` |

## 4. Only admins acquire and curate — SC-002

```bash
for m in POST; do curl -s -o /dev/null -w '%{http_code}\n' -X $m http://127.0.0.1:8080/api/v1/admin/models -H 'Content-Type: application/json' -d '{"repo_id":"mlx-community/Qwen2.5-0.5B-Instruct-4bit"}'; done   # no token
curl -s -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer wrong" -X DELETE http://127.0.0.1:8080/api/v1/admin/models/$ID
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://127.0.0.1:8080/api/v1/admin/models/$ID/retire
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://127.0.0.1:8080/api/v1/admin/models/$ID/load
```

**Expected**: 403 on every admin route without the admin token; `scripts/coire model list` is
unchanged; `scripts/coire audit | jq '.[] | select(.outcome=="refused")'` has one row per attempt.
The contract test enumerates every `/api/v1/admin/*` path from the generated OpenAPI document
and asserts 403 for each, so a new admin route cannot forget the guard.

**FR-005 — credential placement**:

```bash
for c in $(docker compose ps -q); do docker inspect $c --format '{{.Name}} {{.Config.Env}}' | grep -i hf_ && echo LEAK; done   # prints nothing
docker compose exec coire-api ls /run/secrets/    # postgres_password key_signing_secret node_tokens admin_token — no hf token
grep -r 'hf_' deploy/ apps/coire-api/ && echo LEAK   # nothing
```

## 5. Load, status, unload on a Studio — SC-005, SC-006, SC-008

```bash
scripts/coire nodes | jq '.[] | {name, memory_free: .status.memory_free_bytes}'      # record pre-load free memory
scripts/coire model load $ID | jq '{engine: .id, node, port, state}'                   # 202, state: starting
E=$(scripts/coire engines | jq -r '.[0].id')
watch -n1 "scripts/coire engines | jq '.[] | {state, pid, resident_bytes, resident_delta_bytes, cpu_percent}'"
```

**Expected**: `state` goes `starting → ready` only after the engine answers `GET /health` on its
own port; `pid` set; `resident_bytes` populated within one health interval of `ready`, and
`resident_delta_bytes` recorded (SC-008). On the Studio: `ps -o pid,pgid,rss,command -p <pid>`
shows `mlx_lm.server --model /opt/coire/models/mlx-community--Qwen2.5-0.5B-Instruct-4bit --host 192.168.100.1x --port 95xx`
in its **own process group** (pgid == pid). `lsof -nP -iTCP:95xx` shows it bound to the mesh
address only, and `curl http://coire-edge-a.local:95xx/health` from core **fails** (FR-018).

**Same-node double load (FR-019)**: `scripts/coire model load $ID` again → **200** with the
same engine id; `ps` shows one process.

**Budget refusal (FR-020)**: set `NODE_MEMORY_BUDGET_FRACTION=0.01` on one Studio's plist,
restart the agent, load → 409 `reason: budget` with required/committed/budget bytes. Restore.

**Unload**: `scripts/coire model unload $E` → 202 `stopping`; within a few seconds `stopped`;
`ps -p <pid>` gone; `scripts/coire nodes` shows `memory_free_bytes` back within 2 % of the
pre-load figure (SC-006) and `memory_committed_bytes: 0`.

**Failed start (US3 scenario 4)**: on the Studio, `chmod 000 /opt/coire/models/<slug>/config.json`,
load → engine reaches `failed` with `exit_code` and `exit_output` containing the engine's
traceback; never `ready`. `chmod 644` to restore.

## 6. Agent restart re-adopts; registry stays truthful — SC-007, SC-009

```bash
scripts/coire model load $ID; sleep 20; scripts/coire engines | jq '.[0] | {state, pid}'
ssh coire-edge-a.local 'sudo launchctl kickstart -k system/com.coire.node'      # restart the agent, not the engine
sleep 5; scripts/coire engines | jq '.[0] | {state, pid, state_reason}'
```

**Expected**: the same `pid` is still `ready`; the agent log shows `adopted engine <id> pid <pid>`;
`curl http://coire-edge-a.mesh:95xx/v1/models` (from core, over the mesh) still answers —
the engine never stopped (SC-007). The reconcile response after re-registration lists it under
`adopted`, with `dead: []` and `orphans: []`.

**Engine dies while the agent is down (US4 scenario 3)**:
`sudo launchctl bootout system/com.coire.node; kill <pid>; sudo launchctl bootstrap system /Library/LaunchDaemons/com.coire.node.plist`
→ within one reconcile the registry shows the engine `failed` with `state_reason: "process gone during agent restart"` and a `model.engine` audit row records the discrepancy.

**Orphan (US4 scenario 2)**: on the Studio, start an engine by hand outside the agent —
`/opt/coire/envs/current/bin/python3 -m mlx_lm.server --model /opt/coire/models/<slug> --host 192.168.100.11 --port 9599 &`
→ the next reconcile lists it under `orphans` with `engine_id: null`; the registry creates an
`orphan` row; it is neither adopted nor killed. `scripts/coire model unload <orphan id>` removes it.

**External kill (SC-009)**: with a `ready` engine, `kill -9 <pid>` on the Studio; time until
`scripts/coire engines` shows `failed` — must be ≤ `NODE_ENGINE_HEALTH_INTERVAL_S` (5 s default)
plus one prober interval.

## 7. Curation and visibility — US5

```bash
curl -s http://127.0.0.1:8080/api/v1/models | jq length                                          # 0: nothing published yet
scripts/coire model update $ID '{"visibility":"published","tags":["general"],"description":"small, fast"}'
curl -s http://127.0.0.1:8080/api/v1/models | jq '.[] | {display_name, tags, load_state, loaded_on}'
scripts/coire model update $ID '{"visibility":"admin_only"}'
curl -s http://127.0.0.1:8080/api/v1/models | jq length                                          # 0 again
scripts/coire engines | jq '.[0].state'                                                          # still ready — unpublish never unloads
```

**Expected**: the anonymous listing shows the model only while published, with `load_state`
reflecting the engine (`loaded` with `loaded_on: ["coire-edge-a"]`, or `cold`); unpublishing
removes it immediately and leaves the engine and files untouched. `ModelListing` carries no
paths or copies. Publishing a non-`ready` model returns 409. Setting `capability_profile.verified`
returns 422.

**Retire**: `scripts/coire model retire $ID` → 202; engines stop, both Studios' store
directories disappear, `state: retired`, and `scripts/coire model show $ID` still returns the
row with its transitions. `scripts/coire audit` shows `model.retire`.

## 8. CI equivalents

- `uv run pytest apps/coire-api apps/coire-node packages/coire-core` — unit and contract tests
  (Linux and macOS): the registry state machine, fit checks, manifest canonicalisation, the
  admin guard over every admin route, node verbs against a fake engine and a fake Hugging Face.
- `COIRE_INTEGRATION=1 uv run pytest -m integration` (Linux, compose): the stack plus two node
  agents as containers on a simulated mesh (`192.168.100.11/.12`) acquire the 280 MB test model
  end to end — §1, §3's duplicate/not-MLX/no-fit rows, §4, §7, and §6's adoption with the fake
  engine (research R9).
- `uv run pytest -m engine` on a macOS arm64 runner: one real node agent, real `mlx_lm.server`,
  the same model — §5 and §6's real-engine rows. Reported RSS/footprint and timings are attached
  as job artefacts.
- Everything in §3 that needs a cable or a reboot is manual on the real cluster and recorded in
  the PR per Principle VII.
