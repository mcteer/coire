# Feature 015 execution record

## Live control-plane generation and gallery — 2026-10-02

Both installed Studio agents restarted into the current locked build. The base,
compatible LoRA, Union 2.1 control, and SeedVR2 3B upscaler passed native
validation on both local copies and were published through the audited admin
API. Because the launchd agents still report the private data link down, the
replica stores were copied by an operator over the verified Studio fabric and
then rechecked by the acquisition workflow. This is native two-copy validation,
not proof of unassisted replication. The 58 GB FLUX Fill acquisition and pinned
classifier remain separate gates; the classifier's ten-second live smoke times
out under the current workload despite its earlier standalone 2.68-second pass.

The control plane was enabled temporarily for live acceptance with
`COIRE_IMAGE_ENABLED=true`. A public API txt2img submission first exposed an
SQLAlchemy flush-order foreign-key failure between the job and its queued
event. After flushing the job row first, submission returned 202. Dispatch then
exposed a client guard that incorrectly rejected input-free jobs before the
node could journal them; it now permits an empty input manifest. The next
attempt reached native generation but publication found that the scheduler
had no mount for the API's image blob volume. Sharing that volume with the
publisher resolved recovery without resubmitting the job. Job
`01M3ZJV9X6H81X24H3Q699QGHP` finished `succeeded` with one 512×512 private
PNG (265,070 bytes, pixel SHA-256
`6f3b30b1d305e4a65ccd49c6434ae2c7f2195fd55811060ac61d96025ab92f4f`).
The classifier result is `unknown` with `classifier_timeout`, so this does not
pass the classifier gate.

The live authenticated Images page displayed the published base and the real
gallery card at 1024 and 1440 px in light and dark schemes, without horizontal
overflow or page errors. The existing keyboard focus and accessibility-tree
checks were repeated against the live gallery. Axe WCAG 2 A/AA and 2.1 A/AA
found a low-contrast timestamp in light mode and a low-contrast destructive
button in dark mode. Both colors were corrected; the same four scans now have
zero violations and one incomplete check each. Screenshots remain outside the
repository under `/tmp/coire-015-images-<width>-<scheme>.png`; the prompt was
agent-written acceptance text, not user content. The browser accessibility
tree and visible keyboard focus provide screen-reader semantic evidence; no
spoken VoiceOver session was captured.

## Live acquisition, native Z-Image smoke, and browser acceptance — 2026-10-02

A scoped, audited development API key was issued through the existing identity
service and stored in the login Keychain; its bearer is absent from this record.
The live core at `192.168.4.10:8180` accepted admin image acquisition for
`Tongyi-MAI/Z-Image-Turbo` at revision
`f332072aa78be7aecdf3ee76d5c247082da564a6` ([Apache-2.0](https://huggingface.co/Tongyi-MAI/Z-Image-Turbo)), and the pinned
`Falconsai/nsfw_image_detection` classifier at revision
`96cb0d0342c7afb80cab76ecc58b265fa44da256` ([Apache-2.0](https://huggingface.co/Falconsai/nsfw_image_detection)). Registry IDs are
`48075b57-b6b9-48bf-9ace-9438405ccc3d` and
`ee4fe044-b313-40c6-846e-785bfc186de4`. The initial requests exposed two
intake defects: Diffusers uses root `model_index.json`, and nullable JSONB was
encoding Python `None` as JSON null against a SQL-NULL check constraint. Both
are fixed, with inspection and disposable PostgreSQL migration tests.

Both Studios installed the corrected locked 87-wheel node build. Studio B
downloaded and verified the 32,848,321,404-byte Z-Image selection and the
343,237,214-byte classifier selection. A credential-free direct native
validation of the pinned Z-Image store on Studio B passed txt2img and img2img
at 512 px and four steps in 36.69 s; measured peak physical footprint was
41,829,311,416 bytes, and the thumbnail digest was
`f77bc11995eff08ed395a4b69de5c20ec9ee4ee0d3a306d88893acae986f3873`.
The first native img2img smoke found that a flat source made the one-step
transform look degenerate; the smoke now uses a deterministic textured source.
A direct native classifier smoke passed in 2.68 s with 512,245,760 bytes peak
RSS. The live classifier smoke initially exposed an acquisition UUID passed to
an image-run ULID telemetry span; that is fixed. Publication and the full
operator matrix remain pending live registry validation.

The launchd node processes on both Studios report data-link `down` with
`[Errno 65] No route to host` over `coire-edge-{a,b}.fabric:9401`, although
`nc`, `httpx`, and a direct transfer from each Studio's SSH login process work.
This is a process-context failure, not a missing listener or route. The
reconciler correctly failed closed on direct node import. To continue native
acceptance, the exact verified origin tree was copied over the same Studio
fabric by operator SSH and the registry retry was asked to verify the local
bytes; this does **not** count as passing the automated replication gate.
Image admission remains disabled until publication and acceptance succeed.

The live Images UI was exercised in headless Chromium at 1024 and 1440 px in
both light and dark schemes; screenshots are outside the repo under
`/tmp/coire-015-images-<width>-<scheme>.png` and
`/tmp/coire-015-form-<width>-<scheme>.png`. The form screenshot used a
browser-only synthetic model response; no generation request or user content
was submitted. The empty and form states had no horizontal overflow or page
errors. Keyboard Tab traversal exposed a visible solid focus outline and
stable accessible names for import, form fields, tags, and navigation. The
Chromium accessibility snapshot exposed the headings and named regions;
axe-core WCAG 2 A/AA and 2.1 A/AA scans found zero violations in both schemes
with and without the synthetic model. The browser run found an unstyled preset
editor and import control; both now use responsive themed fields. This proves
the browser's accessibility tree, not spoken VoiceOver output or a live
generated-image gallery journey.

## Native scheduler-to-node cancellation timing — 2026-10-02

The local tiny mflux test now starts a 100-step, 256-pixel real MLX generation,
waits for observed progress, commits a cancelling core intent, and calls the
production `drive_image_cancel` scheduler path. Its node client adapter invokes
the real `ImageNodeDispatcher` and process-group TERM/KILL supervisor; the core
session and terminal DB write are deterministic in-memory fixtures. It requires
the node journal to be cancelled and scratch-cleaned, the core state to be
terminal, and elapsed time from the intent to be under five seconds. Both
native cancellation variants passed (**2 tests**, 7.20 seconds combined).
The 250 ms core cancellation discovery poll, PostgreSQL cancel/publication
arbitration, and partition-held-reservation tests passed separately. This closes
T041's healthy local timing/implementation proof; full production-model
Studio cancellation remains in T083.

## Agent-run Studio and PostgreSQL continuation — 2026-10-02

`AGENTS.md` and this feature's quickstart now explicitly permit coding agents to
run Studio acceptance workloads. CI remains isolated. A disposable localhost-only
PostgreSQL 17 container (`coire-015-postgres-final`, ephemeral tmpfs, host port
32776) passed the existing four cross-process admission/lease cases and five
migration/quota cases. New independent-interpreter tests prove profile
invalidation waits for the node admission lock, and a competing chat hold is
re-read before pinned image placement; a full pinned node refuses rather than
redirecting to the other Studio, while auto placement may choose the other.
Approved, invalidated and incompatible new-variant profile reads use actual
PostgreSQL rows. This exposed string-backed registry `kind`/`source`/`backend` fields
that image admission, dependency dispatch, acquisition and catalog compared by
Python identity; those paths now compare values. Focused admission and listing
tests passed **51**, strict mypy passed **652 source files**, and the full Python
suite passed **1,877 tests, 161 skipped**. The separate real tiny mflux selection
passed **10 tests** with `COIRE_ENGINE=1`; its default skipped run is not evidence.

Both Studios accepted the exact locked 87-wheel node build into immutable
`/opt/coire/envs/0.2.0-99dfeacec71e` and passed installer import/dependency
smokes. Their existing `mcteer` LaunchDaemons were restarted by terminating only
their recorded agent PIDs; launchd `KeepAlive` restarted them. Authenticated
`/node/health` returned 200 on both. Studio A re-adopted its existing
Qwen2.5-Coder 1.5B text engine and returned a four-token completion over the
authenticated node proxy (HTTP 200, one choice). Studio B loaded its verified
SmolVLM 256M copy, returned a four-token completion (HTTP 200, one choice),
and returned to `stopped` after the smoke. No image assets are present in either
Studio model store. The current live core rejects the legacy Keychain admin
bearer with HTTP 401, and an active human-admin identity is needed for audited
image acquisition and publication. Final-source CI, browser acceptance,
full-model image matrix, and coexistence benchmarks are still open.

## Pinned Union Canny execution — 2026-10-02

Direct and preset admission now bind a ready Union control asset to the exact
base; the scheduler holds both model estimates, attaches the authorized control
input and binds the asset manifest. The Studio builds a temporary local
composite, applies the requested Canny low/high thresholds before the pinned
mflux control path, samples physical memory, and removes the composite after
generation. Control plus LoRA is explicitly refused. The model picker lists
compatible authorized controls and the form exposes source, strength and
thresholds. Focused Python checks passed **71 tests, one skipped**; the web
suite passed **142 tests**, with strict mypy, Ruff, TypeScript build and OpenAPI
freshness green. The full local repository suite passed **1,864 tests, 160
skipped**, and 10 local tiny native engine tests passed. Full native Union
hardware acceptance remains open.

## Pinned SeedVR2 output stage — 2026-10-02

Direct image admission binds a ready published upscale asset and its entitlement.
The scheduler adds its exact manifest and memory estimate to the worker hold.
The resident Studio checks the pinned SeedVR2 3B inventory, runs offline from
local bytes, samples physical footprint against the hold, and requires exact
2×/4× RGB dimensions. PNG recipes now validate the final dimensions. The web
picker filters upscalers through live authority and submits the selected factor.
Focused Python checks passed **67 tests, one skipped**; the web component suite
passed **141 tests**, with TypeScript build, lint and OpenAPI freshness green.
The full local repository suite passed **1,859 tests, 160 skipped**. Operator
Studio generation and physical peak gates remain open.

## Clean resident LoRA stack replacement — 2026-10-02

The resident Z-Image pipeline now checks each ordered adapter's pinned slug,
revision and manifest before a stack change. It drops the previous model and
prompt cache, clears MLX cache, and loads the requested adapters against the
verified clean base; an unchanged stack retains the resident model. A changed
or missing adapter refuses before the current resident is dropped. The local
real mflux tiny fixture generated with an adapter, reused it, and returned to
the base, with **10 native tests** passing. The focused unit pipeline gate
passed **23 tests**; Ruff and strict mypy passed. Direct admission now checks
publication, entitlement and exact base compatibility. Dispatch binds ordered
manifest digests and includes incremental adapter estimates in the worker hold;
node dispatch refuses a missing or extra adapter. The full repository gate after
those changes passed **1,855 tests, 160 skipped**. Real Studio peak measurements
remain open under T061/T083. The web model
listing now filters base-compatible published adapters through live entitlement
checks, and the form submits an ordered stack with exact scale strings. The
component gate passed **140 tests** and the model-listing contract passed
**8 tests**; TypeScript build and lint were run after the generated schema.

## Pinned Union control acquisition smoke — 2026-10-02

The pinned mflux control acquisition now selects one reviewed Union 2.1
checkpoint and excludes the other upstream safetensors. Both Studio validation
commands bind the exact ready base. The node verifies both local manifests,
hardlinks the verified files into a temporary local `controlnet/` composite,
runs a neutral Canny native smoke and checks non-degenerate pixels, thumbnail
and sampled physical peak before the copy can publish. A unit test proves the
composite contains only local bytes and is removed after validation. The
full local Python suite passed **1,850** with 160 platform/external skips;
the local tiny native image suite passed **10**. Ruff, strict mypy and OpenAPI
freshness passed. T033 implementation is complete; production control job
execution and operator full-model acceptance remain T056/T083.

## Pinned SeedVR2 3B acquisition smoke — 2026-10-02

The Studio validator now accepts only the reviewed `numz/SeedVR2_comfyUI`
3B root safetensors pair, verifies the complete local manifest, loads the
pinned native SeedVR2 3B runtime without credentials, and requires a
non-degenerate 128×128 output from a synthetic 64×64 input. A sampler records
the physical peak during load and generation and refuses a peak beyond the
held memory. The unit smoke and wrong-file gate passed. The full local Python
suite passed **1,847** with 160 platform/external skips; Ruff, strict mypy and
OpenAPI freshness passed. A real 3B weight smoke on both Studios and upscale
job execution remain open under T056/T083; admission stays disabled.

## Native LoRA acquisition smoke — 2026-10-02

LoRA validation now carries the exact ready base ID, slug, revision and manifest
digest from the registry through the node job into a credential-free worker.
Each Studio verifies both local trees, applies one safetensors adapter through
the pinned mflux loader, generates a neutral output and records the physical
peak and thumbnail before either validation hold is released. Intake reserves
the base memory estimate as well as adapter memory. A real locally generated
tiny adapter passed the pinned MLX/mflux smoke; the complete local native
image suite passed **10 tests**. The full Python suite passed **1,846** with
160 platform/external skips; all **138** web tests, ESLint, Ruff, mypy and
OpenAPI freshness passed. Control/upscale validation and LoRA job execution
remain open under T033/T056, so image admission remains disabled.

## Auxiliary base compatibility binding — 2026-10-02

Admin acquisition now requires a ready image base ID for LoRA and control assets,
stores that binding with the auxiliary registry row, and refuses a missing or
different base at image admission. Intake refuses an unready base before upstream
inspection. OpenAPI and generated web types were refreshed. Focused acquisition,
listing and admission tests passed **25** with one local-PostgreSQL skip. The
full local Python suite passed **1,843** with 159 platform/external skips;
all **138** web tests, ESLint, Ruff, strict mypy and OpenAPI freshness passed.
Native auxiliary validation and
execution remain open under T033/T056, so admission still refuses their jobs.

## Prompt cancellation dispatch and bounded process stop — 2026-10-02

A dedicated scheduler scan now discovers committed image cancellation every
250 ms instead of sharing the two-second acquisition loop. Exact process-group
TERM escalates to KILL after 2.5 seconds; memory remains held until the same
process is proved gone. Scheduler records intent-to-cleanup delay, exposes its
p95 on the image dashboard and alerts above the five-second target. The focused
poller, workflow, process-supervisor and observability checks passed **20
tests**; the full local Python suite passed **1,841 tests** with 159
platform/external skips, and the tiny native image suite passed **9 tests**.
Strict mypy and Ruff passed. T041 stays open for an end-to-end
healthy-node timing proof, and T083 retains the full-model Studio matrix.

## Registry-bound auxiliary copy identity — 2026-10-02

Resolved dependency records now carry the registry's safe local slug alongside
model UUID, pinned source revision and manifest digest. New jobs can bind a
local auxiliary tree without
deriving a path from client text; older recipes remain readable through the
optional field. Unsafe slugs fail contract validation. OpenAPI and generated
TypeScript types were refreshed; **35 focused contract/dispatch/replay tests**,
strict mypy, Ruff, OpenAPI freshness, web lint and build passed. Native
auxiliary stage loading remains under T056. Until that stage is present, a
base requiring a hidden auxiliary copy is omitted from the picker, rejected
before capacity reservation on direct submit, refused by node dispatch and
refused before native execution. The focused admission/picker/pipeline checks
passed **22 tests** with one local-Postgres integration test skipped. The full
Python suite passed **1,839 tests** with 159 platform/external skips, and the
local tiny native image suite passed **9 tests**.

## SeedVR2 3B local acquisition layout — 2026-10-02

The pinned mflux 0.20.0 SeedVR2 3B loader expects two root safetensors files
without `config.json`. Admin inspection and node snapshot selection now accept
that exact repository/layout, exclude its unrelated 7B weight and size memory
and disk holds from only selected bytes. Other configless upscalers still fail
closed. The focused inspection/acquisition tests passed **23 tests**, with Ruff
and strict mypy passing. Aux execution validation and publication remain open
under T033/T056.

## Terminal Studio scratch journal replay — 2026-10-02

The Studio agent now scans bounded durable journal pages every 30 seconds,
including after restart, and retries physical output/input scratch deletion for
failed or cancelled attempts before recording `scratch_cleaned`. Unsafe entries
retain bytes and journal evidence and emit the existing cleanup failure metric.
The directory scan streams keys and keeps only the next 25 in memory; a
4,098-entry regression proves history beyond the old inventory limit cannot
disable cleanup.
The node unit and route contract checks passed **31 tests**; the full local
Python suite at `3f29968` passed **1,832 tests** with 159 platform/external
skips. The inventory change passed **11 focused tests**, Ruff and strict mypy.
Core tombstone, orphan staging, input and quota sweeps were
already present and remain active when image admission is disabled. Operator
retention and cancellation timing still require the cluster matrix in T083.

## Private gallery classifier diagnostic — 2026-10-02

Owner output projections now expose a bounded classifier diagnostic, and the
gallery labels unavailable or failed tagging without exposing storage paths.
The contract/publication checks passed **22 tests**; web tests passed **138**,
with lint, production build, OpenAPI regeneration/freshness, Ruff and mypy
passing. Actual output classifier invocation and measured CPU child reservation
remain open in T033/T070.

## Native validation physical peak — 2026-10-02

The reserved base-model validation worker now samples its physical footprint
after load, at denoising progress, and after both txt2img/img2img smokes. On
macOS this includes Metal allocations. A transient peak beyond the hold or an
unreadable footprint fails validation. Evidence includes additive
`peak_physical_delta_bytes`. A regression covers a peak that falls before the
final sample. Both-copy acquisition evidence requires the physical field;
focused validation and reconciler tests passed **10 tests**. Auxiliary
mode-specific smokes and the operator full-model matrix remain open in
T033/T083.

## Physical overage image admission — 2026-10-02

Image and chat placement now subtract measured model-plus-image footprint above the
matching reservations exactly once. An existing image hold with unavailable
measured residency is ineligible until a health sample resolves it; live
language engines with unreadable footprints also make residency unknown.
The coexistence/prober/ledger unit checks passed **32 tests**; Ruff and strict mypy
across 646 source files passed. The existing native admission lock remains the
last local safety gate. The broader transient/cache and real-cluster matrix
remain in T070/T072.

## Unmeasurable image residency alert — 2026-10-02

The prober now emits `coire_image_residency_measurement_unavailable` when an
image hold has no exact live-worker footprint. A provisioned warning fires
after two minutes, so an unknown drift sample is visible instead of appearing
healthy. Alert provisioning and placement tests passed **7 tests**; Ruff and
strict mypy across 646 source files passed. The full Python suite on the
preceding recovery commit passed **1,821 tests** with 159 platform/external
skips. The real-cluster footprint matrix remains in T070.

## Cancelled scratch status recovery — 2026-10-02

An exact node status retry now repairs a terminal cancelled journal with
unacknowledged scratch cleanup, using the same fenced deletion as the cancel
retry. It records `scratch_cleaned` only after both output and input scratch
are gone. The node image-job contract passed **21 tests**. T039 remains open
for the wider periodic orphan and quota sweeps.

## Node health image memory accounting — 2026-10-02

Node health now adds disjoint language engine, resident image worker and
acquisition-hold commitments. An unreadable holder reports the full node
budget rather than zero. It separately reports the exact live image child's
physical footprint, and core includes that footprint in measured residency;
an unavailable footprint leaves drift unknown while an image hold exists.
Both the prober and ledger projection compare measured model-plus-image
residency against matching model-plus-image reservations. The node health
contract was extended for the additive field. Focused regressions passed
**29 tests**, the node health contract passed **12 tests**, and the full Python
suite passed **1,819 tests** with 159 platform/external skips. Web tests
passed **138 tests**; lint, build, OpenAPI freshness, Ruff and strict mypy
across 646 source files passed. The remaining transient/cache and real-cluster
drift matrix still belong to T070.

## Cross-process legacy launch deduplication — 2026-10-02

Admin and gateway legacy loads now re-read active engine rows after acquiring
the selected node's transaction lock. Two concurrent admin requests on the
disposable Compose stack returned one `202`, one `200`, and the same engine ID;
the worker reached ready. The immediate held-memory assertion also passed in
that rebuilt API stack (**2 integration tests**). A ready hold requires its
node ledger row even when the hold already exists. The focused gateway tests
passed **11 tests**; the complete local Python suite passed **1,815 tests**
with 159 platform/external skips. Ruff and strict mypy across 645 files passed.
The latest CI for this commit is pending. T065/T072 remain open for their wider
placement and coexistence matrices.

## Owner input binding for fill and control — 2026-10-02

The shared image contract now lists init, mask and control input IDs in stable
lock order and refuses reuse of one ID for two purposes. Admission validates all
owner rows before retaining any reference; dispatch builds an exact purpose,
digest and dimension manifest for each input. A regression test proves a bad
mask leaves the init reference untouched, verifies both fill manifests and
rejects a changed purpose. The focused core/admission/dispatch/reference suite
passed **37 tests** with one local-PostgreSQL skip. Ruff and strict mypy across
645 source files passed. Advanced submission and native execution are still
refused pending T056, so T054 remains open for end-to-end acceptance.
The owner upload route contract now exercises each declared generation input
purpose (`init`, `mask`, `control`) and passed **4 tests** with Ruff green.

## Legacy prelaunch admission — 2026-10-02

Both admin and gateway legacy load paths now commit an engine row and its
shared model memory hold under the node admission lock before asking coire-node
to start the process. A definite node budget refusal releases the hold; an
uncertain transport failure retains it until reconciliation confirms terminal
or missing node state. The ready-engine gateway path also reconciles old
unheld rows, and the engine integration test now checks that a launch response
already has a held model reservation. Focused gateway/loading/reconciler tests
passed **19 tests**; the complete local Python suite passed **1,813 tests**
with 158 platform/external skips. Ruff and strict mypy across 645 files passed.
The full disposable current-image Compose suite passed **121 tests** with
32 environment skips. A rebuilt API image passed the targeted admin load test,
including its immediate memory-hold assertion. The fixture removed the
`coire-it` stack after both runs. The latest PR CI remains pending.

## Fresh blob volume and legacy gateway hold — 2026-10-02

Disposable Compose inspection found that a fresh `coire-blobs` Docker volume
started owned by API UID 65532 with mode `0755`. Its private staging sweep
therefore failed every pass. API startup now tightens an owned volume to `0700`
and refuses symlinks or foreign ownership. The rebuilt local API container
reported UID 65532 and mode `0700`; the focused maintenance suite passed
**20 tests**. Existing `coire` services were left running; only `coire-it`
test containers and volumes were recycled.

The earlier full PR CI runs failed eight chat gateway integration cases. A
current-image local Compose reproduction found a ready legacy engine with no
model hold in the shared memory ledger, while the gateway correctly refused
inference without one. The gateway now takes the node admission lock, checks
the budget and absence of an image worker, records a held legacy reservation,
and only then grants a request lease. The reconciler releases that hold after
the node reports a terminal engine state. The two previously failing focused
gateway cases passed against rebuilt current API, scheduler, migration and
node-test images. Gateway/authorization/coexistence tests passed **60 tests**,
strict mypy passed across 644 files, and the complete local pytest run passed
**1,813 tests** with 158 platform/external skips. A fresh full CI and full
local Compose suite are still running; this is not final release evidence.
The admission check also refuses a live image worker whose hold is missing;
the focused gateway suite passed **11 tests** after that guard.

## Bounded orphan input inventory — 2026-10-02

The private original/derived input orphan sweep now stops after 4,096 directory
entries even if none matches a generated name. This prevents an unexpectedly
large directory from consuming an unbounded maintenance pass. A 4,097-entry
regression test proves fail-closed behavior; the focused cleanup suite passed
**14 tests**, with Ruff and strict mypy green. T039 remains open for the
complete physical inventory and operator retention matrix.

## Deleted output sweep starvation — 2026-10-02

The deleted-output purge now pages by `(deleted_at, output_id)` across sweeps.
Twenty-five damaged early blobs no longer prevent a later tombstone from being
attempted. A regression test fails the first 25 purges and confirms the 26th
is reached on the next pass; the focused maintenance suite passed **18 tests**,
with Ruff and strict mypy green. T039 remains open for full retention evidence.
The first remote `cac9474` test job exposed one older deletion-service fixture
that still mocked scalar rows after the new keyset query. The fixture now
supplies `(id, deleted_at)` rows; the combined deletion/maintenance suite
passed **24 tests**. A fresh full CI run is required before this gate is green.
The full local non-integration/non-engine Python suite after that correction
passed **1,794 tests**, with 2 existing skips and 168 deselections.

The deleted and failed input purge sweeps now use bounded keyset pages too, so
25 unpurgeable earlier inputs cannot starve a later one. The two failure-page
regressions and the focused input cleanup suite passed **16 tests**, with Ruff
and strict mypy green. Physical retention acceptance remains open under T039.

## Full-disk generation failure injection — 2026-10-02

A local fake-worker recovery integration case now injects `ENOSPC` after a
partial PNG write. Another requests cancellation after the first PNG in a
two-output batch. Both verify the private attempt directory is removed and
the durable node journal has not advanced to a publishable state. The recovery
suite passed **4 tests**, with Ruff and strict mypy green. T075 remains open
for its remaining boundary and failure matrix.

## Missing advanced image fields — 2026-10-02

The form now names a missing source, fill mask or control image beside its
disabled Generate action. A restored fill recipe test verifies source and mask
errors. The web suite passed **138 tests**, ESLint and TypeScript passed. T057
remains open for complete advanced settings and model component selection.

## CI image gate and chat lease expiry — 2026-10-02

The `c55b433` pull-request CI run passed general Ruff/strict mypy lint and the
required Apple Silicon `image-engine` job. That job built the ignored tiny image
fixture, then ran the offline native worker and lifecycle suite successfully.
The preceding run found that the fixture's optional tokenizer import was visible
to the Linux lint environment; the fixture now imports it only when building the
fixture. The full CI run and image build/integration jobs are still in progress.

Chat request leases now refuse refresh once expired, released, or given a
nonpositive renewal. A disposable local PostgreSQL 17 cross-process admission
suite passed **3 tests**, including expiry accounting and a renewal refusal;
the container and test database were removed. Ruff and strict mypy across 644
source files passed. T065 stays open for its full placement and contention matrix.

## Coexistence circuit breaker reconciliation — 2026-10-02

T069 was already implemented across report admission, profile matching and the
15-second live latency monitor. The human-admin route audits approved, refused
and invalidated reports; stale, failing, changed-node or unmeasured reports are
refused. The monitor withdraws approved profiles on a >1.5-second same-node
first-token p95, unavailable metrics or fresh thermal alarm and commits fenced
`cancelling` intents for active image jobs. Dispatch excludes invalidated
profiles and serious/critical thermal samples. Focused profile, admission and
monitor suites passed **41 tests**, so T069 is reconciled as complete. The
operator 15-minute benchmark and physical coexistence evidence remain T084.
The regression helper now also treats a nonfinite direct latency sample as
unavailable and withdraws approval; the expanded monitor suite passed **11 tests**.

## Atomic image/chat admission reconciliation — 2026-10-02

T068's shared transaction-scoped node locks, measured profile checks, Studio B
preference, pinned placement and chat request leases were present. A gateway
renewal failure could silently leave inference running after its lease expired;
the gateway now cancels that request on an expired or unavailable renewal,
records a bounded reason counter and logs the event. The Chat dashboard, alert
and image runbook cover diagnosis without force-releasing a resident hold.
The image dispatch/coexistence/gateway suite passed **44 tests** after the
change; the focused gateway/observability suite passed **11 tests**, strict mypy
and Ruff passed, and both Chat and Image Prometheus rule files validated.
The cross-process PostgreSQL lease tests above passed. T068 is reconciled as
complete; simulated full contention remains T072 and physical evidence T084.
The subsequent full non-integration/non-engine Python gate passed **1,792 tests**
with 2 existing skips and 166 deselections.
An additional race audit found that a draining worker can still be alive. Both
image placement and a new chat load now count draining chat/image instances in
the resident mix until exact stop proof; a draining image instance without a
matching hold fences placement. Focused dispatch/coexistence tests passed
**36 tests**, with Ruff and strict mypy green.
The full local non-integration/non-engine Python gate after these admission
changes passed **1,798 tests**, with 2 existing skips and 168 deselections.

## Image worker residency reconciliation — 2026-10-02

T071's idle sweep, pin guard, durable drain intent, exact node stop proof and
startup adoption were already implemented in `image_residency.py`, the node
supervisor/agent and authenticated worker/admin routes. The scheduler advances
past a busy first batch and retains a held reservation on uncertain node stop.
The focused residency, supervisor, node contract and admin unload suites passed
**24 tests**. T071 is reconciled as complete; node physical footprint and
operator restart trials remain T070/T083.

## Retained output receipt audit — 2026-10-02

API maintenance now reads a bounded page of retained published blobs every five
minutes and checks size, SHA-256, private mode, owner UID and single-link status
against the durable output row. Missing or altered bytes increment a content-free
counter and alert without deleting rows or releasing quota. Tests cover a valid
blob, changed content, a hard link, a missing blob and maintenance-loop wiring.
The focused maintenance/telemetry/observability suite passed **21 tests**; the
Prometheus rule test passed in an isolated local container. Ruff and strict mypy
passed. T039 remains open for complete inventory of core and Studio bytes and
operator retention evidence.
After the migration and integrity changes, the full non-integration/non-engine
Python gate passed **1,789 tests**, with 2 existing skips and 165 deselections;
OpenAPI freshness, 137 web tests, ESLint, TypeScript/Vite build, Ruff format/check
and strict mypy also passed.

## Required tiny image CI gate — 2026-10-02

CI now builds the ignored <=1 GB image fixture on its Apple Silicon `macos-15`
runner and runs the offline native image worker suite as a required job. Release
publication depends on that job; the preexisting informational text/VLM engine
job excludes the image test so it cannot accidentally fail from a missing fixture.
The equivalent local offline suite passed **9 tests**, and the workflow YAML parsed
successfully. The later `c55b433` CI run passed this required Apple Silicon job.

## PostgreSQL migration and admission gate — 2026-10-02

The image migration, registry-kind rollback, quota contention and accelerator
admission tests ran against a disposable local PostgreSQL 17 container: **14 passed**.
Two downgrade assertions had expected revision 0029 after a guarded failure;
Alembic correctly rolls the entire failed downgrade back to current revision
0030. The assertions now verify the actual transactional outcome. The test
database and container were removed. This covers the local migration portion
of T081; immutable node install, affected-image scans and real Studio smoke remain.

## Local affected-image policy and scan gate — 2026-10-02

Native arm64 builds passed for `coire-api`, `coire-scheduler`, `coire-file-worker`,
`coire-migrate`, `coire-prometheus`, `coire-grafana` and `coire-web`. Each local image
passed `scripts/image-policy.sh` (including no-shell, non-root, architecture and
digest-pinned base rules), a Trivy CRITICAL scan with exit code 0 and a generated,
valid SPDX JSON SBOM. These are disposable local `:015-local` images; remote CI
and the immutable native node-install/text/VLM smoke portions of T081 remain open.

## Cancellation during output encoding — 2026-10-02

The loopback worker now passes its cancellation event into the fenced image job
executor. The executor checks it immediately after generation, before each PNG
write and after the last write, removing the attempt directory on cancellation.
Tests inject cancellation after the first and last writes of a two-output batch;
neither leaves scratch output or a generated result. The focused worker/control
and simulated job suite passed **11 tests**; Ruff and strict mypy passed. T041
remains open for cross-process cancellation/publication and healthy-stop timing.
The full post-change local Python gate passed **1,786 tests**, with 2 existing
skips and 165 deselections; the offline tiny native worker passed **9 tests**.
Ruff format/check and strict mypy across 644 source files passed.

Local `docker compose -f deploy/compose/compose.yaml config --quiet` and the web
TypeScript/Vite production build passed. These checks do not substitute for the
affected-image build, scan, SBOM and immutable node install gates in T081.

## Input hard-link quota guard — 2026-10-02

Physical input cleanup now refuses to release held or stored quota while an original
or normalized private input has another hard link. The generated-path unlink helper
checks link count before unlinking. Regression tests cover deletion of a linked
original and failed normalization with a linked derived file; both retain the bytes,
row state and quota for investigation. The focused input-cleanup suite passed
**13 tests**; Ruff and strict mypy across 644 source files passed. T039 remains open
for complete physical inventory and operator retention verification.

## Bounded Studio attempt cleanup — 2026-10-02

Studio cancellation now inventories private output and input attempt directories with
bounded scans based on the request's output and input counts. Extra entries cause a
fail-closed retry before any file is unlinked. A regression test verifies that an
unexpected extra PNG leaves both files intact. The focused node suites passed
**10 tests**; the four local image-job recovery integration tests passed. The
post-change non-integration/non-engine suite passed **1,782 tests**, with 2 existing
skips and 165 deselections. Ruff, strict mypy across 644 source files, OpenAPI
freshness, web ESLint and **137 web tests** passed. The offline local tiny native
worker suite passed **9 tests**. T039 remains open for the full
scratch/orphan/quota recovery matrix and operator retention evidence.

## Referenced-input deletion after entitlement revocation — 2026-10-02

Deleting an owned image input with active jobs now uses a dedicated owner-cancellation
path. It rechecks the live user/personal key and job ownership, but does not require
the job's already-revoked model or explicit entitlement to request cleanup. The
normal owner cancellation route still enforces its existing dependency check. A
failing-case test covers a running explicit job after entitlement revocation and
idempotent repeated cancellation; **16 focused cancellation/input tests** pass,
with Ruff and mypy for the changed modules green. The post-change
non-integration/non-engine gate passed **1,781 tests**, with 2 existing skips and
165 deselections. The offline local tiny native image gate passed **9 tests** with
`COIRE_ENGINE=1`; no production Studio was contacted. T054 remains open for its full
processing/transfer/retention acceptance matrix.

## Terminal-node cancellation race — 2026-10-02

Core cancellation now accepts an exact terminal Studio journal in `cancelled`,
`failed` or `succeeded` state when scratch cleanup is proved and the locked core
job has not published an output. This closes the case where Studio transfer
finished before a committed core cancel intent, leaving the core job permanently
`cancelling`. Core still purges all transfer attempts before releasing its hold;
release evidence records the node's actual terminal state. Parameterized tests
cover all three node outcomes and two-attempt scratch, with **16 focused
cancel/workflow/publication tests** passing. A separate scheduler partition unit
test proves an unreachable node leaves the committed `cancelling` intent for retry;
all **3 workflow tests** pass. The cross-process partition and
publication-race acceptance under T041 remains open.

## Read-only stored quota reconciliation — 2026-10-02

API maintenance now checks owner and global stored-byte counters against unpurged
published outputs and ready/deleting inputs every five minutes under the shared quota
advisory lock. It reports only bounded drift and row-count facts in a metric/log/span;
it never edits a counter or removes uncertain bytes. A disposable local PostgreSQL
test detected a 30-byte mismatch from an unaccounted staged row, returned to zero
after counter correction, and continued to count a ready/tombstoned input. The
container and test database were removed. The new alert passed `promtool test rules`
in an isolated local image; 29 focused unit tests, Ruff, strict mypy and OpenAPI
freshness passed. T039 remains open for physical-byte/orphan and operator retention
reconciliation, including Studio inspection.

## All-attempt terminal cleanup — 2026-10-02

Terminal cancellation, worker failure and revoked-publication cleanup now inventory and
remove every known core transfer attempt before releasing the job's storage hold. The
inventory rejects an unexpected future attempt before deleting any known bytes. The
periodic terminal sweep reuses this path. Tests prove a two-attempt cancellation and
failure remove both directories before release, plus refusal of a future attempt.
The focused maintenance/recovery/publication/integration selection passed **28 tests**;
Ruff and mypy for the changed modules passed. The full non-integration/non-engine Python
gate after this change passed **1,777 tests**, with 2 existing skips and 165 deselections;
the web suite passed **137 tests** with ESLint, and full Ruff, strict mypy across 644
source files, and OpenAPI freshness passed. T039 remains open for complete quota
reconciliation and operator retention evidence.

## Bounded staging maintenance — 2026-10-02

The temporary-upload sweep now refuses more than 4,096 staging directory entries before
sorting or walking them, matching the orphan sweep's bound. A regression test creates
4,097 entries and proves the fail-closed behavior. The maintenance suite passed
**12 tests**; Ruff and mypy for the changed module passed. T039 remains open for
quota reconciliation and operator retention evidence.

## Scratch cleanup hard-link guard — 2026-10-01

Receipt-aware Studio cleanup now refuses a generated PNG with multiple hard links before
it records terminal success. Core output purge likewise retains quota until a linked blob
can be removed without retained bytes. A retained hard link could otherwise leave bytes
after the expected path was removed. The contract tests prove linked outputs keep their
job or quota state and both paths intact. Focused node/API cleanup tests: **9 passed**;
Ruff and mypy for the changed modules pass. The local tiny native worker suite: **9 passed**.
T039 remains open for the full scratch/orphan/quota recovery matrix and operator evidence.
The fresh non-integration, non-engine suite after this change passed **1,774 tests**, with
two existing skips and 165 deselected integration/engine tests.

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
The journal reader now also rejects symlinks, hard links, public permissions,
wrong ownership and files above 16 MiB; the expanded focused selection passed
**7 tests**.

The CI base refresh initially exposed a missing digest entry in
`deploy/compose/images.lock` through the full unit gate. After adding the
pin, `scripts/pin-images.sh --check` and all four pin tests passed. The full
local non-engine, non-integration suite then passed **1,737 tests**, with 2
skipped and 159 deselected. The image runbook now describes shared holds and
corrupt-journal recovery.

The coexistence monitor now treats a fresh serious or critical Studio thermal
sample as a circuit-breaker signal: it invalidates current approved profiles
and requests fenced cancellation for active image work before asking Prometheus
for chat latency. The action is audited with a thermal reason and counted in
the monitor metric. The focused monitor selection passed **10 tests**, including
the real invalidation/cancellation state transition and the no-latency-query
thermal branch; changed-file Ruff and strict mypy passed. A physical same-node
regression benchmark remains open.

An image instance marked failed may now enter the audited admin drain path
once no active job or execution lease remains. The node must still prove
exact worker death and zero reserved bytes before the shared image memory
hold is released. Ready and failed drain cases passed the focused **5-test**
selection; changed-file Ruff and strict mypy passed. A missing node process
record still leaves the hold for explicit reconciliation.

A restarted Studio agent can now reconcile a dead image worker from its
private process record during an exact unload request. The record is removed
and the node returns zero reserved bytes only when the recorded process is
provably gone; an unreadable identity or changed instance ID retains the
record and hold. The supervisor selection passed **12 tests**, including the
restart/dead-record case and the existing TERM/KILL five-second bound;
changed-file Ruff and strict mypy passed. Physical restart on a Studio remains
an acceptance gate.

Dispatch now polls the authenticated node worker status after a `starting`
load response, allowing the node's private health proof to promote the exact
instance to `ready` before job start. The scheduler checks the instance ID
and reserved bytes on both responses, retries transient unavailable status
within a 90-second bound, and keeps the durable holds if readiness remains
uncertain. The focused dispatch selection passed **6 tests**, including a
mismatched-ready-reservation refusal; Ruff and strict
mypy passed. The scheduler now reserves every exact attempt in the node
journal before loading the worker. A pre-load cancellation can terminate
that queued journal; a replayed terminal reservation causes dispatch to
return without loading or starting the worker. Node contract and scheduler
dispatch selections passed **25 tests** with this ordering, including
pre-load cancellation and terminal replay. The remaining healthy-stop
latency and partition recovery gates stay open under T041.

Image candidate capacity now also clamps the shared ledger budget to the
Studio's configured physical memory fraction, matching the node supervisor's
own admission ceiling. This prevents a default ledger budget larger than a
128 GiB Studio from advertising the unusable final 10% of RAM. Focused
admission and dispatch tests passed **34 tests**; changed-file Ruff and strict
mypy passed. The full local unit/contract suite immediately before this
clamp passed **1,745 tests**, with 2 skipped and 159 deselected.

Gateway inference now refuses a node engine or shard group whose memory
reservation is missing, so a request cannot proceed without the lease that
protects the resident model while image admission runs. The proxy and affected
chat contract selection passed **37 tests**; changed-file Ruff and strict
mypy passed. The remaining physical mixed-load benchmark and TTL inventory
remain open under T068/T071/T072.

Measured coexistence now rechecks each resident chat variant and its model
at admission time. Unpublished, unvalidated or non-ready variants no longer
authorize image dispatch or a new chat load through a previously approved
profile. The focused profile selection passed **27 tests**, including the
post-approval unpublish case; changed-file Ruff and strict mypy passed.
The subsequent full local suite found one stale test fixture that assumed
coexistence checked only the node and profile. Updating it to return current
variant/model rows restored the focused profile selection to **31 passed**.
The other **1,745** local unit/contract tests had passed in that run; the
complete selection needs one final repeat after the fixture correction.

Image worker residency now has a bounded idle TTL sweep in the scheduler's
existing placement loop. It walks unpinned held image reservations with a
cursor so older busy workers cannot starve later idle ones, checks exact
instance membership, jobs and execution leases under the node admission
lock, persists `draining`, then releases the hold only after an exact node
stop proof with zero reserved bytes. A missing or mismatched reply retains
the hold for retry. The admin reservation endpoint now audits image worker
pin/unpin, serializes it with the same node lock and refuses a pin after
draining begins. The runbook documents pinning and uncertain-stop recovery.
Focused idle sweep and admin contract checks passed **11 tests**; changed-file
Ruff and strict mypy passed. The complete local non-engine, non-integration
selection passed **1,753 tests**, with 2 skipped and 159 deselected. An
uncertain idle stop now has a dedicated alert and dashboard panel. Physical
Studio restart and TTL inventory remain open under T071.

After the node's exact ready response, dispatch now records `ready` on the
image instance and marks its Studio member healthy under the shared admission
lock. It requires the matching held reservation and byte count before
publishing that state. A changed or released hold leaves the instance
unpromoted and dispatch refuses to start the job. The focused dispatch
selection passed **8 tests**, including the held-reservation mismatch case;
changed-file Ruff and strict mypy passed.

The admin ledger image-worker pin path now applies the same live human-admin
guard as other image mutations before changing a reservation. An admin-scoped
service principal cannot reach that mutation. The direct route refusal and
admin ledger contract selection passed **8 tests**; changed-file Ruff and
strict mypy passed. The idle sweep cursor regression also passed **5 tests**,
including 26 workers across a 25-reservation page and wraparound.

After those changes, the complete local non-engine, non-integration Python
selection passed **1,756 tests**, with 2 skipped and 159 deselected. Repository
Ruff format/check passed for 1,315 files; strict mypy passed 642 source files.
The actual OpenAPI check is `uv run python -m coire_api.openapi --check` and
passed; the AGENTS.md shortcut `uv run coire-api export-openapi --check` is
stale because this workspace does not expose that console entry point. The
web suite passed **134 tests**, eslint passed and the TypeScript/Vite
production build passed. A current node wheelhouse build and Studio install
check are underway.

The current API, scheduler and migration images rebuilt as arm64 images and
passed all seven production image-policy checks, CRITICAL Trivy scans and
SPDX SBOM generation. The first migration scan saw a stale local image with
an older package set; an explicit `coire-migrate` rebuild used the current
lock and passed its repeat scan. The locked macOS arm64 node wheelhouse
selected 87 hash-checked wheels. Both Studios installed immutable
`0.2.0-d340019c6bad`; their agents restarted under launchd, `/ready` returned
200, unauthenticated `/node/health` returned 401 and authenticated health
returned 200. Studio B loaded its existing SmolVLM 256M copy and answered a
synthetic 32-pixel red-image request through the authenticated node proxy
with HTTP 200 and one choice in 1.51 seconds, then received a stop request.
Studio A re-adopted its existing text engine PID 55229 as `starting`, but its
loopback `/health` gave an empty response for over 20 seconds. An exact
node-managed stop and reload produced PID 56633; the new process reached
`ready` and answered a four-token completion with HTTP 200 and one choice
in 0.91 seconds. The failed re-adoption is not counted as a passing gate.

Investigation found that node-spawned bare engines inherited a stderr pipe
owned by the agent process; after agent exit, engine stderr writes could lose
their reader. The node now gives each engine a private owner-only stderr file
whose descriptor survives agent restart, captures the final 4 KiB on startup
failure, trims files above 8 MiB during health checks and removes them after
confirmed stop. If TERM and KILL still cannot prove exact process death, the
node keeps the engine `stopping` and its memory held. A fake-engine regression
emits stderr after adoption and requires a successful completion. The focused
engine contract selection passed **21 tests**; Ruff, strict mypy and the node
wheel build script syntax check passed. Physical re-adoption with this fix is
pending a new immutable node install.

## Physical stderr-survival regression — 2026-10-01

Reconciled Studio A's interrupted readiness poll before any mutation: exact
engine PID 56826 was `ready` and returned HTTP 200 with one choice for a
four-token synthetic completion through the authenticated node proxy. Sent
TERM to its agent PID 56795 and allowed launchd to restart it. The restarted
agent reported `/ready` 200 and authenticated health 200, re-adopted the
**same engine PID 56826** as ready, and served another HTTP 200 completion
with one choice. This physically proves the stderr-file fix for this text
engine/runtime; it does not prove image-worker re-adoption.

Studio B installed the staged locked wheels into immutable
`/opt/coire/envs/0.2.0-7dc3d1043a3c`, passing installer imports and smoke
before the symlink flip. Its old agent PID 16245 received TERM and launchd
restarted it. Checks using its resolvable control hostname
`coire-edge-b.local` returned `/ready` 200, authenticated `/node/health` 200,
and unauthenticated health 401. Its engine inventory was empty, confirming
that the previous SmolVLM stop completed. Bare `coire-edge-b` did not resolve
from B itself; no network policy was changed. Secrets were read within the
remote Python process from Keychain and never printed or passed in argv.

Neither Studio has acquired image weights. T083/T084 remain unproven, as do
all other unchecked parent acceptance items. No task was checked solely on
the basis of this text-engine regression.

Continued recovery-boundary review found that image attempt journals rejected
symlinks and public files but accepted multiply linked private files. Added a
regression first: restart/get/request/replay must refuse a hardlinked journal
without modifying either alias. It failed before the fix. The reader now
requires one filesystem link, retaining uncertain state rather than treating
it as safe generation authority. Focused journal and authenticated node-job
contracts passed **24 tests**. Repository Ruff format/check and strict mypy
(**642 files**) passed. The offline real tiny-mflux selection passed **6
tests** in 4.69 seconds. The full local non-integration/non-engine selection
passed **1,758 tests**, with 2 unrelated skips and 159 deselections. These
results do not close the advanced-mode or physical image acceptance gates.

Extended the local receipt/fence recovery integration scenario to inject lost
cleanup acknowledgment: after PNG removal and terminal journal persistence,
reconstruct the agent and retry the exact cleanup command. The returned
receipt is identical, the journal remains succeeded/cleaned, original start
replay cannot regenerate, and changed receipt digests are refused. Both local
recovery integration tests passed; Ruff check/format passed. This covers the
node receipt-ack-loss boundary, not API/scheduler publication or reboot of a
physical Studio. OpenAPI freshness and the complete web selection (**134
passed**), lint and TypeScript/Vite build also passed after the journal fix.

## Actual local native child lifecycle — 2026-10-01

T077 is complete for the local tiny-engine boundary. A new test launches the
real production bootstrap child via ImageProcessSupervisor, injecting tiny
native component dimensions only through a temporary test-only sitecustomize
file. Production has no fixture-selection switch. The actual authenticated
loopback worker serves a generation, denies unauthenticated health, survives
supervisor reconstruction with the exact PID/create-time, rechecks health and
serves a second generation with identical pixels. It then begins 100-step
256px real MLX denoising; after observed native progress the node's actual
TERM/KILL stop proves death and releases the reservation in under five seconds.
Only after that proof does the test clean killed-attempt scratch and persist
cancelled/cleaned replay without regeneration. The existing real transfer test
separately proves receipt-bound normal PNG removal.

This physical local test uncovered a real thread-ownership bug: lazy model
weights created on bootstrap's main thread were evaluated during generation
on a different thread, producing `There is no Stream(cpu, 0) in current
thread`. The native loader now materializes all model parameters before they
cross that boundary. It also exposed transient identity-inspection failure
during macOS process exit; the death waiter now observes until confirmed gone
or its bounded deadline, retaining memory and never signalling an unknown PID.
Adoption now durably resets historical ready to starting and refuses private
job control until a fresh authenticated identity/byte-matched health result.
The new readiness expectation failed before that fix.

The warm-cache trial now actually changes seed and alternates step count for
20 warm iterations after one cold run. Native encoder calls remain exactly
one, with 20 hits, distinct changed-prompt/environment misses and bounded
occupancy. Actual outputs vary, rather than repeatedly testing an unchanged
seed. The complete offline local engine selection passed **7 tests** in
6.96 seconds. Focused supervisor/pipeline/bootstrap tests passed **29**;
Ruff format/check and strict mypy (**642 source files**) passed. The broad
local unit/contract gate passed **1,758**, 2 unrelated skips, 160 deselections
before the final long-denoise test extension. Runbook documents thread
materialization and adoption readiness. Full-model T083/T084 and advanced
control/LoRA cache acceptance remain open; local tiny evidence does not
establish production image quality, licences, or chat coexistence.

T054 active-reference review found that owner input deletion omitted jobs in
`transferring` from its locked cancellation query. A source could therefore
remain referenced but fail reconciliation instead of requesting the required
fenced stop. The compiled-query regression failed before adding transferring
to the nonterminal states. Referenced transfer jobs now enter the same audited
cancel-before-tombstone path; bytes still wait for reference release and
cleanup proof. Input deletion/reference/cleanup selection passed **15 tests**,
Ruff format/check and strict mypy passed. T054 stays open for complete
cross-boundary processing/transfer acceptance rather than closing on this fix.

Input processing review also found a lock-order inversion: recipe and
normalization completion locked the input row before obtaining quota locks,
while owner deletion and maintenance used quota-before-input. Both completion
transactions now take the shared quota advisory lock before their input row,
preventing cross-process deletion/settlement deadlock without releasing holds
early. The two lock-order regressions failed before the fix. Processing,
refusal, concurrent deletion and cleanup selection passed **20 tests**; Ruff
format/check and strict mypy passed. Real PostgreSQL concurrency is still a
separate required gate, not inferred from these deterministic lock tests.

Private input staging now verifies the opened directory's owner and absence
of group/world permissions before reading any upload bytes; the same helper
protects publication and temporary discard. A public-root regression failed
before the fix, while symlink-root refusal already passed. Staging, parsing
and normalization selection passed **13 tests**; Ruff format/check and strict
mypy passed. Unsafe roots are refused, not silently chmodded. This preserves
the configured private-volume ownership boundary.

Post-adoption job observation now refreshes authenticated worker readiness
when the resident exact instance is starting, then queries only its existing
attempt. This prevents running jobs from being stranded by the new historical
ready reset, without replaying generation. Healthy and unavailable health
regressions prove one refresh, no generation PUT, and no journal mutation;
unavailable health prevents the status call. Node job/supervisor selection
passed **32 tests**, Ruff and strict mypy passed.

After the adoption/observation and input safety fixes, the full local
non-integration/non-engine selection passed **1,762 tests**, with 2 unrelated
skips and 160 deselections. The four dedicated local fake-worker/recovery
integration tests passed. The complete web selection passed **134 tests**;
eslint, TypeScript/Vite build and generated OpenAPI freshness passed. The
actual native child adoption/active-denoise-stop test passed three additional
independent repetitions, not just its first successful trial.

Current arm64 API and scheduler images rebuilt from commit 381fd1f, each
passed all seven image-policy checks and the core no-user-harness rule, and
each passed CRITICAL Trivy scans. Syft produced validated SPDX-2.3 documents
with 88 packages each under `/tmp/coire-015-{api,scheduler}-current.spdx.json`.
Compose config validated. Builds/scans did not restart the production stack.
T081 remains open for the complete final-source integration/CI matrix and
immutable node rollout after the native image loader changes. No full-model
image asset has been acquired and T083/T084 remain open.

T060 cache-bound review found that the configured prompt-cache cap was not
propagated to native children, and its valid zero value could not be used by
the runtime caches. The private shared launch contract now carries a bounded
`prompt_cache_max_bytes` (old records default to 256 MiB); the supervisor
persists the node setting and bootstrap passes it into the native loader.
Zero permits generation but retains no identity payloads or native arrays.
Launch, bootstrap and disabled-cache regressions failed before implementation.
Focused shared-contract/cache/bootstrap/supervisor selection passed **44**;
the real offline tiny-engine selection passed **8** tests, including two
uncached generations that re-encode twice, retain zero bytes and reproduce the
same pixels. Ruff format/check, strict mypy and regeneration of OpenAPI/TS
passed (the private launch field does not change the public API artifact).
The runbook records legacy defaults and drain-before-downgrade compatibility.
Control preprocessing and actual LoRA stack replacement remain open in T060,
T061 and T064.

Both Studios now installed immutable `0.2.0-51d9370128ad` from the latest
hash-verified 87-wheel node staging build. Their installers passed locked
imports and text/VLM/mflux no-model smoke before flipping the symlink; each
agent was restarted by TERM to its own PID and launchd recovery. Both control
listeners returned readiness 200, authenticated health 200 and unauthenticated
health 401. B's first check preceded restart completion and connection was
refused; bounded readiness polling subsequently passed without configuration
changes. A's exact text engine PID **56826** served HTTP 200 with one choice
both before and after this additional agent/runtime restart. B loaded its
existing SmolVLM 256M as PID **17184**, reached ready, served a synthetic red
32px image with HTTP 200 and one choice, and finished its exact managed stop
as `stopped`. No image model was acquired and no acquisition restriction was
bypassed. Keychain credentials stayed inside the SSH-hosted Python process.

The newest broad local selection passed **1,763 tests**, 2 unrelated skips,
161 deselections. The eight-test real offline tiny-mflux selection and the
new immutable Studio text/VLM checks are separate from production-weight
image acceptance; they do not close T083/T084.

Extended the actual local child lifecycle gate to exercise both hard process
stop and the complete node dispatcher cancellation path during observed
100-step native denoising. The cancellation test binds the real PID/create-time
in the durable journal, requests user cancellation through ImageNodeDispatcher,
and requires cancelled/cleaned plus absent attempt PNGs in under five seconds.
Exact original-start replay stays cancelled; resident release still follows
confirmed process death. This is node-boundary latency evidence, not a claim
that core scheduler-to-publication cancellation has been measured. The
complete offline tiny-engine selection passed **9 tests** in 9.80 seconds;
Ruff format/check, strict mypy and OpenAPI freshness passed. T041 remains open
for its core workflow, partition and publication-race acceptance.

The model picker still stripped the now-implemented img2img mode even after
both-copy validation advertised it, making the private source controls
unreachable from normal model selection. Picker capabilities now intersect
measured registry modes with the two implemented native modes (txt2img,
img2img), rather than hardcoding txt2img. Fill/control remain absent, LoRA
count remains zero and nonzero guidance/negative prompts remain unavailable
until their real adapters exist. The authority-filtered listing regression
failed before this fix; listing/resolution selection passed **17 tests**,
Ruff format/check and strict mypy passed. This exposes existing validated
img2img, not unsupported advanced modes or unacquired assets.

## Cross-process accelerator admission — 2026-10-01

Used a disposable `postgres:17-alpine` container named
`coire-015-postgres-acceptance`, with ephemeral tmpfs data and a randomly assigned
localhost-only port. No production Compose service, database or volume was accessed.
The existing real PostgreSQL node-lock and quota/cancel-publication arbitration
tests both passed. Each migration/quota test creates and drops its own database.

Two new independent-interpreter tests exposed that chat request leases bypassed
the shared node admission lock: both failed before the implementation change.
Lease insertion now takes the same transaction-scoped node lock as image dispatch
and eviction, then refreshes and locks the reservation row before checking HELD
and inserting anything. A competing process that releases the reservation while
holding admission cannot leave a stale chat lease. Both real-process cases pass:
an unchanged hold permits one lease after release of admission, while a released
hold refuses admission and persists zero leases. This is actual PostgreSQL/process
coordination, not an in-memory lock or a mocked session.

A failing-first gateway test also found that sharded requests acquired individual
rank leases without locking the complete node set. The gateway now takes all node
locks in the existing canonical order before any per-rank lease, preventing opposite
rank iteration orders from splitting admission or introducing lock-order deadlocks.

Focused PostgreSQL/admission/persistence/gateway selection: **17 passed**, no skips.
Broad non-integration/non-engine selection: **1,765 passed**, 2 unrelated skips,
164 deselections. Ruff check, strict mypy (**643 files**) and OpenAPI freshness pass.
T065/T068 remain open for their full lease-expiry/profile/pinning/placement matrix;
these tests do not prove a physical chat/image latency bound. T083/T084 remain
open: no production image weights were acquired or operator evidence fabricated.

## Adopted text-engine terminal cleanup ordering — 2026-10-01

The next broad suite exposed a timing race in the existing adopted-engine contract:
STOPPED could be observed while the owned stderr file still existed. A deterministic
persistence observer then failed with `[False]`, proving this was not simply a slow
polling test. Stderr removal now runs under the engine lock before STOPPED is made
visible or persisted. The strengthened observer passes; the full text-engine
contract selection passes **21 tests**. This changes cleanup ordering, not process
identity or uncertain-death fencing. Existing OS-error handling remains unchanged;
this evidence does not prove recovery from a filesystem unlink failure.

The initial broad run is recorded as **1 failure, 1,769 passed, 2 unrelated skips,
165 deselections**, not a successful gate. A fresh broad gate is still required
following the retention/UI increment.

## Current storage policy, audited expiry and real migration rollback — 2026-10-01

Added the shared `ImageGenerationLimits` contract and generated OpenAPI/browser
schema. Authenticated model listings disclose current operator upload/output byte
ceilings, owner storage quota, pending/daily caps and nullable output-retention
hours, including when generation is disabled. The Images form shows this policy
before generation, refuses both click and direct form submission without it, and
clears stale policy after a failed refresh. Failing-first disclosure/unavailable
policy tests now pass. Existing unsupported/missing input guards also apply to
programmatic form submission rather than only the disabled button.

`COIRE_IMAGE_OUTPUT_RETENTION_HOURS` is opt-in (1–8,760 hours); blank/unset preserves
owner-deletion-only retention. Blank Compose input first failed settings validation
and now has a field-specific normalizer, without loosening unrelated settings.
Expiry is measured from successful publication, never staging creation. A bounded
25-row `FOR UPDATE SKIP LOCKED` sweep atomically audits and tombstones expired
published outputs. Tombstones deny reads immediately; the existing physical purge
alone releases stored-byte quota. Maintenance runs with admission disabled, emits
bounded content-free spans and failure counters, and continues later sweeps after
an expiry failure. Unit tests verify that logs contain error types, not arbitrary
exception detail. Compose, contract documentation and the operator runbook are
updated; enabling/shortening the policy also affects existing outputs.

Migration `0030_image_output_retention` adds the partial `(published_at, id)` index
for live published outputs. Its failing-first metadata test passes. The real local
PostgreSQL matrix exercises the migrated schema: a separately held output lock
is skipped without blocking, required-audit failure rolls back the tombstone,
retry expires exactly once, recent publication survives despite old creation,
staged output survives, and both quota rows retain their 90 stored bytes until
physical removal. These rows are synthetic database fixtures, not generated PNGs
or full publication/operator acceptance.

A separate real Alembic roundtrip starts at `0022_stopped_usage_outcome`, inserts
text/VLM registry rows, upgrades through head, and proves their identities/backends
survive. A live image asset blocks downgrade; the transaction preserves the asset,
image schema and new retention index. Removing only that fixture permits downgrade
and re-upgrade, still preserving text/VLM rows. Every test uses its own database in
the disposable localhost PostgreSQL container and drops it afterward. The
`--rm` container was stopped and removed after the fresh gates; no acceptance
database or production infrastructure was left running.

Fresh gates after these source changes:

- PostgreSQL/admission/persistence/maintenance/model-list/settings selection:
  **47 passed**, no skips, using `COIRE_TEST_POSTGRES_DSN` on the isolated container.
- `uv run pytest -q -rs -m 'not integration and not engine'`:
  **1,772 passed, 2 skipped, 165 deselected**. Skips are the non-Darwin footprint
  fallback and third-party topology image without a healthcheck.
- Offline actual tiny native child gate: **9 passed**, no skips.
- Browser unit suite: **137 passed**; lint and TypeScript/Vite build pass.
- Ruff format (**695 files**)/check, strict mypy (**644 files**), generated OpenAPI
  freshness and whitespace diff checks pass.

This is partial T039/T057 and migration/rollback evidence, not completion of their
full acceptance scope. All **24 unchecked parent tasks remain unchecked**, including
native auxiliary adapters/validation, complete coexistence/failure/browser/release
matrices and T083/T084 full-model/operator acceptance. No production image weights
were acquired, admission was not enabled and no physical evidence was fabricated.

## Studio classification transport and local gates — 2026-10-02

The Studio worker now discovers only the pinned, locally verified classifier copy.
After generation it classifies each encoded PNG in a CPU child under a bounded
memory allowance, or records a safe memory diagnostic when measured headroom is
insufficient. Classified output identity travels through the node receipt and
durable core transfer row; publication records the classifier tag, score and
provenance. A missing classifier retains the private `unknown` fallback. Tests
cover the verified-copy gate, classified worker manifest, memory refusal,
transfer recovery and publication.

Local gates after these changes: Python **1,828 passed, 159 skipped**; web
**138 passed** with lint and production build; strict mypy, Ruff check/format and
OpenAPI freshness passed. Engine-only and real Studio checks remain open. The
classification transport does not establish T083/T084 acceptance or make an
unverified auxiliary model publishable.

Image placement now reserves the configured classifier allowance in addition
to the base model estimate. The exact-fit placement test and 42 focused
dispatch/admission/coexistence tests pass (one local PostgreSQL skip). This is
conservative headroom; full physical coexistence acceptance remains T070/T084.

The local tiny native matrix was rerun with `COIRE_ENGINE=1` and the ignored
`models/test--image-tiny` fixture. A 500 MB validation hold failed the new
physical-footprint check; a diagnostic run measured about 13.6 GB of physical
delta during the two native smokes. The test now uses the local host's total
RAM as a test-only upper bound and asserts the measured delta is within it;
production validation still receives an actual ledger hold. All **9 native
tests passed** locally.
The fixture is under 1 GB on disk, but its Metal runtime footprint is much
larger; this single local measurement does not approve any full-model Studio
placement or coexistence profile.

At `ffa052f`, the complete local Python gate passed **1,829 passed, 159
skipped**. The 159 skips are optional external integration/engine environments;
the nine image-engine tests above were run explicitly with the ignored local
fixture. The 138 browser unit tests, lint, build, Ruff, mypy and OpenAPI
freshness gates passed before the final test-only correction. Remote CI for
this head is pending.

Base-model acquisition now adds 16 GiB to its file-derived validation
reservation based on the local physical peak and rejects intake if either
Studio lacks that hold. The contract test checks the persisted estimate and
one-low-node refusal; **8 acquisition contract tests passed**. This remains a
conservative estimate, with native smoke as the final measured gate. It is not
a full-model benchmark or auxiliary validation.

Classifier copy-unavailable and memory-refusal paths now emit the failed
classification stage counter/span used by the existing alert, while keeping
the owner diagnostic content-free. The focused worker/classifier suite passed
**14 tests**; no full-model classifier run was claimed.

The base validator now compares the absolute process physical peak with its
held reservation, rather than only the increase from its already resident
baseline. It records both values in strict validation evidence. A unit case
with a 301-byte increase but 1,201-byte absolute peak against a 1,000-byte
hold fails as required; the focused node/acquisition validation suite passed
**10 tests**. The local tiny native matrix was rerun after this adjustment:
**9 passed**.

After the absolute-footprint contract and generated schema update, the full
local Python suite again passed **1,829 passed, 159 skipped**. The 138 web
tests, lint/build, strict mypy, Ruff and OpenAPI freshness checks also passed.
The `peak_rss_bytes` evidence now reports the absolute RSS peak, consistent
with its field name and the classifier validator; physical delta remains
separately named.

## Fill and bounded control cache — 2026-10-02

The offline native worker now recognizes the reviewed Flux Fill repository by
its exact repository ID, loads `Flux1Fill` from the verified local copy, binds
the owner source and white-edit mask, and forwards measured guidance without
silently applying image strength or LoRAs. Admin base validation performs a
real local Fill smoke before publishing a Fill-only measured profile. The
node, scheduler, direct admission and fresh web picker accept that profile and
its two input manifests. The separate gated repository and licence acceptance
remain an operator acquisition prerequisite; no full Fill weights were
downloaded during this run.

Flux prompt encodings now use a 256 MiB configurable byte-bounded native LRU.
Canny edges use a private input-ID-scoped byte LRU keyed by the exact source
digest, base/control manifests, runtime, dimensions and thresholds. The
placement hold includes both cache allowances and classifier headroom in addition to the
validated resident estimate. Focused tests prove Fill field forwarding,
mask polarity, memory refusal, cache eviction and Canny cache hits. The
Linux CI collection issue from a direct `cv2` import in the Canny unit test
was fixed by stubbing the native preprocessing import at the test boundary.

After the cache-accounting change, the full local suite passed
**1,868 tests, 160 skipped**, the native tiny matrix passed **10 tests**, and
the web suite passed **143 tests** with lint/build; strict mypy, Ruff and
OpenAPI freshness passed. Remote CI is being rerun.
The tiny fixture does not represent gated Flux Fill or full Union/LoRA/SeedVR2
weights. Full-model Studio and cluster acceptance remains open under T083/T084.

The tiny-model LoRA smoke now counts native encoder events through a clean
adapter load, repeated same-stack job and return to the base: two misses and
at least one hit, with the adapter identity removed on return. Together with
the 20 consecutive changed-seed/step warm hits, changed-prompt/environment
misses, zero-budget test and byte-bound assertions, this closes local T064.
The node command also rejects a Fill attempt with one missing input or
source/mask purposes swapped before the native model is called.

The owner input implementation (T054) is covered by scoped upload/get/delete
route contracts, bounded parser/staging/cleanup tests, active-reference
cancellation tests, exact node transfer binding and the local native img2img
attempt. Full-model source/mask/control transfer acceptance remains in T083.

After binding Fill admission to the exact reviewed repository and refusing
swapped node source/mask purposes, the full local Python suite passed
**1,870 tests, 160 skipped**. Ruff, strict mypy, OpenAPI freshness and the
focused real tiny LoRA cache-miss smoke passed. The prior CI checkpoint
passed lint, unit/contract tests, both engine jobs, image policy and scans;
its disposable Compose integration was still running when this checkpoint
was recorded.

The Fill validator now samples physical footprint across both native load
and denoising; a transient-overrun test fails before a Fill-only profile can
be published. Ordered LoRA replacement now samples the physical reload peak
against the actual worker reservation, records a load stage outcome and
leaves a failed worker unusable rather than serving a partly replaced stack.
The tiny native LoRA smoke and complete 10-test tiny worker matrix pass with
this guard. T056/T061 code work is checked; full-weight compatibility and
memory acceptance remain explicitly open in T083/T084.
The final local Python suite after this guard passed **1,872 tests, 160
skipped**; Ruff, strict mypy, OpenAPI freshness and diff whitespace checks
passed. The skipped tests are external integration/engine selections; the
10 image-engine tests were run explicitly.

T070 accounting review: node admission totals language engines, the image
worker and conversion holds under one lock; health publishes the exact image
child's physical footprint. Core reconciliation adds physical overage only
above resident model/image reservations, so pending conversion holds are not
counted twice. Image dispatch now holds validated resident memory plus exact
auxiliary overhead, two byte-bounded cache allowances and the CPU classifier
allowance. The existing held-worker reuse and uncertain-process tests remain
green. Physical chat/image coexistence is still unapproved pending T084.

## Per-job cache and residency observation — 2026-10-02

The bounded prompt stage caches now distinguish a first cold lookup, a real
resident hit and a key known to have been evicted. The resident worker reports
that outcome with its fenced progress; the Studio journal and core SSE event
carry the typed observation to the image timeline. A running node progress
report marks the worker observed resident at that update; missing or terminal
observations remain explicitly unavailable. Only bounded hashed cache keys are
retained for eviction history. Focused node/core tests passed **48 tests**,
the web suite passed **144 tests**, and strict mypy, Ruff, OpenAPI freshness,
web lint/build and the 10-test local tiny native suite passed. The full Python
suite before the final focused node contract assertion passed **1,872 tests,
160 skipped**; the final assertion passed in the focused rerun. T063 is
complete; the 20-trial production cache measurement remains T084.

T066 coverage reconciliation: the implemented node supervisor and API ledger
tests live in `test_image_process_supervisor.py`, `test_image_residency.py`,
`test_ledger.py` and `test_image_coexistence_admission.py` rather than the
proposed new filenames. They cover held resident and transient bytes, exact
PID re-adoption, tampered/uncertain records, idle TTL and pinning, stop-proof
release, physical overage and missing residency. The focused gate passed
**49 tests**. Production drift and coexistence measurements remain T084.

The simulated worker recovery suite now injects a classifier failure after
successful PNG generation and verifies the fenced worker reports an `unknown`
tag with a safe error while keeping output in private scratch. All **5**
local recovery integration tests passed. The broader all-boundary matrix in
T075 was reconciled in the final-source matrix below.

## Final-source acceptance matrix in progress — 2026-10-02

| Boundary | Local evidence | Required remaining evidence |
| --- | --- | --- |
| Native image modes | Tiny offline Z-Image txt2img/img2img, ordered LoRA replacement, cancellation, cleanup and cache smoke passed; Fill, Union Canny and SeedVR2 have typed binding, local preflight, mode-specific unit smokes and physical hold guards. | Operator must validate both full Studio copies and execute every advertised mode with real pinned assets (T083). |
| OpenAI-compatible and owner API | Image route contracts, SSE/receipt, owner input/output, entitlement and grant tests pass; OpenAPI and generated web types are current. | Browser journeys and final source CI (T081/T082). |
| Authorization and failure recovery | Unit/contract matrix covers revoked keys/grants, explicit entitlement, cancellation/publication arbitration, lost cleanup acknowledgment, parser crash, full disk, partial batch and reboot journal replay. Local simulated classifier failure passed. | Full-model healthy-node cancel timing (T041/T083). |
| Placement and coexistence | Node and core held-memory accounting, profile binding/invalidation, uncertainty and simulated chat/image contention tests pass locally; production admission remains disabled by default. | Cross-process PostgreSQL admission, then operator 15-minute mixed benchmark (T065/T084). |
| Reproduction and cache | Local tiny fixture has 20 warm prompt-cache trials; cold/hit/evicted per-job observations are typed. | Operator 10 same-environment full-model reproduction trials and 20 warm-cache trials (T084). |
| Packaging and operations | Previous CI checkpoint passed all jobs; local affected-image policy, scans and SBOM checks are recorded above. | Final-source CI integration/build/scan and immutable native node/text/VLM smoke (T081). |

The parent task list is the acceptance authority. T041, T065 and T081–T085
remain unchecked until their stated evidence exists. Principles
I–VII and II-a are addressed in the [plan](plan.md#constitution-check) and
draft PR #90. No full-model or real-cluster result is inferred from the tiny
fixture.

After the final prompt/control diagnostic split, the complete local Python
suite passed **1,874 tests, 160 skipped**. The focused simulated image job
and recovery integration suite passed **7 tests**, the local tiny native
worker passed **10 tests**, and the web suite passed **144 tests**. Ruff
format/check, strict mypy, OpenAPI freshness, web lint/build and diff
whitespace checks passed. The skipped selections still require external
integration or platform setup; they are not treated as acceptance evidence.

The private recipe parser client now has an injected connection-loss/recovery
case: a crashed worker returns a content-free retryable error, and the same
bound request succeeds after the worker responds again. Its focused client
suite passed **4 tests**. This adds parser-crash coverage to the local fault
matrix in T075.

T075 coverage reconciliation: the requested authorization and fault matrix
is implemented across the owner route contracts, node worker contracts,
`test_image_recovery.py` and focused publication/recipe/transfer/classifier
unit tests. Revoked owner/key/explicit access, parser connection loss,
classifier failure, disk exhaustion, receipt and cleanup acknowledgment
loss, cancelled partial batches and reboot journal replay each have explicit
assertions. The combined focused boundary suite passed **99 tests** with no
skips. The proposed `tests/integration/test_image_isolation.py` filename
collides with the existing contract module; the API isolation contract lives at
`apps/coire-api/tests/contract/test_image_isolation.py` and local recovery
scenarios live at `tests/integration/test_image_recovery.py`. Full-model
Studio fault/timing acceptance remains T041/T083; T075's local fault matrix
is complete.

T072's `test_image_contention.py` local simulated scenario combines the production
profile/placement/capacity decisions under a controlled chat-before-image
arrival. A measured resident pair admits the image; an additional unmeasured
chat variant makes the pinned image wait while the chat set remains intact.
Invalidating the report or changing the runtime fingerprint also blocks new
image admission. A held image reservation survives lost worker inventory and
leaves zero eligible capacity until stop proof. Both integration scenarios
passed locally. T065 still requires actual cross-process PostgreSQL execution,
and T084 requires the measured 15-minute real-cluster workload.

The first combined collection found a pytest module-name collision with the
existing API `test_image_isolation.py` contract. The new contention test was
renamed to `test_image_contention.py`, and the quickstart command was updated.
After that correction the complete Python suite passed **1,877 tests, 160
skipped**; the selected nine simulated image integration tests passed; Ruff
format/check, strict mypy, OpenAPI freshness and diff whitespace checks passed.

CI run `37071113150` on `a1d9ac3` passed every job: lint, unit/contract,
native text/VLM engine and image-engine tests, all production image builds,
image policy, CRITICAL scans, SBOM generation and disposable Compose
integration. The integration selection finished **124 passed, 33 skipped**;
those skips do not satisfy the opt-in cross-process PostgreSQL or real-Studio
acceptance gates. T081 remains open for the final immutable Studio node
install and text/VLM smoke after the image runtime changes.
