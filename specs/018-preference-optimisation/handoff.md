# Implementation Handoff — Feature 018

**Branch**: `feat/018-preference-optimisation`
**Baseline**: `d2e4bbf` (PR 94 merged; features 016/017 available)
**Current scope**: Implementation authorized with `$speckit-implement`; release is withheld at the failed T074 acceptance gate. See [execution-record.md](execution-record.md) for measured evidence and failed attempts.

## Start here

Read [spec.md](spec.md), [plan.md](plan.md), [research.md](research.md), [data-model.md](data-model.md), [feedback contracts](contracts/feedback-api.md), [training contracts](contracts/preference-training.md), [tasks.md](tasks.md), then [quickstart.md](quickstart.md). Continue on this branch; do not recreate the feature or repeat completed acceptance jobs. The local `.specify/feature.json` points to `specs/018-preference-optimisation`; it is ignored local state, not a committed artifact.

The implementation covers all 77 tasks; requirements review passes 16/16. Code, migration 0033, generated contracts, UI, observability, operational documentation and native runtime are implemented. Current release checks pass 3,114 CPU tests, static/OpenAPI checks and all 16 ARM64 image gates. The full integration checkpoint passed 517 tests; nine affected real-Postgres cases pass again after content-free timing instrumentation. Unchanged web checks pass 228 tests, TypeScript and lint. Eight exclusive native objective × parameterization × initialization cases and the recorded recovery/privacy/rollback gates pass.

**Release blocker: T074.** Three exact shared profiles are accepted: B ORPO QLoRA V44 (one-second arrival), A DPO QLoRA V47 and B dense DPO V48 (two-second arrival). Their full native coverage, conservative phase-specific overhead evidence and positive stops pass. Studio A dense ORPO V48/V49/V50/V52 failed the baseline gateway-overhead budget, including the four-second-arrival V52 retry. No profile is accepted from those attempts. WAL/CPU diagnostics locate admission costs; two disposable database-batching prototypes passed safety checks but showed no clear latency improvement and were not applied. See the execution record rather than repeating completed jobs or inferring support.

Both training admission flags are false in checked normal service images. Both owned acceptance actors are inactive and their keys revoked; all 69 owned measurements are terminal and their holds released. Both Studios have no remaining text-serving or preference-training processes. The original administrator and privacy owner remain active, with privacy capture disabled at generation 8. Published dataset bytes and SHA are preserved. Native runtime `0.2.0-e5953d0395eb` remains installed. Any future shared qualification needs fresh scoped test credentials and fresh measured evidence; the current draft is not release complete.

## Accepted clarification

The user selected: **preserve published datasets and adapters; purge unexported feedback**. Source contributions become inaccessible immediately on opt-out/deletion and copied content is purged within 24 hours. Publication is committed immutable dataset registration (possibly still analyzing), not eventual analysis readiness. The limitation must be disclosed before contribution. Re-enable does not revive old generations. This is an accepted answer, not an unresolved default.

## Decisions to preserve

- Both DPO and ORPO; first supported matrix is single-Studio dense LoRA and affine 4-bit/group-64 QLoRA on the existing approved architectures, zero dropout. No preference DoRA or distributed implementation in this feature.
- ORPO is reference-free. DPO freezes base plus initial adapter, or bare base when explicitly selected. Restore full policy state on resume without replacing it with initial tensors.
- Separate recipe/resolved/checkpoint version 3; old SFT v1/v2 hashes and behavior remain intact. Include actual measurement, native dispatch, node capabilities, acknowledgements and feature 017 v2-type checks in compatibility work.
- Use pinned MLX/mlx-lm loss/iterator/callback hooks with no trainer fork. The API adds exactly pinned uvloop 0.22.1 (MIT / Apache-2.0); see ADR-0014 and the release dependency record. Pair count is not token count; post-update metrics probes conserve RNG/sampler state.
- Explicit text-only comparisons, no passive training-data harvesting or thumb-to-pair inference. Native exact prompt/variant snapshots and active-answer selection require implementation; existing successful-turn retry does not supply them. Legacy thumbs can lack exact provenance, but legacy comparisons are refused.
- Owner privacy gates live in the shared foundation before any capture surface. Export rechecks generations/deletions/source versions under the documented lock order; durable workflows and receipts contain IDs/status only.
- Minimum deployable feedback increment is foundation + US1 + US3. US2 can be built with uploaded fixtures independently. Full feature completion requires US4 and all release gates as well.
- Preference admission defaults off. Native tests, eight exclusive training/serving combinations, live recovery, privacy and drained rollback have passed as recorded. Sustained shared-node performance remains under qualification; default-off is not acceptance evidence.

## Workflow notes

Existing 018 spec was refreshed in place rather than creating a new feature. The workspace began clean on the already merged issue-93 fix; the feature branch was created from current origin/main. The actual scheduler package is `apps/coire-api/src/coire_scheduler`.

No `.specify/extensions.yml` exists, so before/after hooks for all five stages are absent and skipped. Spec, plan and task templates use the repository's resolution scripts. `check-prerequisites --paths-only`, `setup-plan` and `setup-tasks` resolved 018 successfully. The plan skill explicitly requested research agents; two performed read-only runtime and chat/dataset investigations. No implementation delegation occurred.

Document checks cover sequential task IDs/labels/paths, 40/40 requirement mappings, internal links, fenced blocks and whitespace. Planning document checks alone imply no runtime acceptance. The execution record now records automated and real-Studio implementation evidence, including unsuccessful performance attempts.
