# Quickstart and Acceptance: SFT Training Jobs

**Status**: Implementation validation guide, not evidence of completed implementation.
Commands for new training verbs/tests below become available through `tasks.md`. All production
work uses authenticated admin/gateway/node workflows. CI never targets the real Studios.

## 1. Prerequisites

- Branch `feat/016-sft-training-jobs`; current spec/plan/contracts read; feature migration applied
  with the existing dedicated migration image. Default-off training is enabled only in the test
  environment after compatible node installation.
- Postgres and simulated node integration environment for controller tests. Existing Keychain
  credentials remain outside Git; never record bearer values in the execution record.
- For the engine gate: an isolated Apple Silicon test Mac, frozen runtime with MLX 0.32.2 and
  mlx-lm 0.31.3, and an already acquired **local** <=1 GB text model in `COIRE_TEST_MODEL`.
  Check its manifest/licence and supported target modules. No implicit download from the test.
  The tiny fixture creates a second local artifact-store replica to exercise mirror commitment.
- For full acceptance: both real Studios available through documented control/data fabrics,
  verified base variants for LoRA, QLoRA and DoRA, compatible runtime on both, healthy measured
  JACCL link, and enough disk/memory for complete mirrored checkpoints. The small pinned ops model
  is part of the coexistence test rather than removed to make the test pass.
- Admin-provided text/tool datasets with recorded provenance. Test data and runtime artifacts stay
  outside Git; small synthetic fixture text/configuration may be versioned in tests.

If a credential, approved model/dataset or data-fabric prerequisite is missing, record the exact
blocker. Agent-run Studio validation is authorized by AGENTS.md; do not mark that work prohibited
merely because CI cannot access the Studios.

## 2. Static, contract and controller checks

From repository root, after implementation:

```bash
uv sync --all-packages --frozen
uv run ruff format --check .
uv run ruff check .
uv run mypy apps/ packages/
uv run pytest -q -m 'not integration and not engine'
uv run python -m coire_api.openapi --check
pnpm -C apps/coire-web test
pnpm -C apps/coire-web lint
pnpm -C apps/coire-web build
```

After schema changes, regenerate before freshness checking:

```bash
uv run python -m coire_api.openapi
pnpm -C apps/coire-web exec openapi-typescript ../coire-api/openapi.json -o src/api/schema.d.ts
```

Run the actual transaction/control integration gates using their configured test database:

```bash
COIRE_INTEGRATION=1 uv run pytest -q tests/integration/test_training_transactions.py tests/integration/test_training_lifecycle.py tests/integration/test_training_targets.py
```

Expected: atomic two-node reservation and draining marks, no pin/lease race, release only after
death proof, exactly one final outcome on cancel/publication races, no duplicate trainer/adapter,
authorization revocation, grant/listener separation and failover exclusion. Mocks alone do not
satisfy database-locking gates. Run migration upgrade/downgrade/upgrade on a disposable database,
including downgrade refusal with live jobs/references. Never downgrade an active production run.

## 3. Pinned-runtime and offline tiny-model gate

On the isolated Apple Silicon test Mac, with `COIRE_TEST_MODEL` already set to the verified local
fixture, run the new explicitly offline gate:

```bash
COIRE_TRAINING_ENGINE=1 uv run pytest -q -m engine apps/coire-node/tests/engine/test_training_runtime.py apps/coire-node/tests/engine/test_training_resume.py apps/coire-node/tests/engine/test_training_rendering.py
```

Required outcomes:

1. Direct bare APIs train an adapter, full state round-trips, and the resulting adapter is served
   through an authenticated node/gateway test path. Base tensors remain unchanged.
2. `model_file`, custom tokenizer code, escaping symlinks, missing local assets and remote IDs are
   rejected before executable loading. No downloads or remote telemetry occur.
3. Actual serving/tokenizer fixtures agree with training for Unicode, multipart text, thinking
   kwargs, template overrides, assistant tool-only turns and tool responses. Padding and prompt
   tokens do not enter final-assistant loss; unsupported prefix alignment fails visibly.
4. Train with nonzero dropout, accumulation >1, changing learning rate and a mixture boundary.
   Three interrupted-after-checkpoint trials restore the same optimizer update, RNG and next
   sample sequence as an uninterrupted run. Compare first and next 32 updates: FP32 trainable
   tensors/moments `rtol=1e-5, atol=1e-6`; loss `rtol=1e-4, atol=1e-5`. Exact serialized state
   equality immediately after restore is a separate requirement. A deliberately reset optimizer
   or RNG must make the negative control fail.
5. Interrupt before first checkpoint: record explicit step-zero recovery, not a false resume.
   Corrupt the newest checkpoint and recover the previous complete point. Missing all valid state
   fails clearly. No partial accumulation checkpoint is accepted.

Do not count skipped tests or a fixture that only tests fake numerical results as the engine gate.

To verify the full parameterization matrix, repeat the enabled gate with
`COIRE_TRAINING_PARAMETERIZATION=lora|qlora|dora` and `COIRE_TEST_MODEL` selecting the corresponding
already-acquired compatible base. The value is a test-fixture selection only, not a new runtime
configuration variable. LoRA/DoRA require a verified dense fixture; QLoRA requires the pinned
4-bit/group-64 fixture. Unsupported combinations fail rather than skip. The worker gate binds
the actual Studio name and includes authenticated extraction and bare-engine adapter generation.

## 3.1 External acceptance driver and staged wheels

`scripts/validate-sft-training.py` accepts a registry-bound recipe and calls only authenticated
admin/gateway routes. Credentials come from `COIRE_API_TOKEN` or `--keychain-service`; never pass
bearer values in command arguments. It checks submission replay, verbatim recipe, complete mirrored
checkpoints, independent loss history, private/unverified adapter identity and gateway generation.
Optional `--pause-at-update N` / `--cancel-at-update N` use separate trials and versioned commands.
Failed/expired trials request cancellation while retaining visible uncertainty if that cannot be
confirmed. Reports contain metadata only, are private new files outside Git, and never include
recipes, prompts, generated text or credentials. CLI execution refuses CI Studio targeting.

```bash
uv run python scripts/validate-sft-training.py --api-url "$API_URL" --recipe "$RECIPE_FILE" --report "$EXTERNAL_REPORT_FILE" --keychain-service "$ADMIN_KEYCHAIN_SERVICE"
```

Build/stage the locked node distribution through `scripts/build-node-wheel.sh`. The installer now
supports `--stage-only` to smoke/install an immutable candidate without changing the active link or
launchd service. Verify candidates before any controlled activation and preserve installed service
settings rather than replacing a customized plist with empty image defaults.

## 4. Admin dataset, recipe and console path

Use the installed Python CLI (not the separate `scripts/coire` shell command). Set `COIRE_API_TOKEN`
through the documented Keychain-backed development workflow. `API_URL`, `MODEL_ID`, `VARIANT_ID`,
`DATASET_FILE` and subsequent IDs below refer to test records/assets chosen by the administrator;
they are environment values, not hard-coded production identifiers.

```bash
uv run coire --api-url "$API_URL" data upload "$DATASET_FILE" --name sft-acceptance --format conversation --model "$MODEL_ID" --variant "$VARIANT_ID" --seed 42
uv run coire --api-url "$API_URL" data analyze "$DATASET_ID" --model "$MODEL_ID" --variant "$VARIANT_ID" --wait
uv run coire --api-url "$API_URL" train recipes
uv run coire --api-url "$API_URL" train validate "$RECIPE_FILE"
uv run coire --api-url "$API_URL" train submit "$RECIPE_FILE" --idempotency-key sft-acceptance-yaml-1
uv run coire --api-url "$API_URL" train events "$JOB_ID"
```

Prepare `RECIPE_FILE` through the Training console template and registry pickers; download the
rendered runnable YAML. No placeholder model/dataset ID is executable. Check the original recipe
download equals the submitted bytes and all resolved identities/defaults are displayed separately.
Submit an equivalent form with a distinct output name; compare normalized training fields excluding
output name/job identity. Retry original submit with the same key: same job. Change its learning rate
under the same key: conflict. Conflicting output names must not overwrite an adapter.

Repeat ingestion with plain text, prompt/completion and tool conversations. Test malformed rows,
image content, zero target tokens, oversized sequences, nonfinite recipe values, YAML duplicate
keys/tags/aliases and malformed tool relationships. Verify row diagnostics without echoed content.
Repeat split/mixture sampling; compare manifest/order digests and duplicate non-leakage.

UI acceptance: admin-only Training page, recipe/form controls, datasets/analysis, exact state,
source/resolved YAML, loss chart with attempt boundaries, checkpoint promotion, pause/resume/stop,
adapter visibility/verification. Keyboard navigation works; reconnecting SSE cannot resubmit work.
Unavailable feature 017 scores are labeled unavailable. Ordinary users cannot enter management.

## 5. Recovery and controls on the Studios

After a complete mirrored checkpoint:

```bash
uv run coire --api-url "$API_URL" train pause "$JOB_ID"
uv run coire --api-url "$API_URL" train checkpoints "$JOB_ID"
uv run coire --api-url "$API_URL" train resume "$JOB_ID"
```

- Restart scheduler only: one trainer remains and observation resumes.
- Restart node agent only: matching PID/create time is re-adopted; no duplicate process.
- Restart the Studio itself three times in separate trials after completed checkpoints: recover
  full state and finish. Use documented node operations; never kill an unrelated process by PID.
- Pause: <=60 s to durable checkpoint and process death, or explicit forced-stop/last-saved-step
  result. Cancel separate jobs: every owned rank stops <=5 s while nodes are healthy.
- Disconnect control traffic during cancel/recovery: job shows uncertainty and retains holds;
  node execution lease expires locally, late checkpoint/publication is refused. Record resolution
  once connectivity restores; core timeout alone must not free memory.
- Revoke initiating admin authority during a queued and a running job: delayed starts and final
  publication refuse; running work stops through the durable control lane.

## 6. Adapters, publication and independent verification

```bash
uv run coire --api-url "$API_URL" adapter show "$ADAPTER_ID"
uv run coire --api-url "$API_URL" adapter promote "$CHECKPOINT_ID" --name checkpoint-acceptance
uv run coire --api-url "$API_URL" adapter publish "$ADAPTER_ID"
uv run coire --api-url "$API_URL" eval harness "$VARIANT_ID" --adapter "$ADAPTER_ID" --engine-version "$ENGINE_VERSION"
```

Use the advertised exact `model@adapter` selector with the existing OpenAI SDK client and native
Chat. Test streaming/nonstreaming, `/v1/completions` and `/v1/messages` mapping. Keep base-only and
two distinct adapter instances warm at once and prove each request reaches its expected target;
usage and evaluation scorecards must name it. Adapter readiness requires both verified copies.

Before evaluation, write-capable run is refused even if base is verified. After a genuine passing
harness run, only that adapter passes. A base-only/other-adapter run token must not select it.
Unpublish/retire it and retire the base in disposable test cases: picker/routing refuse new access
without base fallback. Failover with only an adapter warm must not advertise/serve it as the base.

## 7. Real two-rank and coexistence release matrix

Run on real Studios through authenticated admin measurement/training routes. Record immutable
reports, model/dataset/runtime identities, bounds, durations and measured outcomes—not assertions.

| Gate | Required evidence |
| --- | --- |
| Parameterizations | LoRA, QLoRA and DoRA each train, checkpoint/resume, mirror and serve an adapter |
| Two ranks | Data-parallel run completes; same initial trainable digest; full per-node memory reservation; global batch rank assignment and common checkpoint |
| Rank/link fault | Lose rank 0 and rank 1 in separate trials; lose JACCL/data link; stop both, hold uncertainty, restore common checkpoint without stale output |
| Replication fault | Interrupt before manifest commit, expire/refresh transfer grant, corrupt receiver bytes; never mark partial output/checkpoint ready; no control-fabric fallback |
| Chat priority | Frozen recorded workload reused for 15-minute baseline and 15-minute mixed run; >=100 completed requests per resident target in each phase; pinned ops model included, exact target multiset and p95 query version recorded, <=1.5 s p95 first token for <=4k prompts, positive training progress, no swap growth |
| Guard trip | Live latency checks every 5 s over trailing 5 minutes, >=30 first-token samples/target, telemetry age <=60 s; insufficient evidence refuses new mixed admission, confirmed latency/thermal/memory breach pauses/stops training within deadlines; profile invalidated, pinned/live chat protected |
| Admission | Impossible full-rank fit refused; temporary queue reason/deadline visible; image/train mutual exclusion in both directions; shared locks prevent overcommit |
| Evict/restore | Eligible idle instance evicted then offered reload; changed pin/retire/placement is respected; sharded groups never lose an individual rank to training eviction |
| Cleanup | Dataset/checkpoint caps and staging sweeps; newest complete point/promoted artifacts preserved; core contains no tensor artifacts |

Use `coire train measure "$MEASUREMENT_FILE"`, then `train measurement "$MEASUREMENT_ID"` and
`train profiles` to inspect guarded evidence. Ordinary jobs cannot self-approve an unmeasured mix.
If a hardware gate fails, keep the relevant capability disabled and the feature task incomplete.

## 8. Observability, packaging and rollback

- With diagnostics disabled, inspect durable job loss/state, full audit, bounded sanitized logs
  and baseline metrics. Inject stalled job, uncertain recovery and memory/chat-guard faults;
  verify alert firing/clearing and useful runbook links. With diagnostics enabled, follow
  admission -> queue -> analysis -> load -> update -> checkpoint -> replicate -> publish spans.
- Build affected API/scheduler/migrate/node/agent/web distributions using existing frozen paths;
  check non-root/read-only/network/health policy, SBOM and current image scan gates. No production
  debug shell or remote-code dependency is introduced.
- Disable new training; pause/cancel and confirm/fence all trainers; preserve verified artifacts
  and metadata backups. Roll to previous image/node runtime only after incompatible work/targets
  are disabled. Exercise safe migration downgrade refusal and a clean disposable downgrade.
- Confirm existing chat, images, run orchestration, registry and text-only failover still pass
  their relevant regression suites, including <=20 ms p95 gateway overhead.

## 9. Evidence record and completion

During implementation create `specs/016-sft-training-jobs/execution-record.md` with commands,
revisions, fixtures, result counts, numerical comparisons, artifact digests, recovery timings,
coexistence metrics, real-cluster fault outcomes, scan/SBOM and rollback evidence. Do not include
credentials, datasets, tensors or raw user prompts. Link CI/build results when available.

Complete only when all required tasks and checks pass. A missing fixture/credential/licence or
measured hardware failure is a concrete blocker, not a checked box or skipped-success result.
