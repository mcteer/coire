# Implementation Handoff — Feature 018

**Branch**: `feat/018-preference-optimisation`

**Baseline**: `d2e4bbf` (PR 94 merged; features 016/017 available)

**Current scope**: Implementation complete in [PR 95](https://github.com/mcteer/coire/pull/95). All 77 tasks are checked. Required source checks pass at 9dd06b2; the final follow-up reconciles documentation only. Review/merge is the next step.

## Start here

Read [spec.md](spec.md), [plan.md](plan.md), [research.md](research.md), [data-model.md](data-model.md), [feedback contracts](contracts/feedback-api.md), [training contracts](contracts/preference-training.md), [tasks.md](tasks.md), then [quickstart.md](quickstart.md). Work on this branch. Preserve recorded evidence rather than repeating completed jobs. The ignored `.specify/feature.json` selects 018.

Requirements review passes 16/16. Current ownership-snapshot code passes **3,122 CPU tests**, **12 affected real-Postgres cases**, all **16 ARM64 image build/policy/zero-CRITICAL/SPDX gates**, Ruff/format, strict mypy (1,021 files), OpenAPI freshness and pins. Unchanged web has 228 passing tests, TypeScript and lint; alert gates pass. Current hosted integration passes 519 cases / 40 existing optional skips; Linux CPU passes 3,120 / 77 platform/native/optional skips, and web passes 228. Native preference passes 64 required cases and native evaluation passes 18, zero skips. Required native numerical/runtime matrices have no skips. Eight exclusive objective × parameterization × initialization cases, live recovery, privacy, training/image exclusion and drained rollback pass. See the [execution record](execution-record.md) for scope and historical failures.

Four exact sustained shared profiles are accepted: B ORPO QLoRA V44 (one-second arrival), A DPO QLoRA V47 and B dense DPO V48 (two-second arrival), and B dense ORPO V70 (one-second arrival). Each has complete 900-second baseline/mixed phases, native coverage, zero failures/swap growth, latency evidence and positive stopped proof. V70 completed 900 successes per phase and 5,632 updates; exact gateway overhead p95 is 17.443310004/17.997938034 ms, below the unchanged 20-ms limit. Historical failed attempts remain unqualified. No concurrent shared profiles or Studio A dense ORPO support are claimed.

The scheduler uses a session advisory lock on a dedicated physical connection and immediately commits acquisition, retaining cross-worker ownership while releasing the MVCC snapshot. The connection is physically discarded on every exit. Real-Postgres tests prove fencing, snapshot release and cleanup on success/error/cancellation. V70 observations verify an idle owner with no open transaction or retained snapshot during both baseline and training.

All four required native resource CI probes pass in hosted run 38043072721 at production-source checkpoint 9dd06b2: DPO/ORPO × Q4/fp16, one required case per fresh VM and zero skips. Framework Python rewrote argv[0] and failed exact-command ownership; the isolated jobs now require uv-managed standalone Python. Cache preparation releases disk cache, while actual memory/swap/thermal admission remains unchanged. The standard hosted runner passes; the previously requested larger-runner prerequisite is resolved. Do not target production Studios from CI or weaken ownership/resource checks. The unchanged full Qwen matrix also passes all 60 Q4/dense-bf16 cases, zero skips. Hosted evaluation passes 18 cases; full integration passes 519 cases / 40 existing optional skips. All required source gates pass.

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
- Preference admission defaults off. Native tests, eight exclusive training/serving combinations, live recovery, privacy and drained rollback have passed as recorded. The four exact sustained shared profiles are qualified; all 64 required native preference cases pass, with zero skips.

## Workflow notes

Existing 018 spec was refreshed in place rather than creating a new feature. The workspace began clean on the already merged issue-93 fix; the feature branch was created from current origin/main. The actual scheduler package is `apps/coire-api/src/coire_scheduler`.

No `.specify/extensions.yml` exists, so before/after hooks for all five stages are absent and skipped. Spec, plan and task templates use the repository's resolution scripts. `check-prerequisites --paths-only`, `setup-plan` and `setup-tasks` resolved 018 successfully. The plan skill explicitly requested research agents; two performed read-only runtime and chat/dataset investigations. No implementation delegation occurred.

Document checks cover sequential task IDs/labels/paths, 40/40 requirement mappings, internal links, fenced blocks and whitespace. Planning document checks alone imply no runtime acceptance. The execution record now records automated and real-Studio implementation evidence, including unsuccessful performance attempts.
