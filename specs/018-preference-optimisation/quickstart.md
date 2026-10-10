# Quickstart and Acceptance: Preference Optimisation

This guide prescribes reproducible implementation validation. Measured automated and real-Studio outcomes are recorded in [execution-record.md](execution-record.md); planning itself ran no training or deployment.

## Prerequisites

Use `feat/018-preference-optimisation` based on `d2e4bbf`. Read AGENTS, constitution, spec, plan and both contracts. Install through `uv sync --all-packages --frozen`; local web checks use documented pnpm, CI keeps its npm lockfile flow. The API pins `uvloop==0.22.1` (MIT / Apache-2.0) for I/O scheduling; see ADR 0014 and the execution record. Existing Postgres integration fixtures and hardened service images remain required.

For engine tests use isolated Apple Silicon outside core, with approved local tiny dense and affine 4-bit/group-64 variants (≤1 GB each) and the existing native training fixtures. Models are prepared through the documented fixture/admin acquisition workflow, then tested offline. Explicitly enabled missing prerequisites fail; ordinary CPU test deselection does not count as native qualification. CI never contacts real Studios.

For real Studio acceptance use Keychain-backed credentials through documented admin/gateway/node paths, acquired/validated mirrored inputs, a compatible SFT parent adapter for each base variant, published text serving targets, and a distinct evaluation judge where required. Keep raw content, samples, tokens and outputs under a private directory outside Git. coire-node owns every engine and training process. Record immutable digests, operation receipts and sanitized measured summaries in a new `execution-record.md` during implementation.

## Automated gates

After contract changes, regenerate rather than hand-edit schemas:

```bash
uv run python -m coire_api.openapi
pnpm -C apps/coire-web exec openapi-typescript ../coire-api/openapi.json -o src/api/schema.d.ts
uv run python -m coire_api.openapi --check
uv run ruff format --check .
uv run ruff check .
uv run mypy apps/ packages/
uv run pytest -q -m 'not integration and not engine'
pnpm -C apps/coire-web test
pnpm -C apps/coire-web exec tsc --noEmit
pnpm -C apps/coire-web lint
scripts/pin-images.sh --check
```

Use isolated Postgres/Compose fixtures per CONTRIBUTING, then run the new targeted integration files and the existing SFT/evaluation regressions:

```bash
COIRE_INTEGRATION=1 uv run pytest -q apps/coire-api/tests/integration/test_preference_migration.py apps/coire-api/tests/contract/test_feedback_export_publication.py apps/coire-api/tests/integration/test_feedback_withdrawal.py
COIRE_INTEGRATION=1 uv run pytest -q -m integration
promtool test rules deploy/observability/tests/feedback.test.yaml
promtool test rules deploy/observability/tests/training.test.yaml
```

The reproducible browser keyboard gate uses an existing Chromium installation and
no additional package dependency. It uses private temporary profiles, imports the
actual UI components and captures synthetic typed transport receipts:

```bash
COIRE_BROWSER_BIN=/absolute/path/to/Chromium uv run python tests/browser/feedback_keyboard.py
```

The full bounded database load gate is:

```bash
COIRE_INTEGRATION=1 uv run pytest -q -s apps/coire-api/tests/contract/test_feedback_performance.py
```

On the isolated Mac with native fixtures configured:

```bash
COIRE_TRAINING_ENGINE=1 HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 uv run pytest -q -rs apps/coire-node/tests/engine/test_preference_loss.py apps/coire-node/tests/engine/test_preference_runtime.py apps/coire-node/tests/engine/test_preference_resume.py
COIRE_TRAINING_ENGINE=1 HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 uv run pytest -q -rs apps/coire-node/tests/engine/test_preference_worker.py apps/coire-node/tests/engine/test_preference_measurement.py apps/coire-node/tests/engine/test_training_resume.py
```

Wire required preference engine coverage into existing isolated Mac CI with failing missing prerequisites. Keep full image build/scan/SBOM and integration gates. A required skipped test is not a passing gate. Do not lower numerical tolerances or relax authentication/network/ledger safeguards to complete validation.

## Scenario A — Explicit feedback, export and analysis (US1)

1. Use an owner account and a newly completed local text-only turn after provenance capture is installed. Confirm disclosure, toggle and comparison eligibility; historical turns lacking exact provenance give a reason.
2. Thumb a completed response, edit/clear it and replay its request ID. Verify current version and content-free audit, no duplicate charge, no exportable pair synthesized from thumbs.
3. Request a comparison of the latest eligible response. Retry the request and reconnect SSE; one generation/usage charge results. Before choice the original remains active and new sends/model changes conflict. Choose the regenerated response; a subsequent normal turn includes only that answer.
4. Verify dismiss, cancel, expiry/restart, identical/empty/overflow output, unavailable exact variant and a prompt with an earlier file/tool/code/image contribution all release or refuse correctly with no exportable pair.
5. Collect ≥20 distinct eligible pairs across ≥2 prompt groups. Through Training admin or `coire feedback export`, select model/date/tag/owner/source filters. The command returns an export ID; `coire feedback export-show` shows publication/dataset/analysis/warnings and cleanup.
6. Confirm exactly one effective judgement per pair, immutable content/provenance, no hidden reasoning and deterministic grouped splits. Run analysis for the selected target; both sides pass masks/token lengths before readiness. Export fewer than 20 to get a warning; one prompt group fails readiness clearly.
7. Inject crash before/after file rename and DB commit; restart scheduler. No duplicate dataset, stale source version or leaked uncounted staging bytes; cancellation before publication works and after publication points to dataset retirement.

## Scenario B — Training from SFT or bare base (US2)

1. Bind `recipes/training/dpo.yaml` and `orpo.yaml` to ready preference datasets, exact base variants and matching SFT parent adapters. V3 options are explicit; no arbitrary model/path reference. Submit via existing validate/measurement/job API or `coire train`; also prove console submission parity.
2. Enable preference admission through documented config for controlled qualification only. Run objective-aware guarded measurement using the actual runtime, pair batch sizes, probes, reference and checkpoint peaks. Ordinary jobs without fresh matching profiles are refused. SFT or other-objective profiles never substitute.
3. Exercise both objectives × dense LoRA/affine QLoRA × bare/parent start on tiny fixtures. Initial DPO loss equals log(2) when policy/reference match; reference/base tensors do not change. ORPO zero weight equals chosen NLL. Compare loss/gradients against the independent scalar oracle under the contract tolerances.
4. Train, pause at a complete mirrored checkpoint, restart scheduler/node process and resume. Match uninterrupted losses/tensors within declared tolerances, with exact sampler/RNG; reference still equals original initialization. Reject corrupted copies, missing input pins and incompatible template/runtime versions without scratch restart.
5. Serve the resulting exact base@adapter through the gateway; verify complete standalone result tensors and ancestry. Retire an unpinned ancestor and confirm child serving still works. A newer result remains unverified even if the parent was verified; write-capable use fails until its own harness gate passes.
6. With declared v3 task/judge suites, test final obligations and checkpoint evaluation pause/resume; empty-suite v3 creates none. A newer admin pause/cancel defeats automatic resume. Result scores remain separate from training success and harness verification.
7. Refuse preference DoRA/data-parallel/full tuning, unsupported architecture, nonzero dropout, incompatible parent layout, overlength/identical tokenized pairs and impossible memory before launching owned processes. Re-run frozen SFT v1/v2 hash and full resume regressions.

## Scenario C — Withdrawal and disclosure (US3)

1. Disable capture in chat while generation streams, while a pair is reviewed and while export stages. Commit setting change before each competing write/publication. No new contribution bytes/rows become visible; prior contributions disappear immediately and in-flight work cannot resurrect them.
2. Re-enable and replay old request IDs/SSE cursors. Old capture generations remain withdrawn. New chat turns can contribute again; the ordinary previously selected chat answer remains conversation history.
3. Delete a contributing conversation during export. Publication excludes it if deletion won the commit ordering. Test current source-version changes similarly; bounded rebuild or explicit failure, never stale publication.
4. Publish a dataset first, then withdraw/delete before its token analysis completes. It remains an immutable historical snapshot; subsequent exports exclude the contribution. Existing derived jobs/adapters remain unchanged, matching the accepted disclosure.
5. Run bounded purge sweeps after crash/restart and with preference training disabled. Verify copied bodies in provenance, pairs, replay payloads, staging and caches are removed within 24 hours; audit/membership retains no content. Inspect DBOS args/results and receipts for absence of text. Confirm quota remains counted until physical cleanup proof.
6. Exercise keyboard-only controls, loading/error/conflict announcements, setting persistence across refresh and capture-disabled controls. Run owner/settings/refusal audit coverage with both browser and scoped user keys; service/run keys cannot impersonate users.

## Scenario D — Admin review (US4)

1. Submit an explicit complete pair; use an admin account to review it through the bounded queue. Two admins race the same version: one succeeds, the other receives 409. Skip is revisitable and does not create a label.
2. Confirm admin choice is attributed, leaves conversation selection unchanged and cannot access an opted-out/deleted/foreign-ineligible contribution. Revoking admin/key during review/export prevents the final mutation/publication.
3. Make owner and admin choose opposite candidates. Default export selects owner; explicit admin export selects admin; never duplicate one pair in one dataset. Verify date/tag filtering against the effective selected source.

## Performance, observability, cluster and rollback

Seed 10,000 bounded synthetic feedback records with 10 concurrent readers. Record p95 owner mutation ≤500 ms and review page ≤1 s; 10,000-pair publication ≤5 minutes after execution admission, excluding Studio analysis. Verify oversize rows/selections, per-user pending pair, global export queue and storage caps. Use non-sensitive fixtures and record the machine/configuration and raw timing location.

With diagnostics disabled, prove content-free audit/durable progress, baseline export/cleanup/training alerts and dashboard links to authenticated histories. Prometheus rule tests must cover alert firing and recovery; no prompt, owner ID or arbitrary tag labels. Optional diagnostics exports have only enabled destinations.

Real Studio gate: for DPO and ORPO on both parameterizations, record memory envelope/peak and frozen reference behavior, mirrored checkpoint integrity, exact serving, chained start, cancel ≤5 seconds, process restart, no unauthorized eviction, no swap growth. Qualify shared-chat profiles using ≥100 baseline and ≥100 mixed requests per loaded resident, ≤4k input tokens, zero errors, TTFT ≤1.5 s p95 and gateway ≤20 ms p95; include objective probe/checkpoint/validation phases. Do not infer coexistence from a different runtime/target/objective or the earlier node collection-budget test.

Rollback rehearsal: disable new preference admissions and privacy-producing controls; drain pending comparisons/exports and v3 jobs/measurements/evaluation pauses, finish withdrawal cleanup or retain its upgraded owner; restore prior binaries without delivering v3 work to old nodes. Historical additive schema stays readable by supported upgraded tools; destructive downgrade requires explicit retirement/purge of incompatible new records and is tested on isolated seeded DB only. Record restored runtime identities and health. No real-Studio CI targeting or user-data deletion is part of this rehearsal.

Release only when all mandatory tasks/gates have evidence and no required tests fail or skip. Summarize enabled support profiles and remaining admissions-off state precisely; the planning documents are not deployment approval or test results.
