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
| `015b3-image-worker-contracts` | Registry, worker and settings contracts | T005, T007, T011 |
| `015c-image-persistence` | Durable tables, constraints and migrations | T008–T009 |
| `015d-image-security` | Identity, entitlement, audit and presets | T014–T020, T022 |
| `015e-image-worker` | Engine, classifier, journal and asset validation | T016, T021, T024, T027–T033 |
| `015f-image-storage` | Bounded blob staging, grants and cleanup | T012–T013, T026, T031, T036, T038–T039 |
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

Some parent tasks cross child boundaries, as shown by repeated IDs. Their checklist marker
changes only when all referenced work is complete. The final acceptance child reconciles
every parent task and records operator-run real-cluster evidence without committing images.
