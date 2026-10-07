# Feature 016 — operations/packaging evidence

## Scope and status — 2026-10-05

Executed on `feat/016-sft-training-jobs`, preserving the existing uncommitted work.
Read AGENTS, constitution 2.0.0, architecture, roadmap, spec, plan, tasks and the
common execution record. This is the separate evidence file requested for the
operations slice; the common execution record/tasks were not edited.

Owned changes: deployment/image packaging/docs, training telemetry, new scheduler
metrics reducer/poller and their tests, plus the native runtime installer smoke
check and its existing packaging test. No shared contract, `workers.py` or
`main.py` edits. No Studio contact/install, network mutation, model/tokenizer/Metal
work on core, commit or push. Disposable local PostgreSQL/collector containers and
one synthetic Docker dataset volume were removed after verification.

Constitution: I (unchanged bare engine hooks), II (only metadata/upload bytes on
core), II-a (distinct hardened images), III (no wire-shape changes), IV (private
volume/content-free signals), V (no model acquisition), VI (baseline metrics/rules/
panels), VII (meaningful persisted-state, exporter and packaging checks).
Feature acceptance is **incomplete**; no full-task checkbox or hardware result is implied.

## Implemented integration interface

```python
from coire_scheduler.training_metrics import poll_training_metrics

# Scheduler lifespan, after configure_telemetry() and init_engine():
training_metrics_task = asyncio.create_task(poll_training_metrics(stop))
# Run regardless of TRAINING_ENABLED. On shutdown, before dispose_engine():
stop.set()
await training_metrics_task
```

Signature: `poll_training_metrics(stop: asyncio.Event, *, interval_s: float = 5.0,
emitter: TrainingBaselineMetrics | None = None) -> None`. Interval is >0 and <=15 s.
No worker registration or DBOS workflow is necessary. Caller owns lifecycle; this
slice intentionally provides the interface without editing shared main/workers.

For a single refresh use `await load_training_metrics(session)` in a **fresh**
session. The first SQL statement sets REPEATABLE READ, READ ONLY; database time
and all reduction inputs come from that consistent transaction. Publication
happens only after successful session exit. Failed polls log no exception/SQL
text and retain the prior timestamp/values; before first success, gauges are absent.

Actual Prometheus-exported gauges:

* `coire_training_snapshot_timestamp_seconds` — last successful snapshot DB epoch.
* `coire_training_jobs{state}` — all 12 nondeleted states, explicit idle zeroes.
* `coire_training_progress_oldest_seconds` — oldest current-fence running attempt's
  positive-update, non-rolled-back train sample; validation/other attempts cannot reset it.
* `coire_training_recovery_oldest_seconds` — oldest continuous recovering segment
  or unresolved unknown/stopped-without-proof/old-fenced ownership.
* `coire_training_checkpoint_pending_oldest_seconds` — oldest uncommitted,
  unpurged staging/replicating checkpoint, independent of the current job label.
* `coire_training_guard_overdue{reason}` — five closed memory/latency/thermal/
  lease/cancel reason series. Five-second stop/cancel and sixty-second pause
  violations persist until every rank has stored stopped-at + immutable stop proof.
  A successful command receipt or terminal job label does not clear process proof.

Exporter check measured **21 series** (12 state + 5 reason + 4 scalar), with
correct OTel `s` -> Prometheus `_seconds` translation and no resource IDs/content
as labels. State ages use the earliest event in the current continuous state
segment, falling back to persisted updated-at; repeated matching events do not reset
them. Unknown ownership has no dedicated transition timestamp in this schema,
so its age conservatively starts at attempt creation, surviving process restarts.

**Integration requirement:** persist transition events consistently, and do not
refresh updated-at on repeated unresolved observations without such an event.
Dedicated unknown-transition persistence would be needed for exact (rather than
conservative) unknown-ownership age. No contract/migration was changed here.

## Commands and measured results

### Metrics, native packaging regression and topology

```bash
COIRE_INTEGRATION=1 uv run --frozen pytest -q \
  apps/coire-api/tests/unit/test_training_metrics.py \
  apps/coire-api/tests/integration/test_training_baseline_metrics.py \
  apps/coire-api/tests/integration/test_training_metric_export.py \
  tests/unit/test_node_install.py tests/unit/test_image_storage_topology.py
```

Final result: **26 passed, 0 skipped**, 3.63 s. This includes genuine disposable
Postgres queries, immutable train vs validation/discarded samples, all-rank proof
clearing, repeated recovery events/fresh sessions, unknown/fenced attempts,
failed-poll preservation and an actual isolated hardened collector with loopback
OTLP ingress/Prometheus scrape. Native installer calls were mocked, not executed
on core. The mock regression proves exact version/API checks and stripped Hub/
reporting credentials with offline environment flags.

Changed-file Ruff check/format: pass. Strict mypy on telemetry, scheduler reducer,
metric unit tests, native installer and native packaging test: **5 files pass**.
`git diff --check`: pass. `scripts/pin-images.sh --check`: pass.

Effective Compose configuration (including diagnostics/MCP/ops profiles) was
parsed through `docker compose ... config --format json`: dataset volume mounts
exist **only** on API and scheduler. Both are UID/GID 65532, read-only rootfs,
cap-drop ALL, healthchecked, default TRAINING_ENABLED=false. No network membership
was changed. `COIRE_TRAINING_ENABLED` and input URL plus shared dataset/analysis/
queue bounds are now explicitly mapped into both services. Other documented
runtime settings keep typed defaults unless explicitly supplied by an override.

### Alerts and packaged rules

```bash
docker run --rm --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges --entrypoint /bin/promtool \
  coire-prometheus:016-operations check rules /etc/prometheus/rules/training.yml
docker run --rm --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges \
  --tmpfs /tmp:rw,noexec,nosuid,size=64m,mode=1777 \
  --mount type=bind,src="$PWD",dst=/workspace,readonly --workdir /workspace \
  --entrypoint /bin/promtool coire-prometheus:016-operations \
  test rules deploy/observability/tests/training.test.yaml
```

Both pass; **six packaged rules** found. Existing threshold/pending/firing/
clearing/annotation tests pass. Added lease/cancel independent proof-clearing and
fresh-emitter-cannot-mask-stale-emitter controls; aggregate alerts expose no
instance/reason/resource IDs. Prometheus image now includes `.yml` training rules;
Grafana image includes Jobs dashboard with durable history links and diagnostics
OFF guidance. This verifies rules/naming/packaging, not live Alertmanager delivery.

### Production images

Existing CI-equivalent builds (native linux/arm64; nothing deployed):

```bash
docker build --platform linux/arm64 -f apps/coire-api/docker/api.Dockerfile -t coire-api:016-operations .
docker build --platform linux/arm64 -f apps/coire-api/docker/scheduler.Dockerfile -t coire-scheduler:016-operations .
docker build --platform linux/arm64 -f deploy/compose/prometheus.Dockerfile -t coire-prometheus:016-operations .
docker build --platform linux/arm64 -f deploy/compose/grafana.Dockerfile -t coire-grafana:016-operations .
```

All four builds pass. Final local immutable image IDs (not pushed registry digests):

| Image tag | Docker image ID |
| --- | --- |
| `coire-api:016-operations` | `sha256:ef2daa01418854e2ec506699cabc65ddd9d0056f0aa1a988b77f0edc7cd82dc3` |
| `coire-scheduler:016-operations` | `sha256:24f88a36303d2b671ff2afec3cf67c0e7f2490ba8dfffe6f86410a6bbd50ad86` |
| `coire-prometheus:016-operations` | `sha256:eceaecc20ed9ab34ebbc1da6de49f0e44cf995be591cec6b2659a1f2f1702c09` |
| `coire-grafana:016-operations` | `sha256:c4a2ea4f0af3cbbfb0d2398a256b313266255f07562b70f13aa20f3a7c34bfe5` |

For each image ran the existing `scripts/image-policy.sh IMAGE DOCKERFILE`:
**all pass** (no shell/package manager, non-root, read-only-compatible, arm64,
exec-form entrypoint, pinned FROMs; API/scheduler core-harness boundary passes).
Prometheus policy's no-dependency probe exits 2 without filesystem violations;
that result establishes compatibility only, not service readiness. Packaged
rule validation passes separately. Compose provides the production flags/config.

Final image scans: `trivy image --severity CRITICAL --exit-code 1
--ignore-unfixed=false --format json --output OUT IMAGE`: **all four pass**, no
critical findings/secret gate failures. Trivy 0.74.0; this is the existing CI
critical gate, not a claim of zero vulnerabilities at all severities.
`syft IMAGE -o spdx-json=OUT`: **four SPDX SBOMs generated**.

Scan/SBOM artifacts are outside Git in
`/var/folders/cq/hyfmktwx1jq_vh86pcn21w800000gn/T/opencode/016-operations/`.
Final SPDX SHA-256s:

| File | SHA-256 |
| --- | --- |
| `api.spdx.json` | `8b0ee90b156b1d9512f48de3b3690d9b2c56e19ce1c17555c8d101f15a55298d` |
| `scheduler.spdx.json` | `28f25636f173cb0cd26a95ce53e30c1e0e6dd3fac7beb462339f7ebec386bcb6` |
| `prometheus.spdx.json` | `d32b8e106a0449a3cf1069edd205b912d7ff92c2380348f8dfecf5fdab50534e` |
| `grafana.spdx.json` | `9ae723147e2c79107be12a00c1fef3899ae26011602afc0e4c9bbbaab11a998d` |

Installed non-editable API recipe-loader smoke found exactly `sft-lora`,
`sft-qlora`, `sft-dora` from the image, without engine imports. Both images also
package image recipe assets. Initial image smoke caught Docker creating the empty
dataset directory as 0755 despite builder chmod. Corrected with explicit runtime
COPY `--chmod=0700`; final UID 65532/mode 0700 smoke passes. A disposable
`coire-016-operations-dataset-test` named volume proved API writes and scheduler
reads/deletes the synthetic probe with network none, read-only rootfs, dropped
capabilities, no-new-privileges, no secrets. The test volume was removed. Final
recipe/mode smoke passes (only the expected absent test-secret-directory warning).

### Frozen node wheelhouse

`scripts/build-node-wheel.sh --local-only`: **pass**. Built core/node wheels and
sdists, exported locked requirements/pylock, selected and hash/size-verified
**87 CPython 3.13/macOS arm64 dependency wheels**. No Studio contacted, no engine
import/model load. Reused existing cached dependency wheels only after lock hash
verification; no new dependency/pin was introduced.

| Ignored output | SHA-256 |
| --- | --- |
| `dist/coire_core-0.1.0-py3-none-any.whl` | `01049bc3555a1c995d2d80fa84e00aa818e363704714e59a38bd9e11b06fb3ed` |
| `dist/coire_node-0.2.0-py3-none-any.whl` | `1ed8de7b780310f95f93262023e2289fa3d1d7e9822f028564c1bdc6e5c51c2b` |
| `dist/node-wheels/pylock.coire-node.toml` | `f9e2ce3fc1b1ae70e133271fc21592d72852f944dbd1c747403e79163cc8d248` |
| `dist/node-wheels/requirements.txt` | `072fca429ecee502d2fc69ced2086aa195f8e06c57741124f4038286f7480fcd` |

Existing staging/install uses locked HTTPS/hash selection, `--require-hashes
--no-index`, no-deps workspace wheel installation, dependency check and staged
verification before symlink flip. Extended native verification checks offline
training/analysis modules, PyYAML/safetensors availability, MLX 0.32.2/mlx-lm 0.31.3
and upstream train callback/optimizer/data/loss parameters. Installer rollback
unit tests remain green; no actual native environment publication or numerical
runtime acceptance was performed in this slice.

## Task coverage and concrete outstanding prerequisites

* **T115/T116:** reducer/emitter, rules tests, exporter names, rule/dashboard image
  packaging are verified. Caller must wire the scheduler lifespan task. Runtime
  diagnostics-off alert delivery/clearing and on-profile attribution remain untested.
* **T117:** private volume, API/scheduler-only access, empty-volume ownership,
  effective Compose bounds and frozen local wheelhouse are verified. Native
  deployment/import/symlink-flip/restart/rollback must run on an authorized Studio;
  this task explicitly excluded Studio changes. Existing staging scripts required
  no functional change for their hash-verified local build.
* **T118:** runbook/architecture/design now match uploaded text/tool SFT, Studio
  rendering, persisted loss/history, exact adapters, feature-017 unavailable
  wording and disable/drain boundaries. Actual backup restore/drain/fence/service
  rollback results are still required; no measured native success is claimed.
* **T121:** four images, policy, critical scans, SPDX and local frozen wheelhouse
  verified with immutable outputs above. Broader feature images changed by other
  owners and final integrated release builds are outside this slice's verification.

Precise remaining inputs/actions:

1. Scheduler main owner adds/awaits the polling task and consistently persists
   state transitions. No additional contract/worker-registration requirement for
   baseline polling. Unknown transition timestamp remains conservative as noted.
2. A compatible running feature build is needed to measure live Alertmanager
   delivery with diagnostics OFF and stage attribution with diagnostics ON.
3. Confirm same-origin `/api/v1/admin/training/...` links and provide/verify actual
   HTTP resolution for `/docs/runbooks/sft-training#...`; repository Markdown alone
   does not establish a deployed documentation route.
4. Native package install/smoke and rollback require authorized Studio execution;
   no missing credential was discovered because no Studio was contacted here.
   Full native/tiny-model/JACCL/coexistence/physical artifact/restore gates remain
   those recorded in the common execution record, not newly measured prerequisites.

No local verification prerequisite remains missing for this operations slice.
Task completion beyond these measured portions must not be inferred from this file.
