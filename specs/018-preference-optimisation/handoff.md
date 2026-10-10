# Implementation Handoff — Feature 018

**Branch**: `feat/018-preference-optimisation`

**Baseline**: `d2e4bbf` (PR 94 merged; features 016/017 available)

**Current scope**: Application/native implementation is in draft [PR 95](https://github.com/mcteer/coire/pull/95). T074 is complete; T076 and T077 remain unchecked. Continue the authorized implementation; a failed acceptance gate withholds release and does not itself end investigation.

## Start here

Read [spec.md](spec.md), [plan.md](plan.md), [research.md](research.md), [data-model.md](data-model.md), [feedback contracts](contracts/feedback-api.md), [training contracts](contracts/preference-training.md), [tasks.md](tasks.md), then [quickstart.md](quickstart.md). Work on this branch. Preserve recorded evidence rather than repeating completed jobs. The ignored `.specify/feature.json` selects 018.

Requirements review passes 16/16. Current ownership-snapshot code passes **3,122 CPU tests**, **12 affected real-Postgres cases**, all **16 ARM64 image build/policy/zero-CRITICAL/SPDX gates**, Ruff/format, strict mypy (1,021 files), OpenAPI freshness and pins. Unchanged web has 228 passing tests, TypeScript and lint; alert gates pass. Full hosted integration previously passed 516 cases / 40 existing optional skips at f3cacd1; current hosted checks remain pending. Required native numerical/runtime matrices have no skips. Eight exclusive objective × parameterization × initialization cases, live recovery, privacy, training/image exclusion and drained rollback pass. See the [execution record](execution-record.md) for scope and historical failures.

Four exact sustained shared profiles are accepted: B ORPO QLoRA V44 (one-second arrival), A DPO QLoRA V47 and B dense DPO V48 (two-second arrival), and B dense ORPO V70 (one-second arrival). Each has complete 900-second baseline/mixed phases, native coverage, zero failures/swap growth, latency evidence and positive stopped proof. V70 completed 900 successes per phase and 5,632 updates; exact gateway overhead p95 is 17.443310004/17.997938034 ms, below the unchanged 20-ms limit. Historical failed attempts remain unqualified. No concurrent shared profiles or Studio A dense ORPO support are claimed.

The scheduler uses a session advisory lock on a dedicated physical connection and immediately commits acquisition, retaining cross-worker ownership while releasing the MVCC snapshot. The connection is physically discarded on every exit. Real-Postgres tests prove fencing, snapshot release and cleanup on success/error/cancellation. V70 observations verify an idle owner with no open transaction or retained snapshot during both baseline and training.

The native CI resource probes need an isolated Apple Silicon runner with adequate memory. Hosted run 38037775120 passed the separately acquired AMD Llama 135M Q4/fp16 fixtures and the unchanged full Qwen matrix, but its small-resource children stopped before training when the hosted Mac's swap-out counter grew by 16–32 KiB. All four small probes pass locally with real 2.5-GiB admission/peak bounds and unchanged guards. The personal repository has no registered self-hosted runners; eligible larger-runner access or a dedicated non-production Mac is pending user information. Do not target the production Studios from CI or weaken zero-swap/thermal/memory checks.

Both Studios are healthy on immutable node environment `0.2.0-77c83e29e179`, with previous builds retained. The node changes isolate online Hub-pull flags to authenticated acquisition children and apply effective template overrides before MLX constructs its wrapper; native loss/trainer/runtime dependencies are unchanged. Original administrator and privacy owner remain active; capture is disabled at generation 8 and published dataset bytes/SHA are preserved. Qualification uses only owned actors/keys/residents and restores disabled training flags and positive drains on exit. Fresh final reconciliation verifies all four reports/profiles, all 80 owned measurements terminal and all 80 memory/storage holds released. Owned actors are inactive with all keys revoked; training flags are false and both Studios have no remaining engines or workers.

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
- Preference admission defaults off. Native tests, eight exclusive training/serving combinations, live recovery, privacy and drained rollback have passed as recorded. The four exact sustained shared profiles are qualified; required native resource CI remains pending.

## Workflow notes

Existing 018 spec was refreshed in place rather than creating a new feature. The workspace began clean on the already merged issue-93 fix; the feature branch was created from current origin/main. The actual scheduler package is `apps/coire-api/src/coire_scheduler`.

No `.specify/extensions.yml` exists, so before/after hooks for all five stages are absent and skipped. Spec, plan and task templates use the repository's resolution scripts. `check-prerequisites --paths-only`, `setup-plan` and `setup-tasks` resolved 018 successfully. The plan skill explicitly requested research agents; two performed read-only runtime and chat/dataset investigations. No implementation delegation occurred.

Document checks cover sequential task IDs/labels/paths, 40/40 requirement mappings, internal links, fenced blocks and whitespace. Planning document checks alone imply no runtime acceptance. The execution record now records automated and real-Studio implementation evidence, including unsuccessful performance attempts.
