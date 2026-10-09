# Feature 017 Handoff

**Date**: 2026-10-08. **Branch**: `feat/017-evaluation-verbs`.
**Base**: `ec8679c1a3ede4c7b4d897ab4c27b0833a517ecc`, feature 016 squash merge from PR #91.
**Current scope**: implementation is authorized by the user's `$speckit-implement` instruction. Implementation and all 70 task acceptance checks are complete. Review description is prepared; admissions remain disabled. Node collection-budget CPU/RSS limitation remains explicit, so no passing budget or broad production enablement is claimed.

## Start here

Read repository `AGENTS.md`, `.specify/memory/constitution.md`, `docs/ARCHITECTURE.md`, `docs/ROADMAP.md`, then [spec.md](spec.md), [plan.md](plan.md), [tasks.md](tasks.md) and [quickstart.md](quickstart.md). Supporting artifacts: [research.md](research.md), [data-model.md](data-model.md), [admin API](contracts/evaluation-api.md), [training/workers](contracts/training-and-workers.md), [CLI/console](contracts/cli-and-console.md), [requirements checklist](checklists/requirements.md).

The local ignored `.specify/feature.json` selects `specs/017-evaluation-verbs`. The branch is based directly on merged main and has no upstream. Planning and implementation changes are local and uncommitted, including core contracts, the additive 0032 migration, Studio worker/workspace foundations, scheduler admission and recovery, and generated API types. Preserve the entire working tree when switching agents. No 017 PR has been opened; no dependency was added.

## Decisions that must survive handoff

- User clarified that automatic task/judge evaluation runs **only when declared in the recipe**. Existing held-out loss and 016 recipes keep their behavior.
- Preserve v1 normalized/resolved/checkpoint hashes exactly. New suite schedules use separate training v2 types; do not add serialized empty defaults to v1 or rehash history.
- The former local harness CLI and loose score POST have been replaced/refused. Execution uses existing Studio sandboxes; new verification changes require platform execution evidence. Preserve legacy read history and positional CLI syntax; reject loose score POST with a documented 409 transition.
- Core only coordinates/persists. All suite work runs through node-owned agent containers, exact short-lived READ grants, fixed fixtures and no generated-code execution. Private/unverified evaluation must never bypass ordinary user WRITE verification.
- Task/judge scores are informational. A failed evaluation cannot undo successful training. Full queue/disabled admission leaves durable automatic obligations and explicit failures, not silent dropped work.
- Serialize checkpoint evaluation with training at an acknowledged full mirrored checkpoint, with explicit evaluation pause ownership, both-rank stop proof, bounded retention pins and current authorization/admission before resume. A newer operator/protective decision wins.
- Exact judge/base identity rules reject aliases, variants and adapters of the judge's base. Judge is explicitly bound in the suite; no fallback. Comparability uses immutable inputs/settings/runtime, not only suite version.
- Authored bounded local suites, immutable results, seven-day bounded private raw evidence, no new service or dependency. Real Studio acceptance plus tiny-model CI, baseline alerts and rollback remain release requirements.

## Current implementation and remaining work

70 of 70 tasks are checked, including retrieved latest container qualification, actual stale-profile/live guard and safe drained Core rollback. No feature-017 commit, upstream or PR exists. Preserve the whole working tree; do not rebase or rewrite reviewed feature-016 history.

Latest local qualification: 2,832 CPU tests passed, two existing unrelated skips; strict mypy passed across 920 files; Ruff check and formatting passed. Web qualification: 200 tests, TypeScript, ESLint and generated types passed. All 18 required real tiny-engine/numerical checkpoint tests passed. Affected production images passed policy, critical scans including unfixed findings, secret scans and SPDX generation. Five Prometheus rule cases passed. No gates or limits were weakened.

Latest complete isolated guest gate was retrieved from Studio B's Lima/VZ `coire-017-qualification`: **461 passed, 40 existing opt-in skips, 2,875 deselected in 1,374.66 seconds**. Private log: `qualification-continuity-latest-retrieved.log`. The 456-test gate remains historical. Required tiny-engine/numerical cases are independently 18/18 passing with zero required skips. The guest workspace is `/opt/qualification/workspace`; Lima tools/state are under Studio B `/Users/mcteer/coire-stage/017-release/{lima-tools,lima-state}`. Never run the simulated fabric subnet on the Studio host.

## Actual Studio acceptance

Both Studios last authenticated healthy on native `/opt/coire/envs/0.2.0-8703291ef6ed`, worker `coire-agent@sha256:34d4123be9b331f9549cc56448a4624eef770308d391091214bcc79d122e3c3b`. Do not rerun older activation scripts. Core's latest qualified API/scheduler/MCP images were deployed; migration 0032 is applied. Private release `/Users/mcteer/.coire/projects/coire/releases/017-development-20261007` retains default-off `compose.json` and now admission-disabled `acceptance-runtime.json`, with diagnostics disabled. Preserve the feature-016 release, backup and selection link.

- Single-rank training `01M4EARWT6MSR747ZR0MPSSW3S` succeeded at 12 updates. Automatic final Task/rubric evaluations and stored-result comparisons succeeded; ready adapter `33efba4f-4277-56a0-a26a-c7665b90aeaf` remains informational/unverified.
- Two-rank job `01M4ECF8T5R13DN56HGY6QX6FS` succeeded at 12 updates. All six checkpoint-4/checkpoint-8/final Task/rubric evaluations succeeded and cleaned up. Both ranks proved stop at each boundary, exact full checkpoints resumed at 4 and 8, and a real scheduler restart recovered the durable group. Fifteen raw phase payloads passed strict binding/hash checks. Final comparisons returned HTTP 200. Private adapters retired and retention pins released.
- Newer-admin override job `01M4EEFCBZYZ2WYR1H1Q6VP0K2` preserved administrator pause at 4, accepted explicit resume, then preserved administrator cancellation at 8 with complete evaluation cleanup and no automatic resume.
- Actual reachable sandbox cancellation `01M4ED616FS6GGQQMQQR2EK828` proved authenticated child absence in 0.995311 seconds, token revocation and complete cleanup.
- Coexistence measurement `4917081c-7970-4d7e-a808-5172cfd7d2b7` succeeded and minted a profile: 100 baseline plus 300 actual mixed requests, zero failures/swap growth, thermal qualification passed. Baseline first-token p95 0.521838 seconds; worst mixed p95 0.452312 seconds; worst gateway p95 0.018455 seconds. Unchanged limits are 1.5 seconds and 20 milliseconds. Installed prompts were 37/32/37/37 tokens; do not claim 4,000-token prompts. Diagnostics remained disabled.

Private receipts are under `/private/tmp/coire-017-acceptance`; the execution record identifies the exact files. Earlier failed attempts remain truthful history, never qualification.

## Final live guard and rollback evidence

- Refreshed measurement `687d4af4-9167-48bc-b7f4-186c484473e0` succeeded: 100 baseline/300 mixed requests, zero failures/swap, thermal pass; baseline p95 0.192986 s, mixed p95 0.492036 s, gateway p95 18.430 ms. Unchanged limits passed. Private `guard-refresh-*` receipts.
- Original guard run `01M4EGYT1K23GDE1DB87D9QAC6` was inspected: failed `telemetry_stale`, null scores, complete cleanup before perturbation. Its used key/history remain preserved.
- Fresh ordinary run `01M4ET5SX6T9Y9183SZN7XSVF3` proved a RUNNING authenticated Studio A sandbox using qualified profile `c4d7b64a-127d-48ac-a9d3-4b915d757b7c`, with fresh real ordinary chat samples. Audited drain of exact resident `5668edd7-f161-4d3f-876e-ebc9041404bf` caused live stale-telemetry refusal, profile invalidation and complete cleanup in 9.376074 s. Replacement `53484432-25f3-407d-9114-02e977e5b430` reached READY, proved a different process/resident fingerprint with no qualified match, and was drained. Both residents are now STOPPED. Private `stale-profile-v5-*` receipts.
- Disabled evaluation/training admission; new evaluation returned 503. Durable drain counts all zero; authenticated native inventories have zero containers and only STOPPED historical engines. Rolled Core API/scheduler/MCP back to final qualified 016 binaries, all healthy with diagnostics off. Legacy v1 job `01M4BFX18RRCPVTTE3TA5PDK8G` response was byte-equivalent as parsed JSON. Digests for 38 results, 57 immutable training records and 147 checkpoint manifests were unchanged. Migration 0032 retained. Restored latest qualified 017 binaries, all healthy with all digests/legacy response unchanged and final drain complete. Private `rollback-v5-*` receipts/manifests.

## Current runtime and review state

017 private release `/Users/mcteer/.coire/projects/coire/releases/017-development-20261007/acceptance-runtime.json` now matches healthy restored API `ff33527ea81e445a…`, scheduler `055ba8654abbc204…`, MCP `bc8b84859bea2722…`. Evaluation/training admission and diagnostics are disabled. Default-off compose also has evaluation/diagnostics disabled. Studios remain on the previously qualified native and worker pins. The 016 release, backup and selection link are preserved. No migration downgrade or live model remains from these tests.

Final PR description: `/private/tmp/coire-017-acceptance/pr-body.md`, using the repository template and Principles I–VII. No 017 commit, upstream, push, PR or remote CI run exists. Working tree remains fully uncommitted on `feat/017-evaluation-verbs`; preserve it and do not rebase/rewrite history. Prepared real Harness/pairwise requests were not run and are not claimed as extra real coverage.

Node collection CPU/RSS observations exceed unchanged 2%/150-MB limits (roughly 4–5%/569–598 MB). This remains an operational limitation, not a passing budget qualification. Broader enablement must account for it. All scoped tasks and acceptance described above completed without gate changes.

## Access and credentials

Fresh unrestricted-session checks succeeded for Studio SSH, gateway, Docker, release-directory writes and Git metadata. The previous sandbox restriction is resolved. Existing login Keychain service `coire-017-development-key`, account `coire`, remains the authenticated human admin credential. Never print it or put it in argv/Git. Gateway `http://192.168.4.10:8180`; Studios `mcteer@192.168.4.11` and `.12`. Raw evidence/helpers remain `/private/tmp/coire-017-acceptance`.

## Publishing continuation

The user authorized commit, push, PR creation and merge when CI is green. All 017 implementation/spec/evidence changes are to be published from `feat/017-evaluation-verbs`; no main commit, rebase, force-push or branch-protection bypass is authorized. The preceding uncommitted/no-PR statements describe the pre-publication snapshot. Merge must wait for green CI on the published head, including image and isolated engine gates.

## Follow-up: node collection budget (2026-10-09 UTC)

Feature 017 was published and merged in [PR 92](https://github.com/mcteer/coire/pull/92), with all 70 tasks complete. Its recorded CPU/RSS overrun is historical. [Issue 93](https://github.com/mcteer/coire/issues/93) now has an independently qualified native fix: both Studios ran `/opt/coire/envs/0.2.0-a68cf8319860`, diagnostics disabled, with unchanged 2% / 150 MiB limits. All 108 polls / 53 distinct steady-state snapshots per node passed; maxima were 1.5% CPU and 144.671875 MiB (A) / 146.296875 MiB (B). All 106 generation requests and six real capability probes succeeded, followed by authenticated drain and at least 171 seconds of passing idle collection. Startup transients are not qualified. See [complete follow-up evidence](../../docs/runbooks/node-collection-budget-evidence.md); raw evidence stays outside Git. The previous pre-publication and limitation statements describe the original 017 snapshot, not this follow-up.
