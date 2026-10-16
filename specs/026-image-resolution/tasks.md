# Tasks: Image resolution and aspect ratios

## Setup and foundation
- [x] T001 Add dimension/qualification contract regressions and shared models in packages/coire-core/src/coire_core/models/images.py and image_worker.py.
- [x] T002 Add deterministic padded-canvas/crop pipeline regressions and implementation in apps/coire-node/src/coire_node/image_runtime/pipeline.py and control paths.

## US1 / US3: measured expanded capability
- [x] T003 Add bounded square/landscape/portrait txt2img/img2img qualification in apps/coire-node/src/coire_node/image_validation.py with result evidence and memory tests.
- [x] T004 Add audited admin qualification service/route in apps/coire-api/src/coire_api/images/ and routes/admin_models.py; contract tests for auth, registry identity, duplicate and no acquisition.
- [x] T005 Extend registry/reconciler.py for durable both-copy qualification, preserve prior profile on failure and terminal reservation cleanup; restart/failure tests and telemetry/alert.
- [x] T006 Regenerate OpenAPI/TS, pass schema/API/node checks, build native/API candidates and qualify published Z-Image on both Studios; record sizes/peak memory/rollback.

## US2: resolution / ratio interface
- [x] T007 Add pure size/ratio computation and ImageForm controls with restore/custom/limits regression tests in apps/coire-web/src/.
- [x] T008 Add typed admin qualification control/status to existing model detail; preserve confirmations and live capability display.

## Acceptance and release
- [x] T009 Run owner authenticated 1980 square, portrait/landscape, metadata/reuse and cancellation qualification; retain evidence privately and clean outputs.
- [x] T010 Inspect actual controls in headless WebKit desktop/mobile/light/dark; run web tests/build/lint and image policy/scan/SBOM, activate independently and verify.
- [ ] T011 Update runbook/design/execution record, reconcile requirements, cleanup owned resources and open PR with constitution evidence.

Dependencies: T001 → T002 → T003 → T004/T005 → T006 → T009; T001 → T007/T008 → T010; all → T011.
Independent read-only test discovery can run concurrently; no sub-agent delegation is used.
MVP includes exact final dimensions and measured capability, then ratio UX and release gates.

## Expanded image workspace
- [x] T012 Inspect official Midjourney creation/viewer/selection screenshots and Firefly aspect guidance using headless WebKit; record references and design rationale.
- [x] T013 Implement typed owner history deletion, reversible migration, actual event removal, filtering, audit and contract tests.
- [x] T014 Replace oversized image sidebar with compact workbench and guided source/mask/reference workflows; preserve advanced settings and recipe behavior.
- [x] T015 Add accessible large image preview, compact overlay actions and multi-selection/bulk deletion with partial-failure handling and tests.
- [x] T016 Inspect all populated image workspace states in deployed headless WebKit desktop/mobile, keyboard/touch and both appearances; record evidence and clean owned test artifacts.
