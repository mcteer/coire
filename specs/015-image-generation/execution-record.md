# Feature 015 execution record

## Local implementation checks — 2026-09-30

The current writable checkout is `feat/015-image-generation`. Its working tree includes the
local consolidated implementation from `feat/015e20-image-node-cancel` plus the native job
observation work. The sandbox allows source edits but denies writes to `.git`, so these
working-tree changes are not committed or pushed from this session. No additional PR or CI
run was created.

- Native job observation adds owner-scoped job listing and SSE replay, strict `Last-Event-ID` parsing,
  reset snapshots after retention gaps, live access rechecks, terminal stream closure,
  and published output projections on job reads. Classifier-explicit outputs are hidden
  when current explicit access is absent.
- The native image model picker lists only published, ready Studio mflux bases whose required
  hidden dependencies and current caller entitlements are usable. Its public capability
  advertises only the implemented txt2img mode and worker-supported settings. The route remains
  empty while admission is disabled. Contract tests cover explicit personal-key scope, hidden
  dependency failure, and incompatible registry defaults.
- API and shared-contract suite: **1038 passed, 5 skipped** after the capability and
  admission changes.
- Focused image contract/unit tests: **32 passed** after the SSE OpenAPI contract; the broader
  API/shared run includes these checks.
- Web: **96 tests passed**, ESLint and TypeScript checks passed.
- A typed web API module covers available private model, job, preset and output reads. The shared
  SSE hook now supports typed image event decoding, duplicate/gap checks, terminal closure,
  and a permanent stop after 401/403/404 authorization failures.
- Ruff checks and mypy on API/core source passed; generated OpenAPI freshness passed.
- The private preset picker now uses `Cache-Control: private, no-store` in both disabled and
  enabled states, matching the other owner-sensitive image reads.
- T017 and T035 were reconciled against existing tested authorization and admission code and
  marked complete in `tasks.md`; native route/dispatch/cancellation tasks remain open.
- Owner cancellation now commits a queued terminal event or a fenced `cancelling` intent with
  audit and unchanged uncertain holds. DBOS retries the node stop, and terminalization requires
  the node's exact cleanup acknowledgment, core staging purge, matching execution lease and
  quota settlement. The node removes cancelled scratch and repairs older cancelled journals.
  API startup now has the missing bounded output maintenance worker; it also runs existing input
  sweeps while admission is disabled. Alerts and dashboard panels cover cancellation recovery
  and output purge failures.
- Latest API/shared run after cancellation changes: **1050 passed, 5 skipped**; two scheduler
  cancellation workflow tests passed after that run. Focused node cancellation/cleanup/journal
  tests: **19 passed**. Web: **96 passed**, ESLint and TypeScript pass. Ruff, mypy and OpenAPI
  freshness pass. Node supervisor tests that inspect processes or bind sockets still fail under
  this restricted sandbox; no gate was changed.
- Recipe import now restores saved direct settings from an owner-held ready recipe, validates
  replacement digest, dimensions and input purpose, reports missing dependencies and inputs,
  and keeps exact reproduction unavailable until runtime and hardware equivalence can be
  established. It cannot submit or acquire a model. The storage runbook describes the API.
- After this work, the full API/shared suite is **1060 passed, 5 skipped**; the focused node
  cancellation suite is **19 passed**; the web suite is **96 passed**. API/core mypy and Ruff,
  web TypeScript and ESLint, Grafana JSON parsing, and `git diff --check` pass. The `uv`
  launcher cannot access its cache in this sandbox, so these checks used the existing virtual
  environment binaries directly.
- A fenced DBOS observation workflow now polls only previously placed `reserving`/`running`
  jobs, checks the selected node, attempt, fence, instance and live execution lease, and
  persists monotonic start/progress observations at no more than four progress events per
  second. It transitions a transfer-ready journal into transfer recovery and never sends a
  generation start command. The observation alert, dashboard panel and runbook are updated.
  The full API/shared suite after observation is **1062 passed, 5 skipped**; Ruff, mypy,
  dashboard JSON and alert YAML parsing pass. A subsequent focused test proving scheduler
  recovery calls status without starting generation passes (three observation tests total).
  T034 remains open because placement/lease issuance and failed-attempt recovery are not built.
- The web dock now routes to an owner Images view with job history, a stop action and private
  output metadata pagination. It states that generation is unavailable while submission is
  gated; no generate control is exposed. Owner downloads now redeem a fresh short-lived grant
  in `X-Coire-Image-Grant`; the token never appears in the content fetch URL. The view offers
  reauthentication after a 401 and requires two clicks before output deletion. The full web
  suite is **102 passed** with TypeScript, ESLint and Prettier green. T044/T046/T047 remain
  open for the generation form, live progress timeline, thumbnails and complete gallery actions.
- Whole-batch publication now treats the durable private transfer keys as final blob keys only
  when one transaction verifies every receipt, PNG recipe, node cleanup acknowledgment, selected
  lease, live owner/key access and pinned registry manifest. It publishes all output rows, settles
  the byte hold and records one terminal event together. A revoked key, entitlement or registry
  manifest instead removes private staging and records an audited terminal failure after node
  cleanup is proved; an uncertain cleanup retains the hold. The Images dashboard, alert and
  storage runbook cover publication recovery. The full API/shared suite is **1071 passed,
  5 skipped** after these changes; Ruff, mypy and `git diff --check` pass. T038 remains open
  for classifier integration and Postgres/engine acceptance evidence.
- The terminal `done` SSE event now carries the complete typed published batch. OpenAPI and
  generated web API types were refreshed; the API/shared suite remained **1071 passed,
  5 skipped** after the contract change. Publication outcome metrics are now recorded only
  after the database transaction commits. Successful publication also writes an
  `image.complete` audit row with the originating identity and entitlement names in that
  transaction; an explicit-output case proves the audit excludes private content.
- A 30-minute overdue job that was never placed now expires through a locked DBOS recovery
  step. It releases its pending slot, output allowance and byte hold with a `queue_timeout`
  terminal event and content-free audit row. Any fence, node, instance, reservation or
  cancellation intent retains its holds for reconciliation. The image dashboard, alert and
  storage runbook include this path. The API/shared suite after completion audit is
  **1079 passed, 5 skipped**;
  focused expiry/publication tests are **15 passed** after adding an active-lease fence. Ruff format/check, mypy, alert YAML,
  dashboard JSON and `git diff --check` pass. T034 remains open for placement, lease issuance
  and failed-attempt recovery.
- Admission now requires a published image base for administrators as well as ordinary
  users, matching the publication gate. Previously an admin-only base could be queued but
  could never publish. A regression test covers this; the API/shared suite remains
  **1079 passed, 5 skipped** after the correction.
- API maintenance now visits 25 transfer job directories per pass with a cursor and
  removes only private `.uploading` files older than one hour. Durable staged/published
  PNGs stay under job recovery control. Tests cover the age boundary, symlink refusal,
  pagination and maintenance with image admission disabled; the dashboard and alert
  track failures. The complete API/shared suite is **1082 passed, 5 skipped** after this
  change. The web suite is **102 passed**, TypeScript and ESLint pass, and OpenAPI
  freshness passes.
- `docs/ARCHITECTURE.md` and ADR-0011 now describe the approved bare Studio worker,
  fenced core publication, bounded recipe import, exact-pixel scope, local CPU tagging
  and measured chat coexistence. The older optional image wrapper and URL-grant prose
  was removed. T079 is complete as a design-document task; the corresponding runtime
  and hardware gates remain open.
- Full repository suite under the restricted sandbox: **1469 passed, 143 skipped,
  18 failed, 32 errors**. Representative failures show denied host process inspection
  (`psutil`/`sysctl`), denied loopback binds, and denied Git operations in test checkouts.
  These are environment limitations, so this run is not a green full-suite gate. No
  test or check was disabled to obtain a pass.

Remaining feature work includes dispatch, publication, cancellation arbitration,
advanced modes, UI, cache/coexistence, and operator-run Studio evidence. Keep admission
closed until the required safety and recovery paths are complete. Run the full suite and
hardware gates in an unrestricted development environment before final acceptance.

## Dispatch and submit — 2026-09-30

Queued image jobs now place once. The scheduler prefers a healthy `coire-edge-b`, skips a
Studio that already holds an image lease or an unmeasured chat mix, and commits the fence,
lease and runtime binding before the node start command. A missing worker journal fails the
attempt; it does not start generation again. `POST /api/v1/images` admits through the existing
replay-safe transaction, and the Images page can submit a prompt when a model is listed.
Admission remains off unless `COIRE_IMAGE_ENABLED` is set. Focused dispatch tests passed.
Advanced modes, stage cache, coexistence approval, the compatible `/v1` adapter, and
operator Studio evidence are still open.

## Compatible generation, form bounds and stage cache — 2026-10-01

`POST /v1/images/generations` maps supported fields into the existing admission transaction.
Quality is rejected because no capability profile defines a quality mapping, so that refusal
creates no job. The route waits up to `IMAGE_COMPATIBLE_WAIT_S` and returns either PNG URLs
with grant fragments, base64 of the verified blob, or a problem document carrying
`coire_job_id`. Timeout and caller disconnect leave the accepted job running. The Images form
offers width, height, steps, count and seed from the selected model's measured capability.
The library can filter by tag and shows an authenticated thumbnail. A byte-capped stage cache
exists, but the current mflux pipeline records prompt identity bytes rather than reusing native
encoder output. Cache-hit metrics therefore do not prove encoder reuse; T060 and SC-003 remain
open. Advanced modes, coexistence approval and operator Studio evidence remain
open. Focused adapter, cache and web tests passed.

A worker that has not accepted the job is no longer treated as a finished start. `POST /api/v1/images` still returns the admitted queued receipt. Dispatch retries while the worker is unreachable, delivers the same fenced command once the worker is up and has no attempt, and does not send that command again after the attempt is running. Node dispatch tests and the submit route test passed.

## Consolidated local gates and preset editor — 2026-10-01

The role-gated Images preset editor now calls the existing audited admin create, revisioned
update, and retire routes through generated TypeScript types. An edit carries the selected
revision and preserves all other default settings; stale or dependency failures remain visible
for correction. Retirement requires two clicks. T045 is complete. A malformed stored
coexistence profile list is rejected before it can authorize a chat/image mix.

On this checkout, the non-integration, non-engine Python suite passed **1,612 tests** with
2 skips and 147 deselected tests. Ruff check/format, strict mypy (618 files), generated
OpenAPI freshness, and 52 focused image contract/unit tests passed after type corrections.
The web suite passed **116 tests**; ESLint, TypeScript and the production build passed.
`COIRE_TEST_MODEL` and `COIRE_TEST_POSTGRES_DSN` are unset, so tiny-engine and cross-process
PostgreSQL evidence remains open. Image admission remains disabled by default. The other
unchecked parent tasks and operator-run Studio gates are still required for release.

## Recovery and native-runtime reconciliation — 2026-10-01

A fully pruned image event history now yields a current reset snapshot to an older cursor,
while a future cursor remains invalid. Placed jobs recheck current user/key/entitlement access
before resumed dispatch and on each scheduler observation. Revocation records an audit row and
keeps holds in `cancelling` until the node and core prove cleanup; it emits a span, bounded
metric, structured job ID log and alert. Focused event, revocation, dispatch and observation
tests passed. T018/T040 remain open for the rest of their cross-boundary acceptance.

The consolidated branch now includes the missing MIT `mflux==0.20.0` Darwin-only dependency,
hash-pinned lock graph and no-model Z-Image import smoke. `uv lock --check`, all six node
installer tests, and the actual local staged-environment smoke passed. `uv sync --all-packages
--frozen` installed the locked graph on this development Mac. This restores the code portion
of T003 to the current branch; Studio upgrade/rollback and real-weight validation remain open.

The image alert test fixture passed with the local scratch-based `coire-prometheus:dev`
`promtool`, using read-only rule and test mounts and no network. Two repository tests verify
baseline Prometheus/Alertmanager inclusion, diagnostics-only historical services, dashboard
provisioning and content-free queries. T073 is complete; T074 remains open for the full
queue, classifier and chat-regression alert coverage.

Placed attempts now keep their lease and storage hold when a Studio journal or worker reply
is missing. The generic failure helper refuses placed rows without node termination proof;
node 404 during dispatch/observation triggers retry and an alert instead of releasing
capacity while a worker may still run. Twelve focused dispatch, observation and revocation
tests passed after this correction. Full failure recovery and operator reconciliation remain
open under T025/T041/T075.

The local disposable PostgreSQL 17 migration gates passed on 2026-10-01: both image
upgrade/guarded-downgrade tests passed after their Alembic-head assertions were updated
to the branch's actual `0028_image_instance_variant` head. The test databases were
created and dropped by the tests. This verifies migration compatibility for seeded
text/VLM rows and image records; cross-process quota and cancel/publication races
remain open under T008.

The broad local `-m integration` run reached **86 passed, 24 skipped, 1 failed** before
interruption after 9m33s. The failure was the existing MCP lifecycle kill test:
its research call never appeared as a running Studio container within 90 seconds.
A focused rerun reproduced the failure in 3m10s. The disposable stack used locally
cached `:ci` images built 36–47 hours earlier than this checkout; the run remained in
`placing`. These runs are not release evidence for current source. Rebuild the affected
images and repeat the integration gate before attributing this to current code.

After the recovery fixes, the non-integration, non-engine Python suite passed
**1,623 tests**, with 2 skipped and 147 deselected. The web suite passed **116 tests**;
Ruff format/check, strict mypy (619 source files), web lint/build, OpenAPI freshness,
and `git diff --check` passed. The two disposable PostgreSQL migration tests passed
separately. These gates do not replace the required tiny-model and operator acceptance.

The local `coire-node-test`, API, MCP, scheduler and migration images then rebuilt from
this checkout. The four production control-plane images passed the seven-rule local
image policy, including non-root, no shell, read-only runtime and digest-pinned base
checks. This does not include a vulnerability scan or an SBOM gate. A fresh-image
focused Compose rerun follows.

The focused MCP lifecycle kill test passed (**1 passed in 116s**) with rebuilt images.
This explains the earlier `placing` failure as stale local test-image evidence. The
full integration selection is being repeated against the rebuilt stack.

The file-worker and web images also rebuilt from this checkout. API, MCP, scheduler,
migration, file-worker and web production images each passed `scripts/image-policy.sh`
and a local Trivy CRITICAL scan (zero findings, 2026-10-01 vulnerability database).
Syft generated SPDX JSON SBOMs for all six under `/tmp/coire015-*.spdx.json`; these are
local verification artifacts, not committed assets. The CI build/scan gate still needs
the complete image matrix and a passing workflow run.

The next full Compose integration run completed **114 passed, 26 skipped, 1 failed**
in 20m00s. The lone failure was the ops model recovery test: the older locally
cached `coire-agent-ops:ci` image sent `max_completion_tokens` to the current API,
which correctly rejected that unknown field. Current ops source explicitly uses
`max_tokens`. The ops image was then rebuilt, passed image policy and CRITICAL scan,
and received a local SPDX SBOM.

The focused ops recovery case passed (**1 passed in 136s**) with the rebuilt ops
image. The complete local integration selection was then rerun with all seven
affected production images and the CI-only node image rebuilt from this checkout.

The final local Compose integration selection passed **115 tests**, with 26 skipped
and 1,631 deselected, in 18m56s. It used the rebuilt API, MCP, scheduler, migration,
file-worker, web and ops images plus the rebuilt CI-only node. The two earlier
full-suite failures no longer occurred. This is a green local Compose regression
gate, not an image-generation end-to-end acceptance gate: the dedicated fake-worker
recovery tests, real tiny-mflux fixture, full-model Studio checks, reproduction/cache
trials, coexistence benchmark and browser journeys remain open in T025/T072/T075–T085.

## Closure decision — 2026-10-01

Do not close feature 015 or enable image admission. The parent task list still has
56 unchecked items at this checkpoint. The most direct functional gaps are admin image acquisition and
component validation (T027/T033), real output-access and publication/recovery proofs
(T022/T025/T038–T041/T075), advanced mode execution and recipe reproduction
(T049–T058), native encoder reuse rather than identity-only cache hits (T060–T064),
and measured chat coexistence (T065–T072). The tiny-model, full-model/operator and
browser gates (T076–T085) are unproven. Keep `COIRE_IMAGE_ENABLED=false` until
those requirements and their contract/integration evidence are complete.

## Continued implementation — 2026-10-01

T012 is checked after verifying the private blob volume, route-specific upload limits,
purpose-specific API bounds and isolated file-worker mounts. Its topology tests passed
and the combined Compose configuration validated. T008 is checked after adding a
disposable PostgreSQL contention test with independent sessions: one of two concurrent
admissions wins a one-job quota; the other fails without double-counting. The same
database test holds cancellation after its row lock while publication attempts to
proceed, then proves publication is refused after cancellation commits. Together with
the seeded text/VLM migration and guarded-downgrade cases, all six focused persistence
tests passed on local PostgreSQL 17. Ruff and strict mypy passed. These are local
database tests, not substitutes for the broader T025/T075 recovery matrix.

The image acquisition pull contract now requires an immutable 40-character commit for
image and auxiliary kinds. The node worker uses the image-only selected-file snapshot,
checks the inspected revision and declared licence before transfer, and refuses an
incomplete or enlarged local tree before publishing its manifest. The shared node API
contract includes the optional Hub licence. The focused node/client suite passed
**52 tests**. Admin acquisition, reserved validation and two-copy publication are
still open under T027/T033.

T040 is checked after reviewing the durable cursor, retention-gap reset, private SSE
heartbeat and terminal behavior, plus the independent scheduler journal observer's
250 ms progress bound. Its unit/contract selection passed **15 tests**. Browser
reconnection and fault-injection acceptance remain open in T023/T025/T075.

T027 is checked after adding the separate admin image-asset intake contract and
route. Intake requires an explicitly reviewed licence matching Hub metadata,
records the resolved source commit and selected inert file inventory, and creates
the existing durable two-node pull job. The node pull refuses unpinned revisions,
uses only exact allow patterns, and rejects enlarged or incomplete trees. The
reconciler verifies origin and replica manifests against the inspected paths,
sizes, upstream safetensors digests, source revision and each other. Image assets
remain outside `ready` until reserved Studio validation supplies capability
evidence (T033). The focused acquisition/node/admin suite passed **59 tests**;
strict mypy passed **623 source files**. The new migration's upgrade and guarded
downgrade passed on disposable local PostgreSQL 17 (**2 tests**). This does not
count as the required real pipeline or Studio acquisition acceptance.

T030 is checked after reviewing the single-process node supervisor and its agent
composition: launch persists PID, create time, loopback port and reservation before
return; private bearer health gates readiness; restart adoption checks exact process
identity and local copy; uncertain process state retains the hold. The focused
supervisor, node-route, worker-control and storage-topology selection passed
**17 tests**. Real Studio re-adoption and capacity acceptance remain in T077/T083.

T033 remains open. The separate `image_validate` node command now requires a held
reservation and runs in a credential-free offline worker. The base path verifies the
exact local copy, performs a neutral native mflux smoke, rejects degenerate pixels,
and records a thumbnail digest and narrow capability. The reconciler persists
separate origin and replica results before releasing holds and setting validation
time; neither copy can make an image model ready on its own. Auxiliary assets
currently fail closed because mode-specific native validation is still missing.
The focused validation/observability selection passed **6 tests**, and the
non-integration Python suite passed **1,647 tests** with 2 skipped before the last
validation-bridge test was added. Migration upgrade/downgrade and quota contention
passed **3 local PostgreSQL tests** after the new validation columns. No real
mflux smoke has been run.

T029 and T038 are checked after reviewing the resident txt2img callback/preflight
path and the fenced whole-batch publication, owner gallery, grants and deletion.
Their focused selections passed **29** and **26** tests respectively. T046 is
checked after the Images page began refreshing committed jobs and gallery outputs
once per terminal event, preserving the tag during pagination and ignoring events
from a previously selected job. The web suite passed **116 tests** before the
new page regression test; that focused test subsequently passed, as did lint.

T049 and T053 are checked after adding the isolated file-worker normalization
route and ID-bound CPU processor. Generation inputs require exact private UUID
files and <=10 MiB, decode only PNG/JPEG/WebP within 4096 pixels per dimension,
honor EXIF orientation, preserve white-edits/black-keeps mask polarity, and
publish exclusive 0600 PNGs with output digest/size. The separate recipe path
accepts <=64 MiB PNGs without decoding pixels, rejects >64 KiB metadata and
chunk-count bombs, and cannot be selected by a generation-purpose command.
The generated >10 MiB PNG round-trip and file-worker/core contract selection
passed **48 tests**. Core input contracts now reject wrong purposes and input
dimensions before node execution. Owner storage, transfer and advanced pipeline
integration remain open.

T014 is checked after a structural route audit confirmed all 16 native image,
input and output routes depend on the live private-image guard. Its actor,
origin, scope, refusal-audit and revocation contract selection passed **27
tests**. T018 is reconciled with the existing admission, route-guard,
publication and scheduler cancellation modules: successful submit/completion,
refusal and revocation paths each write audit events, while entitlement
grant/revoke authority remains in `identity/entitlements.py`. The focused
authorization, admission, publication and revocation selection passed **35
tests**. T021 is checked after the pinned offline Studio-CPU classifier and
its timeout, RSS, threshold, explicit override and unknown-fallback tests
passed **8 tests**. This does not establish a real classifier run or output
integration; T022 and the operator acceptance tasks remain open.

T047 is checked after the private gallery gained responsive cards, a visible
preview-failure retry, keyboard focus styling and dark-scheme colors. An
expired content grant receives one fresh grant and retry. The web suite
passed **119 tests**, and ESLint and production TypeScript/Vite build passed.
The node now moves staged input writes, fsync and PNG validation off its event
loop; focused node route/journal tests passed **19 tests**. Gallery and job
output projections now share a fail-closed owner visibility predicate, with
explicit download authority rechecked separately; its focused contract
selection passed **31 tests**. T022 remains open pending live classifier
provenance wiring. T062 is checked after reviewing fixed-label cache event,
duration and occupancy instrumentation; its focused cache tests passed **3
tests**. T032/T054 remain open until scheduler input binding and native
advanced execution are connected.

T048 is checked. The human-admin image job list has a bounded cursor, exact
inspect and audited cross-owner kill routes expose status without prompts or
blob paths, and the Activity page shows image jobs and resident image workers.
A revoked owner's entitlement cannot prevent an authorized human admin from
requesting fenced cancellation; uncertain placed work retains its holds.
Worker unload takes the scheduler admission lock, refuses active jobs and
unreleased image leases, persists a draining state and audit before the exact
node stop, then records completion only after a matching zero-reservation stop
receipt. A node failure leaves the instance draining; the dispatcher excludes
draining workers from new image placement. Focused API admin/dispatch tests
passed **10 tests** after the lease check, and the web suite passed **121
tests**, with ESLint, TypeScript/Vite build, strict mypy and OpenAPI freshness
green. Restart reconciliation of a drained worker still belongs to T071 and
the operator acceptance gate.

T023 route contracts include compatible timeout recovery through native owner read;
the focused native/compatible contract selection passed **19 tests**. T024
node lifecycle, fenced route/journal/progress/cancellation, process identity and
credential-isolation selection passed **34 tests**. T026 is checked with
**22** focused storage/quota/grant/receipt/deletion/cleanup tests and web
receipt/progress/expired-auth coverage; the web suite passed **123 tests**.
Coverage is spread across the existing focused test modules rather than the
nominal aggregate filenames in the parent task.

T032 node-owned commands are reconciled with the existing authenticated
`routes/image_jobs.py` and durable journal: reserve/stage/start/status/cancel/
transfer/cleanup all bind job, attempt and fence. Staging now checks the
job deadline throughout streaming, validation and commit; the focused node
route suite passed **14 tests**. Advanced input execution and scheduler
transfer remain open under T054/T056. The full non-engine, non-integration
Python suite passed **1674**, with **2 skipped** and **148 deselected**;
these are not counted as acceptance for the still-open engine and operator gates.

T078 runbook and Compose settings now document the admin acquisition and
licence path, owner/admin stop and unload, live scopes, all bounded quotas,
classifier fallback, grant expiry, physical deletion/retention, paired
DB/blob backup and drain-before-rollback. It continues to identify unfinished
engine and operator acceptance explicitly.

T050 owner upload/read/delete and recipe-import contracts, staging/deletion
unit cases, and new replay/preset-prefix/full-precision/version/replacement
metadata checks passed **15 focused tests**. The import returns direct effective
fields once and explicitly reports the runtime environment as unverified;
end-to-end reproduction remains open under T055/T077/T084.

T074 is checked: the already-provisioned image dashboard and lean-profile
Prometheus rules now include queue/dispatch, cancellation/worker stop, storage,
cleanup, classifier and gateway first-token latency during image activity.
The last signal is a cluster-wide alert, not same-node coexistence approval;
T069/T084 remain open for that gate. The dashboard queries use emitted metrics,
Python provisioning/content-label tests passed **2 tests**, `promtool check
rules` parsed **21 rules**, and the existing `promtool test rules` file passed
inside the local Prometheus image.

T058 gallery cards now offer reuse settings, unchanged regeneration and
new-seed generation. A regenerated request copies the recipe's effective direct
fields and input IDs without reapplying its preset; all actions enter the
native submit path, which issues a fresh intent key. Reuse fills and focuses
the form while retaining advanced bindings even before their controls ship.
The web suite passed **125 tests**, ESLint and TypeScript/Vite build passed.

T067 adds a repeatable 15-minute default same-node benchmark driver and
strict content-free JSON report schema. It concurrently streams pinned chat,
submits/polls image jobs, samples admin node thermal and memory readings, and
queries the existing gateway overhead histogram. It records first-token
p50/p95, decode throughput, image progress, same-node fingerprint evidence,
thermal states and peak node memory. Missing evidence remains explicit; this
is not an operator-run result or coexistence approval. Its **2 deterministic
probe/report tests**, Ruff and strict mypy passed. T084 remains open.

T076 is checked: the local-only tiny mflux factory built a **341,441,216-byte**
fixture under ignored `models/test--image-tiny`, below the 1 GB cap. It wrote
a Store manifest with nine files (SHA-256
`d800d5234f5f08708bc237f8a81beab4beb39c5c0f318ea43331d0cea07df9be`),
and `verify_image_copy` accepted the exact local tree. A second independent
build had identical hashes for all nine content files; the repeat artifact
was removed. The production loader has no test-factory switch or import.
Ruff and strict mypy passed. Real encoder/denoise/decode execution and
operator gates remain open under T077/T083.

The local Apple Silicon tiny fixture has since passed a real mflux encoder,
denoiser and VAE decode through Coire's PNG recipe writer. Recipe and pixel
digests round-tripped, and a repeated seed produced the same pixel digest.
The same real worker now passes a step-boundary cancellation test that proves
its partial attempt directory is removed. The focused engine selection is
**2 passed** with outbound model fetches denied. This is local fixture
evidence only: cache speedups, process re-adoption, transfer cleanup and the
operator's full-model matrix remain open under T077/T083/T084.

T051 is checked after the real pipeline recipe/pixel test and browser PNG
drop/import/reuse/error tests were defined and run. The metadata import UI
discloses missing source and model-component digests and an unverified runtime
instead of promising exact reproduction. After this change the full web suite
is **127 passed**; ESLint and TypeScript/Vite build passed. A separate worker
unit selection now rejects valid img2img, fill, Canny, LoRA, upscale,
full-precision nonzero guidance and negative-prompt specs before cache or
denoise work; it also rejects a missing local model copy before native import.
These are refusal checks, so T052/T056 remain open for working advanced modes.

The form now exposes model-advertised modes, decimal guidance, supported
negative prompts, and private source/mask/control uploads. It blocks missing
source bindings and unsupported restored modes; ordered LoRA and upscale
selection still require the auxiliary registry picker. The PNG import waits
for missing source replacements, posts digest-to-input rebinding for the
server's ownership/dimension/hash validation, and blocks restoration when
model components are absent. The browser/API upload and replacement tests
pass. The complete web suite after these changes is **132 passed**, with
ESLint, Prettier and TypeScript/Vite build green. T055/T057 remain open for
complete environment comparison and all advanced controls.

Normal node transfer now deletes its staged private inputs before the final
output cleanup receipt; cancellation and explicit cleanup already did so.
A focused transfer test verifies this order, and the node route/cleanup
selection passed **16 tests** before the new regression test, then the node
job route module passed **15 tests** including it. T039 remains open for
cross-boundary orphan/quota recovery acceptance.

T080 is checked on this checkout. Fresh OpenAPI generation passes `--check`,
and a new `openapi-typescript` output is byte-identical to the tracked
`schema.d.ts`. The full non-integration, non-engine Python selection passed
**1,689 tests** with **2 documented unrelated skips** (Darwin fallback on
this Mac and an existing third-party supervision exception) and **150
deselected integration/engine cases**. Ruff format/check passed for 1,304
files, strict mypy passed for 633 source files, and `git diff --check`
passed. The complete web selection passed **132 tests**; ESLint, Prettier,
TypeScript and Vite build passed. Engine and cluster gates are tracked
separately under T077/T081/T083/T084.

The image timeline now displays the server's safe terminal failure code and
explicitly says cache and worker residency details are unavailable when the
job contract has no such measurements. Focused timeline/hook tests pass.
T063 stays open until actual cold, warm, evicted and residency facts are
available through the typed job stream.

Image dispatch now takes the same transaction-scoped, stable-order Studio
admission locks as chat model placement before reading chat/image occupancy.
The helper rejects duplicate lock identities, and the focused shared-lock
and dispatch selection passed **7 tests**. T068/T072 remain open for
cross-process chat arrival, profile matching and physical contention proof.

The native mflux Z-Image text encoder is now cached as evaluated MLX arrays,
keyed by runtime, model manifest, dependency digests, adapter identity,
prompt, negative prompt and guidance. A separate LRU enforces the configured
byte cap; changing the adapter identity clears both prompt caches. On the
local ignored tiny fixture, **1 cold + 20 warm** generation trials invoked
the real encoder once, produced identical pixel hashes, and emitted exactly
20 prompt-cache hits. A changed prompt missed and invoked the encoder once
more. The focused real-engine selection passed **3 tests**; the cache unit
selection passed **21 tests**, and Ruff/mypy passed. T060/T064 remain open for
control preprocessing, actual LoRA stack replacement and cluster matrix
evidence.

The same local engine selection now runs a real generated PNG through the
node's authenticated transfer body and exact receipt validator, then proves
receipt-aware scratch cleanup removes the Studio PNG and marks the journal
cleaned. A fake core endpoint receives the bytes; no production Studio or
core service is contacted. The local engine module passed **4 tests**.
Re-adoption of the live native child and the operator matrix remain open
under T077/T083.

A deterministic test-only fake image pipeline now drives the authenticated
worker control API without model weights. Local integration-marked tests prove
one serial active job, exact-command replay, changed-fence refusal and a
durable node journal after agent reconstruction; both passed without starting
the Compose fixture. Scheduler restart/publication races remain open under
T025/T075. The metadata UI now checks the normalized replacement SHA-256
before binding a reattached source, with server validation still authoritative.
Metadata import distinguishes missing model components, missing source inputs,
changed runtime version and an otherwise unverified environment. Focused
metadata tests passed **7**; T055 remains open for verified hardware/runtime
equivalence and complete dependency rebinding.

The affected local arm64 images (`coire-api`, `coire-scheduler`,
`coire-file-worker`, `coire-migrate`, `coire-web`) rebuilt from this checkout
as `:015-local`. Each passed `scripts/image-policy.sh`, including no-shell,
non-root, read-only compatibility and pinned-base checks. Trivy 0.74.0
reported no CRITICAL findings in all five images, and Syft wrote valid
SPDX JSON SBOM files under `/tmp/coire-015-*.spdx.json` (not committed).
`docker compose -f deploy/compose/compose.yaml config --quiet` passed.
T081 stays open for a fresh full local integration run and immutable node
installer/text/VLM smoke after the final source state.

The node's production four-second TERM/KILL grace was exercised with a
simulated stubborn child on this Mac. Exact PID identity remained required;
the reserve was released only after simulated process death, and the stop
returned in under five seconds. The full supervisor selection passed
**11 tests**. T041 stays open for a whole-job cancellation-to-terminal
measurement including scheduler dispatch and scratch cleanup.

An isolated `coire-it` Compose run initially found that the Linux node-test
image lacked Pillow, which `image_jobs.py` now imports to validate staged
inputs. The node package now pins Pillow 12.3.0 (HPND licence), matching the
existing file-worker workspace pin; `uv lock` resolved and the arm64 node-test
image rebuilt successfully. The same Compose command then passed all **3**
image job/recovery integration tests in **67.73 seconds**, with fresh migration,
API, scheduler, two simulated nodes and authenticated node registration.
No real Studio or production cluster was contacted. T081 remains open for the
full integration and immutable installer/text/VLM gates.

Normal output rows now require a structurally valid `ImageClassificationResult`
with a matching normal tag before owner reads, grants or shared projections
can expose them. Missing, unavailable and conflicting provenance is refused;
explicit policy still requires live entitlement at download. The focused
authorization/gallery/download selection passed **30 tests**. The node-to-core
classifier stage is still unconnected and new outputs therefore remain
owner-private `unknown`; T022 stays open until that measured stage and full
boundary evidence are in place.

The pending-terminal transfer maintenance pass now pages through old failed
and cancelled jobs, locks each row, refuses any job with a published output,
and retries no-follow deletion of its exact private staging attempt. The
deletion helper reports whether bytes were actually found, so absent staging
does not inflate purge counters. The focused maintenance selection passed
**8 tests**. T039 remains open for older-attempt orphan inventory and
operator retention evidence.

Recipe import now checks current live owner/key/entitlement authority for the
base and each auxiliary model before treating its digest as reusable. A
revoked or unauthorized asset is reported as unavailable and the browser
keeps import blocked. The focused replay, rebinding and route selection
passed **8 tests**. T055 remains open for verified same-environment matching.

Coexistence admission now rejects an approved row unless its stored report
shows at least 15 minutes, actual image progress, no swap or thermal alarm,
the pinned runtime and first-token p95 at or below 1.5 seconds. The focused
selection passed **20 tests**. A disposable local PostgreSQL check also
proved two independent connections serialize on the same node advisory lock
(**1 test**). T065/T069/T072 remain open for full profile administration,
live regression handling and chat/image contention scenarios.

The full isolated Compose integration selection finished with **118 passed,
21 skipped, 48 deselected** in **19m 13s**. It used disposable `coire-it`
services and two simulated Linux nodes; the skipped scenarios need their
declared model/cluster prerequisites, so T081 is still open. The disposable
local PostgreSQL image migration, guarded downgrade, registry kind migration
and cross-connection quota test passed **3 tests**.

A typed human-admin coexistence report route now accepts only measured
15-minute same-node evidence with chat first-token p95 <=1.5s, gateway p95
<=20ms, image completion/progress, no swap/thermal alarm and a current
published base plus validated chat variants. It writes one audited approved
profile. Admission revalidates the entire stored typed report, its canonical
hash and the current node hardware/agent-runtime fingerprints before sharing
the Studio. The focused profile/route selection passed **27 tests**, and the
admin guard sweep passed **12 tests**. T069 remains open for live latency and
thermal circuit-breaker cancellation and operator benchmark evidence.

Human admins can now invalidate an admitted coexistence profile through an
audited DELETE route. Subsequent mixed placement refuses the row. The
generated OpenAPI and TypeScript contracts include both admission and
invalidation. The focused image/recovery Compose rerun passed **3 tests**.

Core maintenance now removes **all** known attempts for old failed or
cancelled jobs, then pages old unowned staging directories. The orphan pass
requires no durable job or output, checks a one-hour age for the job,
attempt and every recognized file, and uses no-follow private-directory
deletion. Focused maintenance passed **9 tests**. The alert set now has
**23 valid Prometheus rules**, including orphan cleanup failures; the
dashboard and storage runbook cover both passes. T039 remains open for quota
reconciliation and operator retention evidence.

Newly dispatched recipes bind their environment digest to the declared
Studio hardware fields, agent version, pinned runtime and base manifest.
Recipe import checks current healthy Studio declarations and distinguishes
changed from matching-but-unverified environments; it does not claim exact
pixel reproduction without the hardware gate. Focused metadata/dispatch
tests passed **12**. T055 remains open for full environment equivalence.

The latest broad local non-integration/non-engine Python run before the
environment fingerprint change passed **1,705 tests**, 2 unrelated skips,
156 deselections. Strict mypy passed **636 source files**; focused tests for
the subsequent fingerprint change passed. The complete isolated Compose run
above predates these latest maintenance and metadata changes, so it is not
claimed as a final-source gate.

A fresh Trivy database now reports `CVE-2026-84782` (HIGH, `libssl3`, no
fixed Debian 12 version) in the pinned distroless runtime used by API and
scheduler images. The earlier clean scans used an older scanner database and
are superseded. A locally pulled Debian 13 distroless base scanned without
HIGH/CRITICAL findings, but runtime migration needs compatibility/build and
service regression checks before changing the pinned base. T081's clean
affected-image scan gate remains open; no scanner rule was relaxed.

The Debian 13 compatibility check then passed: the API canary imported its
application and OpenSSL 3.5.7, and the refreshed digest-pinned runtime was
applied to the API, scheduler, MCP, migration, file worker, ops, failover,
agent and run-relay Dockerfiles and deployment lock. `urllib3` was upgraded
in `uv.lock` from 2.7.0 to fixed 2.8.0 (existing MIT-licensed dependency)
after the refreshed scanner flagged two HIGH findings in the ops image.
All nine rebuilt runtime images pass the repository image policy; all nine
pass the latest Trivy HIGH/CRITICAL scan; all nine have validated SPDX-2.3
SBOMs under `/tmp` and imported their service packages/OpenSSL under
`--network none`. `scripts/pin-images.sh --check`, `uv lock --check` and
Compose config pass. The isolated Debian 13 Compose image/recovery selection
passed **3 tests in 60.43 seconds**. The full integration and operator
hardware gates remain open under T081/T083/T084.

The scheduler now binds new recipe environment hashes to declared Studio
hardware/agent/base bytes and probes current healthy nodes during import.
It added a thermal circuit breaker: a fresh serious/critical Studio sample
blocks new placement and requests an audited fenced stop for an active image
job, retaining the lease and byte holds pending node cleanup. Focused thermal,
dispatch, observation and revocation tests passed **36**; the alert rule set
validates at **24** rules after the thermal alert.

Gateway first-token and overhead histograms now carry the bounded selected
Studio name. A scheduler monitor queries five-minute same-node first-token
p95 for each currently approved coexistence node every 15 seconds. A value
above 1.5 seconds or an unavailable/malformed monitoring response
atomically invalidates approvals and requests audited fenced cancellation for
active image jobs. A valid empty vector leaves the approval in place because
there is no live chat sample. Typed parsing, bounded payloads, node label
validation, failure handling, invalidation and cancellation passed **8
focused tests**. Prometheus validates **26** image alert rules and the
dashboard JSON parses; the browser-facing image observability tests pass.
T069 remains open until same-node hardware measurements and end-to-end
regression/cancellation evidence are recorded.

After the Debian 13 base and initial thermal/latency code, the local broad
Python selection passed **1,717 tests**, 2 unrelated skips, 156 deselections.
The subsequent fail-closed monitor change passed focused tests but needs a
new broad run. The web suite passed **134 tests** across 36 files with ESLint,
TypeScript and production build green. The actual local no-model installer
smoke for `mlx_lm.server`, `mlx_vlm.server` and mflux imports passed.

T025 is complete: the fake worker now injects a failure after the first output
of a two-output batch. The worker removes partial scratch, retains the failed
fence without regeneration on replay, and accepts a later distinct job. The
queue/replay and restart/fenced-cleanup integration selection passed **4 tests**.
These deterministic local lifecycle tests do not substitute for the operator
cancellation and publication gates in T083.

T055 is complete at the import boundary: current owner authority, base and
auxiliary model digests, input purpose/digest rebinding, direct settings and
declared Studio environment are checked. Matching declarations still return
`runtime_environment_unverified` and `exact_reproduction_available=false`;
only T084's physical trials can establish the pixel claim. The focused recipe
import and authorization selection passed **26 tests**.

A local synthetic-data Chromium pass against the built Images page exposed a
collapsed settings form, absent page-level dark colors and an incomplete
select focus outline. The page now uses a responsive settings grid, full-width
Images panels, dark theme tokens and explicit focus for selects. At 1024 and
1440 pixels in light and dark modes, Chromium reported viewport width equal
to document scroll width, and all 12 sampled keyboard Tab targets were
visible. Its accessibility tree exposed the Images landmark, model, prompt,
Generate action and image-jobs region. Four screenshots and the structured
report are stored outside the repository as `/tmp/coire-015-images-*.png` and
`/tmp/coire-015-browser-report.json`; they contain only synthetic model data.
The final page still needs a VoiceOver journey and real populated-gallery
journey before T082 can close. The web suite passed **134 tests**, lint and
TypeScript/Vite build after the visual changes.

T022 is complete at the output-access boundary. Initial image route refusals
and later gallery, detail, deletion and download refusals now write separate
content-free audit rows. Owner scope, current explicit entitlement/key scope,
unknown exclusion from sharing, defensive normal-tag provenance and recipe
prompt preservation are covered by the gallery/download/authorization contract
selection (**31 passed**). This does not claim that a classifier is connected:
newly generated standard outputs still publish as private `unknown` until T033
and the node-to-core classification path are completed.

T033 now includes reserved offline classifier validation: the node verifies the
exact local manifest and pinned revision, then runs the CPU classifier against
a synthetic local PNG. A failed or `unknown` smoke cannot produce validated
acquisition evidence. The focused image-validation selection passed **6
tests**, and strict mypy passed the changed modules. LoRA, control and upscale
kind-specific execution validation and node-to-core tagging remain open.

## Physical baseline and native fixture — 2026-10-01

The development Mac is Apple Silicon (`arm64`, macOS 27.0.1). Its existing
ignored `models/test--image-tiny` fixture is 341,441,216 bytes, generated with
seed 15015 for mflux 0.20.0; its Store manifest digest is
`d800d5234f5f08708bc237f8a81beab4beb39c5c0f318ea43331d0cea07df9be`.
With Hub access disabled, the real tiny-mflux encoder, MLX denoiser, VAE,
recipe/pixel round trip, cancellation, 20 encoder-cache reuse trials,
changed-prompt miss and PNG transfer/Studio-scratch cleanup selection passed
**4 tests** in 3.07 seconds. This is local test-model evidence, not a
production-weight image-quality or same-node chat benchmark.

Both real Studios were reached over authenticated `mcteer@` SSH after the user
explicitly authorized these checks. Each is `arm64`, macOS 27.0, hardware
`Mac15,14`, with 274,877,906,944 bytes of RAM and a running coire-node 0.2.0
agent. Edge A has six existing model manifests, 171 GiB of text/VLM model
data and an active 1.5B text engine; edge B has six model manifests, 1.9 GiB
of text/VLM data and no active engine. Neither current node environment has
`mflux` installed, and neither Store contains an image asset. No image model
was acquired by these read-only inventory checks. The production-weight
validation, replication, tagging, ten-trial reproduction and 15-minute
coexistence gates in T083/T084 therefore remain open pending the audited
image acquisition and current node runtime installation.

The classifier validation smoke was moved under the existing 10-second
kill-supervised child and records sampled child RSS. The focused classifier
and validation selection passed **14 tests**, with Ruff and mypy green.
The complete non-integration, non-engine Python selection passed **1,722
tests**, with 2 skipped and 157 deselected. Strict mypy passed 638 source
files. Repository Ruff format/check, generated OpenAPI freshness and
`git diff --check` passed after formatting an existing image-agent line.

A populated-gallery Chromium pass exposed action buttons clipped by the
200-pixel card width. The gallery now uses 270-pixel minimum cards and a
two-column action grid with wrapping text. Synthetic thumbnail and all five
actions render inside the card at 1024 and 1440 pixels in both themes;
document width equals viewport width in all four cases. The full-page
screenshots and structured report remain outside the repository as
`/tmp/coire-015-gallery-*.png` and
`/tmp/coire-015-browser-gallery-report.json`. The web test suite passed
**134 tests**; lint and TypeScript/Vite build passed. T082 remains open for
a complete screen-reader and real-service journey.

The current branch's locked node wheelhouse selected 87 macOS arm64 wheels.
The immutable `0.2.0-eb2ddbd382f5` environment installed on both Studios;
the installer passed imports for coire-core/node, mlx-lm, mlx-vlm and mflux,
both engine CLI smoke checks and the pinned Z-Image import before flipping
`envs/current`. After one at a time agent restarts, both `/ready` endpoints
returned 200 and unauthenticated `/node/health` returned 401. Authenticated
health returned 200 on the control path. Edge A re-adopted its pre-existing
text engine PID 25184, but an authenticated text completion returned 502 and
its loopback health gave an empty response. Stopping and starting that exact
engine via the node API produced new PID 55229; an authenticated 4-token
text completion then returned 200 with one choice and usage in 0.615s.
Edge B loaded the existing 256M SmolVLM through the node API, reached ready,
returned 200 with one choice and usage in 0.585s, and was stopped with a
confirmed terminal state. Neither Studio acquired or ran an image model in
these checks. The transient edge A failure is recorded as a re-adoption
readiness defect to investigate under T071/T081 rather than a passed
re-adoption gate.

The native encoder cache key now includes the variant and declared Studio
environment fingerprint. The real tiny-engine selection again passed **4
tests**, including 20 warm hits and distinct misses for changed prompt and
environment, with byte occupancy within its configured bound. Ruff and mypy
passed the changed runtime/test files. Adapter and control-stage execution
remain open under T056/T060/T061/T064.

The observed edge A re-adoption defect led to a node change: a surviving
process is now held as `starting` until a fresh one-token completion proves
it can serve. A dead adopted process fails without releasing a live hold,
and a concurrent stop cannot be overwritten by a late readiness response.
The adoption counter, span, chat dashboard panel, alert and instance
runbook were updated with this path. The fake text/VLM lifecycle and image
observability selection passed **30 tests**; Ruff, strict mypy, alert YAML
and dashboard JSON checks passed. This fix has not yet been rolled out to
the Studios, so the physical re-adoption regression still needs a repeat
after a subsequent versioned node install.

A terminal failed image-worker result now triggers fenced node scratch removal
for both generated PNGs and staged inputs. A pre-existing failed journal can
repair its cleanup acknowledgment on a later status request. Core retains the
execution lease and output hold until the exact node reports
`scratch_cleaned=true` and core transfer staging is absent, then records a
content-free terminal failure audit and event. A poisoned symlink leaves the
journal nonterminal and the outside file intact. The focused recovery
selection passed **26 tests**. The complete local unit/contract selection
passed **1,727 tests**, with 2 skipped and 157 deselected; strict mypy passed
638 source files, Ruff format/check and generated OpenAPI freshness passed.
This closes the proved failed-worker branch of T039/T041. Orphan scratch
sweeps, advanced input delivery and the healthy-stop latency gate remain open.

The first advanced-mode slice now passes a retained, owner-bound image-to-image
input from API admission through scheduler reservation, Studio staging and
worker-local digest verification to mflux 0.20.0. Its native progress reports
the actual denoise portion against the requested step total, and the recipe
keeps the full-precision decimal strength. Base acquisition validation now
smokes both text-to-image and image-to-image before advertising those two
modes. Six real tiny-mflux tests passed offline in 5.20 seconds, including
image-to-image generation and the dual-mode acquisition smoke. Local scheduler
transfer-order, admission/reference, node contract and validation selections
passed. Fill, control, LoRA, upscale, owner active-reference cancellation and
full production-mode validation remain open under T033/T054/T056.

Owner deletion of an input with active references now takes the same quota
lock as admission, requests cancellation of each exact referencing job in
the deletion transaction and tombstones the input. Physical purge still waits
for terminal node cleanup and reference release. The deletion and cancellation
selection passed **15 tests**, including the referenced-input path; strict
mypy and Ruff passed the changed files. The wider cancellation latency and
all advanced modes are still open.

Image placement and chat placement now share transaction-scoped Studio locks
and the authoritative memory ledger. An image worker receives a pinned image
reservation before dispatch; admission counts every active chat, sandbox and
image reservation, permits reuse only for the exact resident image instance,
and withholds new image work when an image hold has uncertain residency. New
chat loads check the approved current coexistence profile against every held
image worker and the resulting complete chat variant set. Admin unload marks
the image hold released only after the exact node confirms its process stopped
with zero reserved bytes; a missing reply keeps the hold and draining state.
Admission also fails closed if a live worker has no matching durable hold,
including workers inherited from an earlier runtime version.
Focused coexistence, dispatch, placement and unload tests passed **41 tests**.
The complete local unit/contract selection passed **1,734 tests**, with 2
skipped and 159 deselected; changed-file strict mypy and Ruff passed. The
cross-process PostgreSQL concurrency and live inference priority scenarios
remain open under T065/T068/T070/T072.

A disposable localhost PostgreSQL 17 container ran the image migration,
registry-kind downgrade guard, populated chat migration, image quota
serialization and transaction advisory-lock tests. The selection passed
**7 integration tests**, with 15 unrelated tests deselected, in 3.35 seconds.
The scratch database container was stopped and removed after the run. This
adds local migration and concurrency evidence for T081; the image-build,
scan, SBOM and complete cluster acceptance gates remain open.

`docker compose -f deploy/compose/compose.yaml config --quiet` passed. Current
API and scheduler images built as native arm64 images. Both passed all seven
`scripts/image-policy.sh` checks, including no shell, non-root, read-only
compatibility, digest-pinned bases and the core-hosting rule. Trivy reported
no CRITICAL findings for either image (exit 0). Syft generated SPDX JSON
SBOMs with 88 packages each; the files are in `/tmp`, outside the repository.
The full node/worker image matrix and production image-model checks remain
open under T081/T083.

The Linux-only `coire-node-test` CI image also built and produced an SPDX SBOM.
Its initial local CRITICAL Trivy scan failed on the pinned Debian 12 test
base: Perl, SQLite and zlib findings had no fixed package version in that
base. Updating only this CI image to digest-pinned Python 3.13 on Debian 13
and the matching pinned Git package produced a clean rebuild, CRITICAL Trivy
scan (exit 0), SPDX SBOM and node/core import smoke. The test image still
stays outside the production image-policy matrix and is never deployed.

The node acquisition reservation journal now fails closed if its existing
file is malformed or unreadable. Previously a parse failure silently
returned an empty ledger and could admit image/model memory over an uncertain
live conversion hold. The focused persistence and shared-lock selection
passed **6 tests**; changed-file Ruff and strict mypy passed. The corrupt
journal remains intact for operator recovery.

The CI base refresh initially exposed a missing digest entry in
`deploy/compose/images.lock` through the full unit gate. After adding the
pin, `scripts/pin-images.sh --check` and all four pin tests passed. The full
local non-engine, non-integration suite then passed **1,737 tests**, with 2
skipped and 159 deselected. The image runbook now describes shared holds and
corrupt-journal recovery.
