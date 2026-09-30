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
| `015b6-image-settings` | Settings, errors and deployment defaults | T011 |
| `015c1-image-job-schema` | Durable job, event and preset tables | part of T008–T009 |
| `015c2-image-asset-schema` | Durable inputs, outputs, grants and transfer receipts | part of T008–T009 |
| `015c3-image-capacity-schema` | Quotas, execution leases and coexistence profiles | remainder of T008–T009 |
| `015c4-image-migration-verification` | Disposable PostgreSQL upgrade/downgrade and constraint checks | part of T008; verifies T009 |
| `015d-image-security` | Identity, entitlement, audit and presets | T014–T020, T022 |
| `015e-image-worker` | Engine, classifier, journal and asset validation | T016, T021, T024, T027–T033 |
| `015f1-image-storage-config` | Disabled API-only blob volume, settings and ingress bounds | part of T012 |
| `015f-image-storage` | Bounded blob staging, grants and cleanup | remainder of T012, T013, T026, T031, T036, T038–T039 |
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

Some parent tasks cross child boundaries, as shown by repeated IDs. Their checklist marker
changes only when all referenced work is complete. The final acceptance child reconciles
every parent task and records operator-run real-cluster evidence without committing images.
