# Quickstart and Acceptance: Evaluation Verbs

This is the validation guide for feature 017. Implementation and authorized Studio acceptance are in progress; tasks.md and execution-record.md distinguish delivered code from outstanding release gates.

## Prerequisites

Use branch `feat/017-evaluation-verbs` based on merged 016. Read AGENTS, constitution, plan and contracts. Install with `uv sync --all-packages --frozen`; use the repository's existing web lockfiles (CI currently uses `npm ci`, local AGENTS documents pnpm). No new dependency is required. Run the local Postgres/isolated integration environment documented in `CONTRIBUTING.md` and existing test fixtures.

For real acceptance, use documented Keychain-backed admin credentials through `COIRE_API_TOKEN`/existing environment sourcing, never command-line secret values or logs. The credential must have an active human admin owner. Use acquired/validated/mirrored base and adapter targets, an eligible SFT dataset and approved 016 training profile; the judge must have a distinct registry model and base artifact. Keep evidence under a private directory outside Git. Set these non-secret bindings from real receipts: `COIRE_017_MODEL_ID`, `COIRE_017_VARIANT_ID`, `COIRE_017_ADAPTER_ID`, `COIRE_017_JUDGE_MODEL_ID`, `COIRE_017_JUDGE_VARIANT_ID`, `COIRE_017_DATASET_ID`. Never replace a missing prerequisite with a hard-coded engine path or an automatic download.

## 1. Automated gates

```bash
uv run ruff format --check .
uv run ruff check .
uv run mypy apps/ packages/
uv run pytest -q -m 'not integration and not engine'
uv run python -m coire_api.openapi --check
pnpm -C apps/coire-web test
pnpm -C apps/coire-web exec tsc --noEmit
pnpm -C apps/coire-web lint
scripts/pin-images.sh --check
```

Regenerate OpenAPI and TS whenever a schema/route changes, then check the resulting diff:

```bash
uv run python -m coire_api.openapi
pnpm -C apps/coire-web exec openapi-typescript ../coire-api/openapi.json -o src/api/schema.d.ts
```

Run Postgres migration/recovery and isolated tiny-model integration using their fixture prerequisites:

```bash
COIRE_INTEGRATION=1 uv run pytest -q apps/coire-api/tests/integration/test_evaluation_migration.py apps/coire-api/tests/integration/test_evaluation_recovery.py apps/coire-api/tests/integration/test_training_evaluation_triggers.py
uv run pytest -q tests/integration/test_run_core_isolation.py
promtool test rules deploy/observability/tests/evaluations.test.yaml
```

The required `evaluation-engine` CI job uses an isolated Apple Silicon runner,
the frozen workspace lock and an owned temporary PostgreSQL 17 instance. The
test-only PostgreSQL binaries are the existing database dependency (PostgreSQL
licence); no database service is started or application dependency added. On an
isolated Mac, acquire the fixtures through the authenticated node acquisition
verbs before switching generation offline:

```bash
uv run python tests/integration/build_evaluation_fixture.py
export COIRE_EVALUATION_ENGINE=1 COIRE_TRAINING_ENGINE=1
export COIRE_TEST_MODEL="$PWD/models/mlx-community--Qwen2.5-Coder-0.5B-Instruct-4bit"
export COIRE_TEST_JUDGE="$PWD/models/mlx-community--Qwen2.5-Coder-1.5B-Instruct-4bit"
export HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1
uv run pytest -q -rs tests/integration/test_evaluation_suites.py tests/integration/test_evaluation_training.py apps/coire-node/tests/engine/test_training_resume.py
```

Use the disposable Docker database by default, or explicitly point
`COIRE_TEST_POSTGRES_BIN` at PostgreSQL 17's binary directory for the native CI
fixture. Each fixture binds only loopback and cleans up its own cluster. Leave
`COIRE_INTEGRATION` unset for this isolated engine gate: that variable starts the
separate container integration stack. The fixture refuses core before model
access, and explicitly enabled missing engine prerequisites fail rather than skip.

Use a registered local ≤1 GB test model in the isolated Mac environment. Judge self-identity tests must use truly distinct fixture artifacts for candidate and judge where successful admission is expected. Real tiny-model generation may produce an asserted infrastructure outcome for malformed judging; deterministic stub tests separately prove valid rubric/pairwise aggregation, and real Studio acceptance below must produce valid actual judge results. Do not skip these gates because a fixture is absent; establish the prerequisite or record the concrete blocker. Run the repository CI image builds/policy/scans/SBOM for every affected image without lowering existing gates.

Expected: v1 recipe/resolved/checkpoint digests unchanged; new contracts and every API/node operation covered; unauthorized submission/evidence reads fail; task/judge scores never alter verification; duplicate collection/restart is idempotent; cancellation and holds reconcile; all required checks pass.

## 2. Register suites and run on demand

Create private acceptance storage:

```bash
COIRE_017_PROOF_DIR=$(mktemp -d /private/tmp/coire-017-acceptance.XXXXXX)
chmod 700 "$COIRE_017_PROOF_DIR"
export COIRE_017_PROOF_DIR
uv run coire eval suites templates
uv run coire eval suites list
```

Prepare suite registration JSON (configuration only, no code/credentials):

```bash
python3 - <<'SUITES'
import json
import os
from pathlib import Path
root = Path(os.environ['COIRE_017_PROOF_DIR'])
for kind in ('task-coding-instructions', 'judge-rubric', 'judge-pairwise'):
    registration = {'suite_id': kind, 'version': 1, 'template_id': kind, 'template_version': 1}
    if kind.startswith('judge-'):
        registration['judge'] = {'model_id': os.environ['COIRE_017_JUDGE_MODEL_ID'], 'variant_id': os.environ['COIRE_017_JUDGE_VARIANT_ID']}
    (root / f'{kind}.json').write_text(json.dumps(registration))
SUITES
uv run coire eval suites register --file "$COIRE_017_PROOF_DIR/task-coding-instructions.json" --idempotency-key 017-task-v1
uv run coire eval suites register --file "$COIRE_017_PROOF_DIR/judge-rubric.json" --idempotency-key 017-rubric-v1
uv run coire eval suites register --file "$COIRE_017_PROOF_DIR/judge-pairwise.json" --idempotency-key 017-pairwise-v1
uv run coire eval harness "$COIRE_017_VARIANT_ID" --adapter "$COIRE_017_ADAPTER_ID"
uv run coire eval task --suite task-coding-instructions --suite-version 1 --model "$COIRE_017_MODEL_ID" --variant "$COIRE_017_VARIANT_ID" --adapter "$COIRE_017_ADAPTER_ID" --against-base
uv run coire eval judge --suite judge-rubric --suite-version 1 --model "$COIRE_017_MODEL_ID" --variant "$COIRE_017_VARIANT_ID" --adapter "$COIRE_017_ADAPTER_ID" --against-base
uv run coire eval judge --suite judge-pairwise --suite-version 1 --model "$COIRE_017_MODEL_ID" --variant "$COIRE_017_VARIANT_ID" --adapter "$COIRE_017_ADAPTER_ID" --against-base
```

Enable new admission only after migration/node/agent capability preflight; the evaluation feature defaults off. Use an otherwise idle admissible node for initial runs, or first qualify a coexistence profile through the controlled measurement operation. Inspect actual node/container placement and prove core never executes the suite. Record real judge identity, case evidence and score provenance. Repeated low task/judge scores must not change the adapter's harness status.

## 3. Automatic final and checkpoint evaluations

Copy `recipes/training/sft-evaluated.yaml` into the private evidence directory and bind registry IDs, dataset/adapter slug and supported training settings. Use two pre-final checkpoint updates aligned to checkpoint cadence and declare task plus rubric judge suites; final evaluation is implicit for each declared suite. Obtain the normal 016 measurement/admission prerequisites for that exact training configuration. The recipe carries no secrets or data bytes.

```bash
uv run coire train validate "$COIRE_017_PROOF_DIR/sft-evaluated.yaml"
uv run coire train submit "$COIRE_017_PROOF_DIR/sft-evaluated.yaml" --idempotency-key 017-evaluated-training
```

Capture the returned job ID in `COIRE_017_JOB_ID`, then:

```bash
uv run coire train show "$COIRE_017_JOB_ID"
uv run coire train checkpoints "$COIRE_017_JOB_ID"
uv run coire eval list --job "$COIRE_017_JOB_ID"
```

Expected: each declared checkpoint is fully committed/mirrored before both trainer ranks stop; holds are not released early; private checkpoint adapters serve only through exact grants; evaluation runs sequentially and training resumes the exact checkpoint. At final success, the ready output adapter shows automatic task/rubric base-versus-adapter scores in the console without manual eval submission. The training state stays succeeded if one evaluation is deliberately made unavailable.

Use documented service/node restart controls to interrupt after trigger commit, after phase completion before collection acknowledgment, and during evaluation-owned pause. Confirm one trigger/result per identity and unchanged previous result digests. Inject a newer admin pause/cancel while evaluation finishes; confirm no unwanted resume. Repeat the checkpoint pause on an already supported two-rank 016 training configuration and retain both stop/recovery receipts. Do not extend the supported training matrix to complete 017.

## 4. Failure, comparability and contamination matrix

Automated tests exercise every case; real acceptance samples the principal operational failures without changing live model artifacts:

| Scenario | Required evidence |
|---|---|
| Self-judge with same model, variant, alias/base digest or adapter | Pre-inference refusal; zero launched worker; audit reason. |
| Malformed judge output / unavailable judge / model load failure | Bounded retries or fail; infrastructure outcome, null aggregate, trained adapter unchanged. |
| Wrong task answer | Successful measured execution with low score. |
| Suite/case/runtime/decoding/judge mismatch | Non-comparable reasons; no numeric delta. |
| Exact reused training input with changed completion | Overlap detected by input projection, not missed through full-example hashing. |
| Missing dataset/evidence / retired subject | Explicit unavailable/expired/non-runnable status; historical metadata readable. |
| Same idempotency key / explicit rerun | Same request replays; rerun creates new result; old digest stable. |
| Owner demotion, token expiry, forged collected result | Work stopped/refused, no unauthorized verification change. |

## 5. Serving coexistence, kill and rollback

Create `measurement.json` following `EvaluationMeasurementRequest`, with actual resident instance IDs, exact candidate/judge targets, fixed ≤4k-token prompts and enough duration to collect ≥100 baseline and ≥100 mixed requests per resident. Submit and inspect:

```bash
uv run coire eval measure --file "$COIRE_017_PROOF_DIR/measurement.json" --idempotency-key 017-evaluation-coexistence
uv run coire eval measurement "$COIRE_017_MEASUREMENT_ID"
```

Expected: TTFT ≤1.5s p95, gateway overhead ≤20ms p95 excluding inference, zero failed chat requests/swap growth and complete actual-target measurements. With the passing exact profile, repeat normal task/judge admission alongside chat. Change a workload/runtime/resident fingerprint and confirm stale profile refusal. Inject telemetry staleness or breach using the test/acceptance fault controls; evaluation yields and serving holds stay protected.

Start another run with `--no-wait`, capture its ID/version, then cancel through CLI/console. Measure credential invalidation and reachable sandbox stop ≤5s. Exercise node-unreachable cleanup in the isolated recovery suite and verify held reservations remain visible until proven stopped. Display the jobs panel and trigger the stuck-cleanup alert with optional diagnostics disabled.

Disable new admission, let reconciliation/cleanup continue, and drain/cancel remaining work. Convert unresolved evaluation-owned pauses to explicit admin pause before binary rollback; preserve additive history tables and old v1 resumes. Test migration downgrade on a disposable copy only after all pins/work drain. Restore the tested release and verify history/evidence-expiry labels remain accurate.

## Evidence and release record

Implementation creates `specs/017-evaluation-verbs/execution-record.md` with commands, versions, result counts, timing/latency measurements and private proof paths/digests. Raw credentials, model outputs, datasets and receipts stay outside Git. Record actual CI run/check status and image scan evidence; no task is checked off on skipped or failing required acceptance. All four stories plus release gates are required; the US1 demo is not the full release.
