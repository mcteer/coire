# Image generation delivery map

The parent specification is the acceptance contract. Each child is a separately reviewed
feature branch from `main`, with its own spec, plan, tasks, tests, and PR. Rebase a child
onto current `main` after preceding children merge. Keep each PR near the 800-line review
limit (generated lockfiles and API types excluded); split a child again if it exceeds it.
No image admission is enabled until authorization, persistence, publication, and cancellation
paths are complete. Parent tasks are checked when their child implementation is validated.
Branch and merge status remains separate from task completion.

| Child | Scope | Parent tasks |
| --- | --- | --- |
| `015a-image-runtime` | Frozen, native Studio mflux installation | T002–T003 |
| `015b-image-contracts` | Request, capability, input and recipe contracts | T004, T010; part of T006 |
| `015b2-image-projections` | Job, event, preset, grant and compatible API projections | remainder of T006 |
| `015b3-image-registry-contracts` | Registry kinds, backend isolation and generated clients | part of T005, T007 |
| `015b4-image-worker-contracts` | Node/worker/transfer command shapes | remainder of T005; part of T007 |
| `015b5-image-asset-contracts` | Auxiliary acquisition, isolated parser and console shapes | remainder of T007 |
| `015b7-image-profile-dependencies` | Required hidden base dependencies in capability contract and live policy | part of T006, T015, T019, T027 |
| `015b6-image-settings` | Settings, errors and deployment defaults | T011 |
| `015c1-image-job-schema` | Durable job, event and preset tables | part of T008–T009 |
| `015c2-image-asset-schema` | Durable inputs, outputs, grants and transfer receipts | part of T008–T009 |
| `015c3-image-capacity-schema` | Quotas, execution leases and coexistence profiles | remainder of T008–T009 |
| `015c4-image-migration-verification` | Disposable PostgreSQL upgrade/downgrade and constraint checks | part of T008; verifies T009 |
| `015d1-image-identity-guard` | Human/personal-key preflight and live rechecks | part of T014, T017–T018 |
| `015d2-image-route-guard` | Audited route dependency and owner-only ordinary reads | part of T014, T017–T018 |
| `015d3-image-preset-resolution` | Immutable preset override and dependency-policy resolution | part of T015, T019 |
| `015d4-image-preset-store` | Live revision/dependency lookup and entitlement recheck | part of T015, T017, T019 |
| `015d5-image-preset-admin` | Append-only admin preset mutation and audit service | part of T015, T019–T020 |
| `015d6-image-preset-routes` | Human-admin create/update/retire routes with live role and refusal audits | part of T014, T020 |
| `015d7-image-preset-list` | Live entitlement-filtered picker listing, disabled until image admission | completes T015, T019; part of T022 |
| `015d8-image-preset-templates` | Inert admin-bound template examples and import instructions | completes T020 |
| `015d9-image-output-access` | Live explicit output-download recheck and conservative shared-view predicate | part of T014, T017, T022 |
| `015e1-image-routing-isolation` | Exclude image backends from language/vision routing and legacy acquisition | part of T027–T028 |
| `015e2-image-registry-kind` | Persist asset kind and kind/backend DB constraint with guarded rollback | part of T027–T028 |
| `015e3-image-profile-schema` | Persist measured base capability with ready-state and downgrade constraints | part of T027 |
| `015e4-image-asset-file-policy` | Studio metadata preflight and exact safe-file snapshot helper | part of T027 |
| `015e5-image-routing-regression` | MCP kind/backend fix and signed-snapshot isolation regressions | completes T028 |
| `015e6-image-classifier` | Supervised local Studio CPU tagger and strict provenance result | T016; part of T021 |
| `015e8-image-txt2img-pipeline` | Fixed offline Turbo txt2img with synchronized progress and exact output validation | part of T029 |
| `015e9-image-metadata` | Canonical private PNG recipe, pixel and file digests, bounded writer | T031 |
| `015e10-image-worker-job` | Fenced in-process Studio job execution and private scratch | part of T029/T032/T039 |
| `015e11-image-worker-control` | Authenticated resident worker control and path-free output status | part of T024/T029/T030/T032 |
| `015e12-image-worker-bootstrap` | Strict 0600 launch config and offline native child entrypoint | part of T024/T029/T030/T033 |
| `015e13-image-process-launch` | Reserved node-owned process launch and durable PID/port identity | part of T024/T030/T033 |
| `015e14-image-process-readiness` | Authenticated health, durable ready state and exact process re-adoption | part of T024/T030/T033 |
| `015e15-image-process-stop` | Fenced TERM/KILL and reservation release after confirmed death | part of T024/T030/T039/T041 |
| `015e16-image-node-routes` | Authenticated node control routes, restart adoption and shared image/language memory budget | part of T024/T030/T033 |
| `015g1-image-gallery` | Owner-only metadata list/detail with stable cursor and generated clients | part of T022, T023, T043–T044 |
| `015d-image-security` | Route audit, owner filtering, revocation cancellation and presets | remainder of T014–T020, T022 |
| `015e-image-worker` | Engine, classifier, journal and asset validation | T016, T021, T024, T027–T033 |
| `015f1-image-storage-config` | Disabled API-only blob volume, settings and ingress bounds | part of T012 |
| `015f2-image-telemetry` | Content-free API/node metrics, spans, dashboard and alerts | T013 |
| `015i1-image-recipe-parser` | Isolated 64 MiB PNG recipe parser with bounded metadata and no pixel decode | part of T012, T050, T053 |
| `015f-image-storage` | Bounded blob staging, grants and cleanup | remainder of T012, T026, T031, T036, T038–T039 |
| `015g-image-jobs` | DBOS admission, events, cancellation and API | T023, T025, T034–T035, T037, T040–T042 |
| `015h-image-ui` | Generated types, native UI and gallery | T043–T048 |
| `015i-image-inputs` | Isolated parsing, owner inputs and reproduction | T049–T058 |
| `015j-image-cache` | Bounded stage reuse | T059–T064 |
| `015k-image-coexistence` | Atomic admission and measured chat isolation | T065–T072 |
| `015l-image-acceptance` | Dashboards, real engine and cluster evidence | T073–T085 |

The first child is [draft PR #31](https://github.com/mcteer/coire/pull/31), based on `main`.
Its installer tests and local smoke passed; real Studio upgrade/rollback evidence is pending.
The request/recipe contracts are [draft PR #32](https://github.com/mcteer/coire/pull/32),
also based on `main`. The remaining projections are
[draft PR #33](https://github.com/mcteer/coire/pull/33), reviewed against PR #32 until it merges.
Registry kind isolation is [draft PR #34](https://github.com/mcteer/coire/pull/34),
reviewed against PR #33.
Fenced node/worker commands are [draft PR #35](https://github.com/mcteer/coire/pull/35),
reviewed against PR #34. Asset, parser and console shapes are
[draft PR #36](https://github.com/mcteer/coire/pull/36), reviewed against PR #35.
Conservative settings and safe errors are [draft PR #37](https://github.com/mcteer/coire/pull/37),
reviewed against PR #36. The first persistence slice is
[draft PR #38](https://github.com/mcteer/coire/pull/38), reviewed against PR #37.
T008–T009 remain open until all three persistence slices and their migration checks finish.
The asset/transfer/grant schema is [draft PR #39](https://github.com/mcteer/coire/pull/39),
reviewed against PR #38. Capacity, lease and coexistence persistence is
[draft PR #40](https://github.com/mcteer/coire/pull/40), reviewed against PR #39.
The three migrations are drafted and verified on disposable local PostgreSQL 17 in
[draft PR #41](https://github.com/mcteer/coire/pull/41), reviewed against PR #40.
T009 is complete. T008 remains open for cancellation/publication race evidence and
operator-run drain/rollout checks; no Studio or production database was contacted.
The disabled storage topology is [draft PR #42](https://github.com/mcteer/coire/pull/42),
reviewed against PR #41. T012 stays open until the API enforces purpose-specific file limits.
The inherited PyJWT 2.13.0 critical scan finding is fixed separately in
[draft PR #44](https://github.com/mcteer/coire/pull/44) from issue #43; it must land before
the image PR stack can pass the unchanged image scan gate.
The bounded telemetry seam is [draft PR #45](https://github.com/mcteer/coire/pull/45),
reviewed against PR #42. T013 is complete; later services must call its helpers.
The user-bound authorization guard is [draft PR #46](https://github.com/mcteer/coire/pull/46),
reviewed against PR #45. The audited route dependency and owner lookups are
[draft PR #47](https://github.com/mcteer/coire/pull/47), reviewed against PR #46.
Preset override and frozen dependency-policy resolution are
[draft PR #48](https://github.com/mcteer/coire/pull/48), reviewed against PR #47.
The parent story tasks remain open for production routes, admission and completion audits,
revocation cancellation, preset persistence/admin routes and full entitlement integration.
Routing isolation is [draft PR #49](https://github.com/mcteer/coire/pull/49), reviewed
against PR #48. Asset-kind persistence is [draft PR #50](https://github.com/mcteer/coire/pull/50),
reviewed against PR #49 and verified on disposable local PostgreSQL 17. The measured
base profile schema is [draft PR #51](https://github.com/mcteer/coire/pull/51),
reviewed against PR #50 and verified on disposable local PostgreSQL 17. The dedicated
admin image acquisition and inspection path remains required before T027–T028 can be
marked complete.
Live preset revision and dependency lookup is [draft PR #52](https://github.com/mcteer/coire/pull/52),
reviewed against PR #51. Admin preset mutation/list routes and admission integration
remain required before T015, T017 and T019 can be marked complete.
The additive required-dependency contract and policy check are
[draft PR #53](https://github.com/mcteer/coire/pull/53), reviewed against PR #52.
Audited append-only preset mutations are [draft PR #54](https://github.com/mcteer/coire/pull/54),
reviewed against PR #53. Human-admin routes are [draft PR #55](https://github.com/mcteer/coire/pull/55),
reviewed against PR #54. Admin-imported templates remain required.
The eligible picker is [draft PR #56](https://github.com/mcteer/coire/pull/56), reviewed
against PR #55. T015 and T019 are complete across the preset slices. T022 remains open
for output-access/tag filtering.
The inert admin-imported templates are [draft PR #57](https://github.com/mcteer/coire/pull/57),
reviewed against PR #56. T020 is complete across #54, #55 and #57.
The reusable output access guard is [draft PR #58](https://github.com/mcteer/coire/pull/58),
reviewed against PR #57. T022 remains open until gallery and grant/content routes use it.
Image asset file preflight is [draft PR #59](https://github.com/mcteer/coire/pull/59),
reviewed against PR #58. T027 remains open for licence checks, component closure,
dedicated admin acquisition, local validation and publication.
The isolated recipe-only PNG parser is [draft PR #60](https://github.com/mcteer/coire/pull/60),
reviewed against PR #59. Upload routes, owner staging and quota enforcement remain open, so
T012, T050 and T053 are not yet complete.
MCP routing isolation is corrected in [draft PR #61](https://github.com/mcteer/coire/pull/61),
reviewed against PR #60. The combined chat, listing, direct resolver, MCP and failover paths
complete T028; an image asset with a stray `coding` tag cannot enter a coding run.
The local Studio classifier is [draft PR #62](https://github.com/mcteer/coire/pull/62),
reviewed against PR #61. T016 is complete. T021 stays open until the generation worker
uses the stage with a measured reservation and the pinned model is tested on a Studio.
Private gallery metadata is [draft PR #63](https://github.com/mcteer/coire/pull/63),
reviewed against PR #62. T023 and UI tasks remain open for the other routes and flows.
Subject-bound output grants and authenticated content are
[draft PR #64](https://github.com/mcteer/coire/pull/64), reviewed against PR #63.
T022 remains open for completion audits, runtime prompt preservation and full policy
boundary tests. The grant token moved from the URL query to a fragment plus request
header after tracing instrumentation was found to capture query strings; ADR 0009
records the decision.
Bounded private image input staging is
[draft PR #65](https://github.com/mcteer/coire/pull/65), reviewed against PR #64.
T012, T049 and T054 remain open for authenticated upload admission, quota/processing,
real-size boundary tests and cleanup before the route can be enabled.
The private recipe parser handoff is
[draft PR #66](https://github.com/mcteer/coire/pull/66), reviewed against PR #65.
T053 remains open for generation-input normalization and T054 for owner upload,
processing and cleanup. The worker accepts generated IDs and checks the recipe PNG
against its committed size/hash without decoding pixels.
The typed private parser client is
[draft PR #67](https://github.com/mcteer/coire/pull/67), reviewed against PR #66.
It refuses mismatched worker identity, size or digest and keeps response content out
of errors. T054 stays open until a durable owner-upload workflow calls this client.
Owner output tombstones and API-side physical purge are
[draft PR #68](https://github.com/mcteer/coire/pull/68), reviewed against PR #67.
T038 and T039 remain open for generation publication, Studio cleanup acknowledgments,
input purging and end-to-end retention evidence. New reads fail after tombstone;
stored-byte counters release only after a safe unlink or confirmed absence.
Atomic owner/global storage holds are
[draft PR #69](https://github.com/mcteer/coire/pull/69), reviewed against PR #68.
T008/T026/T035/T054 remain open for cross-process PostgreSQL contention evidence and
calling the ledger from upload/job admission, settlement and cancellation. The local
environment did not have `COIRE_TEST_POSTGRES_DSN` configured.
Owner recipe-only upload admission and durable parser recovery are
[draft PR #70](https://github.com/mcteer/coire/pull/70), reviewed against PR #69.
T012/T049/T050/T054 remain open for generation-input normalization, failed-input and
orphan cleanup, full owner lifecycle and cross-process quota evidence. The route stays
behind disabled-by-default `COIRE_IMAGE_ENABLED`; uncertain commits retain bytes for
reconciliation rather than risking a committed row pointing to missing data.
Failed and orphaned input cleanup is
[draft PR #71](https://github.com/mcteer/coire/pull/71), reviewed against PR #70.
T039/T054 remain open for ready-input deletion, normalization and end-to-end
retention evidence at this slice; T073/T074 remain open for the remaining generation metrics and
alerts. Failed-input holds release only after unlink, and uncertain-commit files are
removed after a quota-lock-protected row absence check and one-hour grace.
Owner input tombstones and safe purge are
[draft PR #72](https://github.com/mcteer/coire/pull/72), reviewed against PR #71.
T039/T054 remain open for generation-job reference cancellation, normalized inputs,
and end-to-end retention evidence. Active references return 409 until job cancellation
can drain them; held or stored input bytes release only after physical deletion.
Measured defaults and strict basic txt2img request resolution are
[draft PR #73](https://github.com/mcteer/coire/pull/73), reviewed against PR #72.
T035 remains open for live registry/entitlement checks, idempotent transaction admission,
quota, audit and durable queue receipts. Profiles without measured defaults are readable
but cannot admit a job.
Queue, daily-output and worst-case disk reservation helpers are
[draft PR #74](https://github.com/mcteer/coire/pull/74), reviewed against PR #73.
T026/T035/T041 remain open until durable job admission and state transitions call the
helpers under a fenced job row and publication/cancellation reconciles retained bytes.
The queued-job settings snapshot is
[draft PR #75](https://github.com/mcteer/coire/pull/75), reviewed against PR #74.
It carries effective settings before Studio selection and binds runtime facts once;
T023/T035/T037 remain open until native job routes and durable admission use it.
Internal replay-safe queued admission is
[draft PR #76](https://github.com/mcteer/coire/pull/76), reviewed against PR #75.
It commits a canonical intent, quota hold, job, first event and audit atomically for
basic txt2img, but T023/T035/T037 remain open for a public route, denial audit,
cross-process PostgreSQL contention and dispatch/recovery. Default image admission
stays disabled until worker, publication and cancellation gates complete.
Studio image-copy preflight is
[draft PR #77](https://github.com/mcteer/coire/pull/77), reviewed against PR #76.
It binds a registry slug to an exact local manifest and rejects unsafe files before
load. T024/T029/T030/T033 remain open for the resident worker, admin acquisition,
node routes and real runtime evidence. Draft PR #31 supplies the pinned native mflux
runtime and must land before live worker execution.
The fixed local Turbo txt2img pipeline is
[draft PR #78](https://github.com/mcteer/coire/pull/78), reviewed against PR #77.
It rejects unsupported settings before generation and synchronizes each progress
callback. T029 remains open until process supervision, output metadata and real
Studio execution are integrated; public admission stays closed.
Canonical private PNG serialization is
[draft PR #79](https://github.com/mcteer/coire/pull/79), reviewed against PR #78.
T031 is complete: the bounded writer embeds the exact recipe, strips upstream
metadata, returns file and pixel digests and cleans incomplete scratch files.
Transfer, publication and operator-run verification remain open.
Fenced in-process image attempts are
[draft PR #80](https://github.com/mcteer/coire/pull/80), reviewed against PR #79.
The executor binds the resident model and attempt, writes canonical outputs in
private scratch and cleans failed attempts. T029/T032/T039 remain open for
supervision, durable journal, transfer and cleanup acknowledgments.
Authenticated resident worker control is
[draft PR #81](https://github.com/mcteer/coire/pull/81), reviewed against PR #80.
Its loopback app accepts typed fenced run/status/cancel commands and reports
path-free manifests; T024/T029/T030/T032 remain open for node process launch,
durable journal, hard cancellation and end-to-end tests.
The offline native worker bootstrap is
[draft PR #82](https://github.com/mcteer/coire/pull/82), reviewed against PR #81.
It verifies owner-only launch files and sets offline mode before mflux import;
T024/T029/T030/T033 remain open for node-owned launch, PID/reservation
persistence, readiness, re-adoption and real Studio evidence.
Node-owned image process launch is
[draft PR #83](https://github.com/mcteer/coire/pull/83), reviewed against PR #82.
It verifies the local copy, reserves the configured memory and loopback port,
and persists PID/create-time identity before returning `starting`. T024/T030/T033
remain open for readiness, re-adoption, stop/kill, route integration and live
Studio validation.
Authenticated readiness and exact process re-adoption are
[draft PR #84](https://github.com/mcteer/coire/pull/84), reviewed against PR #83.
Unknown or corrupt records keep a conservative memory hold; T024/T030/T033
remain open for stop/kill, node routes, scheduler use and real Studio proof.
Fenced process TERM/KILL and cleanup are
[draft PR #85](https://github.com/mcteer/coire/pull/85), reviewed against PR #84.
It holds memory on uncertain inspection or cleanup and leaves generated scratch
untouched. T024/T030/T039/T041 remain open for node routes, job-level
cancellation arbitration, receipt-aware cleanup and live timing evidence.
Authenticated Studio worker routes and shared memory admission are
[draft PR #86](https://github.com/mcteer/coire/pull/86), reviewed against PR #85.
ADR 0010 records the dedicated route choice. T024/T030/T032/T033 remain open
for journaled job commands, transfer, acquisition validation and live Studio
acceptance; the public admission flag remains disabled.

Some parent tasks cross child boundaries, as shown by repeated IDs. Their checklist marker
changes only when all referenced work is complete. The final acceptance child reconciles
every parent task and records operator-run real-cluster evidence without committing images.
