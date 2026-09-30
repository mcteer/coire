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
| `015e1-image-routing-isolation` | Exclude image backends from language/vision routing and legacy acquisition | part of T027–T028 |
| `015e2-image-registry-kind` | Persist asset kind and kind/backend DB constraint with guarded rollback | part of T027–T028 |
| `015e3-image-profile-schema` | Persist measured base capability with ready-state and downgrade constraints | part of T027 |
| `015d-image-security` | Route audit, owner filtering, revocation cancellation and presets | remainder of T014–T020, T022 |
| `015e-image-worker` | Engine, classifier, journal and asset validation | T016, T021, T024, T027–T033 |
| `015f1-image-storage-config` | Disabled API-only blob volume, settings and ingress bounds | part of T012 |
| `015f2-image-telemetry` | Content-free API/node metrics, spans, dashboard and alerts | T013 |
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
against PR #55. T015 and T019 are complete across the preset slices. T020 remains open for
admin-imported templates; T022 remains open for output-access/tag filtering.

Some parent tasks cross child boundaries, as shown by repeated IDs. Their checklist marker
changes only when all referenced work is complete. The final acceptance child reconciles
every parent task and records operator-run real-cluster evidence without committing images.
