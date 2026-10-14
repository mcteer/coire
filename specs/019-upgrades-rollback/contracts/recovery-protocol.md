# Distinct expired native recovery protocol (T073)

Status: implementation in progress. The primitives below do **not** make native
activation/release ready. Required native/crash/qualification gates remain open.

## Separate authority cancellation

Live human-admin `POST /api/v1/admin/upgrades/recoveries/{recovery_id}/cancel`
accepts only `UpgradeExpiredRecoveryCancelRequest` with exact accepted intent
SHA256. A different live administrator may revoke a demoted/revoked initiator's
intent, including after its budget expires. Wrong bindings, missing authority and
resolved operations refuse. The response is an immutable
`UpgradeExpiredRecoveryCancellation`; concurrent/lost-response retries return the
first receipt, actor and timestamp without another audit or metric event.

Cancellation changes only the separate phase to `recovery_required`, retaining
its active slot and original operation, fences, epochs, transfer/consumption/owner
preimages and resolutions. Existing live effect gates refuse this phase. Receipt
inspection is still historical and never effect permission. The SQL constraint
forbids reopening a cancelled intent into executable phases, and the migration
trigger protects cancellation bytes through any future qualified resolution.
Existing recovered-phase ownership/resolution constraints still apply. This
conservative revocation path does not clear an
uncertain native effect or implement native resolution/handback. A future supported
resolution producer must explicitly preserve cancellation history while proving
the required linked physical/serving/pair/data obligations.

## Final native observation boundary (partial evidence)

The passive same-kernel observer now repeats current support identity, positive
predecessor/original legacy retirement, kernel, unloaded service and exact Source
facts **after** its final Core I/O. A late Source change during that I/O refuses;
no fresh timestamp disguises earlier physical facts. The returned sample records
the beginning of the final physical window, bounded to five seconds and the
unchanged root deadline, with monotonic overrun refusal. Actual private-store
fixtures preserve predecessor bytes for late-change and slow-sample refusals;
native process/launchd/Core facts in these fixtures remain mocked.

Current combined unit/contract/disposable PostgreSQL/migration recovery selection:
340 passed, zero skips, 20.02s (`coire-019-recovery-current-combined-v22.log`, protected).
Full Ruff and strict mypy pass (1212 source files); all 1493 scoped Python files are
formatted. This is not native deployment, serving/cleanup/pair/data qualification,
a release-gate completion or permission to resolve the retained original job.

## First-session lost acceptance acknowledgement (actual private exclusion)

The first-session loss fixture now runs passive-session composition through the
real holding selector and real initial owner context, rather than directly invoking
the owner context. Acceptance asserts an actual continuously held private descriptor;
a lost commit acknowledgement propagates, preserves the exact immutable claim,
refuses blind initial retry and resumes only the exact accepted identity after the
short window inside the unchanged root budget. Nine actual-lock matrix fixtures
pass (2.86s, zero skips, protected session-lease log). Positive first-session
composition now also proves the real initial context accepts once, retains actual
exclusion into holding dispatch, rejects concurrent acquisition and invalidates
the non-wire capability after return; original predecessor bytes stay unchanged.
The holding body and passive delivery in this positive composition remain mocked. Passive grant delivery and
native/Core facts remain mocked here; this is not deployed handover qualification.
Combined recovery/unit/contract/real PostgreSQL/migration: 385 passed, zero skips,
21.86s (protected v26 log). No serving/pair/data qualification or live resolution.

## Trusted observer/publisher composition (not native deployment)

The fixed native observation publisher now posts only to authenticated Core ingress,
bounded by the original budget and physical sample's original five-second window.
It refuses stale/future samples before network, foreign acknowledgements and
redirects; acknowledgement remains metadata, never a grant. Eight publisher cases
pass (3.43s, zero skips, protected composed-publisher log), including the fixed
client through real API middleware into the bounded relay (mocked persistence)
and lost acknowledgement propagation with no fallback. This composes authenticated
transport, not native physical sampling or actual deployment. Only explicit authenticated pending in the passive wait loop invokes
the same-kernel physical observer and publisher; cancellation brackets publication,
errors propagate, and no claim/Source serving/retirement effect occurs. Eleven wait
fixtures with mocked physical/Core boundaries pass (2.66s, zero skips; protected
wait-failure log), including observation identity change refusal before publication
and lost publication acknowledgement propagation without a grant, retry or fallback.

`accept_with_trusted_relay()` supplies the application-installed relay to existing
fresh snapshot/audited acceptance orchestration; caller snapshots/destinations
remain excluded. Actual disposable PostgreSQL exercises relay-backed orchestration
versus direct concurrent acceptance for both retirement proof types (two cases,
7.25s), preserving exact replay/audits. Admin acceptance is still unexposed pending
supported launch/schema preparation and full qualification.

Current combined recovery/unit/contract/PostgreSQL/migration: 381 passed, zero
skips, 21.83s (protected v25 log). Full Ruff/strict mypy pass (1215 sources), scoped
formatting passes (1501 files). No live activation, serving/pair/data qualification,
Source handback, deadline renewal or retained-operation resolution occurred.

## Trusted observation relay foundation

Safe reversible implementation assumption: transient physical observations may be
held in a bounded per-API-process relay, **never** as grants/claims or recovery
authority. API restart/multi-worker mismatch loses a sample and refuses acceptance;
it does not imply pending, renew an epoch, alter Source/fences or authorize an
owner. The producer must resample through authenticated fixed-node ingress, and
admin orchestration must still freshly reauthorize and durably audit acceptance.

`SuccessionObservationRelay` has a 16-snapshot hard cap, five-second UTC/monotonic
retention inside the original root budget, canonical detached DTOs (returned identity is frozen; reads are distinct snapshots
and rejected mutation leaves the retained sample unchanged, eight relay cases pass
0.07s in protected regressed log with fresh owned pytest base, including explicit
regressed-sample rejection that preserves the newest retained sample), exact replay
without retention refresh, predecessor/process fork and regressed-sample refusal.
Absent/stale/future samples and capacity overflow refuse. Seven relay fixtures
pass (zero skips, 2.46s, protected relay log); scoped Ruff/format and strict relay
mypy pass. The relay is now per-application state with metadata-only authenticated ingress
`POST /api/v1/internal/upgrades/{recovery_id}/recovery-succession-observation`:
strict DTO, configured node credential, enabled/no-store, exact path/body scope and
fresh retained predecessor Core gate before publication. Six real-middleware
fixtures with mocked persistence pass (4.26s, zero skips); standalone fixture import
setup was repaired without weakening gates. Ingress emits an OTel span. Native
publication, orchestration wiring and actual native startup acceptance remain
necessary. OpenAPI/TS regenerated/freshness passed; combined recovery/migration:
374 passed, zero skips, 21.84s (protected v24 log). Durable grant/claim ledgers remain unchanged.

## Initial successor session composition (not installed startup)

`run_initial_successor_session()` now composes passive authenticated delivery with
the separate holding controller, which owns claim persistence, repeated physical/
Core gates and actual lifetime exclusion. It checks the enabled fixed node, original
budget and exact existing predecessor, validates the succession id before private
path/I/O use, and refuses any retained successor claim instead of recapturing its
epoch. Delivery errors propagate; cancellation returns without controller effects.
A fresh exact grant forms a distinct claim only in memory before controller dispatch.
The session itself never writes claim/authority records. Four actual-private-store
fixtures (mocked delivery/controller) passed, zero skips, 2.36s; scoped Ruff/format
and strict session mypy passed (`coire-019-successor-session.log`, protected).

Configured guard selection now defaults to `resume` (existing exact claim).
`UPGRADE_RECOVERY_SUCCESSION_MODE=initial` explicitly selects this initial
session only with both distinct scoped selectors and exact existing startup/
predecessor. Missing context or session refusal never changes modes or falls back
to legacy. Strict settings/configured/session tests: 17 passed, zero skips, 2.49s;
scoped Ruff and strict settings/support mypy passed (protected mode-green log).
This initial helper is deliberately not an installer or a missing-file fallback;
trusted observer backend and actual immutable native launch remain incomplete. Session composition is not native deployment or qualification.

## Passive succession polling (not acquisition)

`wait_for_succession_grant()` is a separate passive helper: it samples the exact
current immutable support identity and polls only the fixed authenticated Core
delivery client, waiting in cancellable intervals of at most one second within
the unchanged root budget. Cancellation before/after the read exits; transport
errors propagate rather than implying pending or invoking initial/legacy mode.
It creates no namespace/claim, retires no process and publishes no serving state.
Returned metadata still requires actual exclusion and repeated physical/Core
checks at initial acquisition inside the original short grant epoch. The configured
guard selector continues to require existing claims; this helper is not yet wired
to unsupported startup acquisition or deployment. The helper repeats immutable support identity after granted delivery and rechecks
cancellation, root deadline and the original grant epoch before returning. Eight
mocked identity/Core cases pass, including positive metadata delivery, late identity
change, root expiry across I/O and grant expiry (2.36s, zero skips, protected
`coire-019-succession-wait-matrix-green.log`). Scoped Ruff, formatting and strict
helper mypy pass. Combined recovery/unit/contract/real disposable PostgreSQL/
migration validation: 351 passed, zero skips, 19.95s (protected v23 log). This is not native startup qualification.

## Trusted succession acquisition progress (partial)

Internal orchestration now snapshots the exact retained predecessor and initiating
user/key/version before invoking an application-installed trusted observer. The
snapshot transaction closes before bounded I/O (five seconds or the unchanged
root budget, whichever is shorter); final audited acceptance repeats authority
checks. Foreign responses, timeout and cancellation do not invoke acceptance.
Actual disposable PostgreSQL exercises concurrent orchestrated/direct acceptance
for both retirement proof types. Replay does not repeat observations or effects.

A separate strict grant-delivery read/state contract distinguishes explicit pending
from errors. Pending requires the exact canonical predecessor, distinct successor
in the pinned support environment and a fresh live Core gate; missing/revoked/
foreign authority never implies pending. Delivered grants bind successor, predecessor
hash, succession id and original short handover epoch. Fixed authenticated delivery callback
`POST /api/v1/internal/upgrades/{recovery_id}/recovery-succession-delivery`
and the native fixed-Core client now enforce configured node credentials,
enablement/no-store, strict scope and a nonfuture five-second response sample.
No redirect, foreign response or expired root budget permits a fallback.
Mock-HTTP native-client coverage also verifies a positively delivered grant,
foreign grant process refusal and refusal of an initially valid response whose
short grant epoch has expired by receipt (nine client cases, zero skips, 2.24s;
protected `coire-019-succession-granted-client.log`). A fresh response sample never
renews the grant window. The
callback is metadata only; audited admin grant acceptance remains unexposed.
Native passive acquisition/launch and physical lifetime checks are still required. Neither pending nor delivered metadata
is an owner claim or permission to act. Combined recovery/migration validation:
326 passed, zero skips, 18.32s (`coire-019-recovery-current-combined-v20.log`, protected).
No live schema change, deployment, activation or original-job resolution occurred.

## Current successor execution evidence (partial, not deployment permission)

Configured guard mode now has a strict distinct successor selector scoped to the
original recovery id and exact existing immutable startup/claim. Missing selected
preimages, controller errors and cancellation never fall back to initial/legacy
mode. Accepted/pending selection is explicit and authenticated, including canonical
grant hash. Initial acceptance, actual private descriptor lifetime, lost response,
accepted exact-identity resume, journalled ordered unready publication and the
bounded separate heartbeat loop are composed without unlocking between phases.
Original claim/selection/link/deadlines/receipts/fences remain unchanged; ready is
always false and no serving/pair/data qualification is claimed. Tests include
journal lost response, runtime/status partial write replay, cancellation during
publication/heartbeat gates and exact deadline refusal. Physical/Core native facts
remain mocked in the actual-private-lock layer, distinct from PostgreSQL persistence
and real middleware transport evidence.

Latest combined recovery/unit/contract/migration: 317 passed, zero skips (18.17s),
protected `coire-019-recovery-current-combined-v19.log`. Full Ruff and strict mypy
(1210 sources) pass; scoped formatting check reports 1487 files already formatted.
Successor refusal telemetry has a dashboard/runbook/alert with all isolated
promtool scenarios passing (no network/downloads, non-root/read-only/capped).
Remaining: initial immutable startup/process acquisition and trusted audited grant
orchestration, multihop/changed-boot/standalone-supervisor handling, actual immutable
deployment and supported legacy schema preparation, linked fresh serving/cleanup/
exact-pair/data qualification and fenced Source handback/resolution, complete release
gates. No live migration, support activation or retained-job resolution occurred.

## Same-kernel succession ledger (partial implementation)

The passive observer and strict grant DTO retain the complete predecessor and
once-captured recovery deadline. Internal `accept_succession` requires a trusted
node-produced observation (not an admin-supplied physical proof), the same live
initiating admin/credential, exact original binding/fences/catalog and fresh
observation. A short transfer window is captured once inside the original budget.
Grant and audit commit atomically; exact replay preserves epochs, and a unique
recovery/predecessor ledger key refuses forks. The ledger is append-only. Once
accepted, the predecessor's live effect gate refuses, while original maintenance
and history remain unchanged. This first-hop scope is deliberately unexposed:
trusted transport/orchestration, successor claim/lifetime exclusion, repeated
physical gates, subsequent crash succession and changed-boot handling remain
unimplemented. Neither the DTO nor an opaque persisted fixture proves ownership.

Validation: actual disposable PostgreSQL checks audit rollback, concurrent
single acceptance, exact replay/fork refusal and predecessor effect revocation
for both acknowledged and observed retirement. Combined recovery suite: 252
passed, zero skips (13.69s); physical observations remain synthetic in these tests.

A separate unexposed initial succession-grant gate now verifies the canonical
ledger hash/scope, exact successor process and fixed destination, current initiating
admin/credential, original ownership/fences/catalog, active recovery and original
short handover window. It is metadata permission only—not a physical claim. At
transfer expiry it refuses even if the root recovery budget is still live; exact
grant replay remains inspection without renewal. Real PostgreSQL verifies foreign
process/node, exact expiry, demotion, single audit and preserved replay epochs for
both retirement proof kinds. Updated combined suite: 257 passed, zero skips
(14.28s); full Ruff and strict mypy (1206 sources) pass. Successor claim, lifetime
lease, authenticated transport and actual native qualification remain open.

`UpgradeExpiredRecoverySuccessorClaim` now describes a distinct successor
preimage, not a rewritten initial owner/consumption. It requires exact grant
successor, verified runtime selection, same kernel, preserved Source and a claim
epoch inside the original short handover window. Its shape grants no ownership:
a trusted producer, real lifetime capability, authenticated audited persistence
and repeated physical/Core effect checks remain unimplemented. Seven strict
contract cases cover mismatched process/kernel/runtime/Source and early/expired
claim epochs. Combined suite: 264 passed, zero skips (14.05s); targeted Ruff and
shared strict mypy pass. No activation or original receipt mutation occurred.

Distinct successor claims now have their own immutable append-only Core ledger
linked restrictively to the grant, never updating initial owner or consumption.
Internal (unexposed) acceptance checks exact retained grant/predecessor and current
initiating admin binding, requires a live short-window initial grant and fresh
claim, and commits claim plus audit atomically. Concurrent identical claims replay
once; different preimages refuse. The independent live successor gate permits
only the accepted exact process to resume after transfer TTL while still inside
the original recovery budget; exact root expiry and demotion refuse. Actual
private lifetime capability and repeated current physical checks remain mandatory
at the future native caller. PostgreSQL tests cover immutable table writes,
audit rollback, concurrent acceptance, lost-response replay, transfer-TTL resume,
root expiry and demotion for both retirement proof kinds. Combined suite: 264
passed, zero skips (14.38s). No live schema preparation or ownership occurred.

Successor lost-commit acknowledgement now has explicit Core pending/accepted
inspection via `UpgradeExpiredRecoverySuccessorState`. Pending is returned only
after positive exact live initial grant/process verification, not merely a missing
claim row. Missing authority or Core failure never means pending. Accepted state
requires the exact retained claim/process and fresh original-budget gate, including
resume after short transfer TTL. The DTO binds the accepted hash and original
epoch. Actual PostgreSQL covers pending, accepted and accepted-after-TTL for both
retirement histories; 2 cases passed (4.71s), targeted Ruff/strict mypy pass.
Core now exposes fixed `POST /api/v1/internal/upgrades/{succession_id}/recovery-successor-check`
and `/recovery-successor-state` callbacks with strict shared claim-hash/process
request DTO. Each authenticates the configured Studio credential independently,
requires upgrades enabled and returns no-store; middleware exceptions are limited
to these existing-depth POST suffixes. Admin grant acceptance remains unexposed;
the successor mutation callback described below now has the same fixed-node gate. Eleven transport tests cover missing/wrong/
foreign credential, disabled feature and extra fields, plus real middleware
positive state roundtrip with mocked persistence (not physical qualification).
Combined suite: 275 passed, zero skips (16.47s); OpenAPI/TypeScript regenerated
and OpenAPI freshness passes.

Native fixed successor check/state clients now enforce destination/credentials,
exact claim hash, unchanged original budget, fresh nonfuture state, no redirects
and no expired-pending fallback. An accepted same-identity successor may resume
past transfer TTL without renewal. `resumed_successor_owner` now holds actual
private `recovery-transfer.lock` continuously and binds the non-wire descriptor
capability to the distinct successor hash. It repeats live Core, current verified
support identity, positive original/predecessor retirement, same kernel, unloaded
known service and exact preserved Source; `require_live_successor` requires that
actual capability before repeating Core/physical effect gates. It creates no
Source/status/runtime/owner records and is not an initial acceptance path. Six
fixture cases prove real exclusion, rejected concurrent acquisition, descriptor
invalidity outside lifetime, Core revocation while held and physical uncertainty
refusal; physical/Core collaborators remain mocked, not native qualification.
Combined suite: 297 passed, zero skips (17.25s); subsequently strengthened six
lease cases also pass (1.27s), targeted Ruff/strict module mypy pass. Initial native
claim acceptance/activation, controller publication, multi-hop/changed boot and
actual recovery qualification remain open.

The fixed successor acceptance callback now uses
`POST /api/v1/internal/upgrades/{succession_id}/recovery-successor-claim`, strict
claim DTO, configured node credential, enabled/no-store gate and exact path/body
succession binding before the audited service. The native initial acceptance
client uses only the captured short transfer deadline and exact canonical reply;
expiry refuses before network work, and late/lost acknowledgement uses explicit
state inspection instead of renewal. Positive real middleware/client roundtrip
with mocked persistence and missing/wrong/foreign credential/path mismatch refusal
pass; 24 client cases include exact binding, redirects and expired acceptance.
Combined suite: 306 passed, zero skips (17.29s); targeted Ruff/strict mypy pass;
OpenAPI/TS regenerated and freshness checked. This is not an initial native
permission to deploy partial support.

The initial native successor owner context now composes real continuously held
private exclusion, exact retained predecessor, canonical authenticated pending
grant/hash inspection, repeated current physical gates and immutable separate
claim preimage before one acceptance attempt. The accepted live gate and descriptor
remain held through caller lifetime. A retained preimage prevents blind initial
retry; an uncertain response propagates without readiness/effects or budget renewal.
Actual-lock fixture tests prove lost acknowledgement retains bytes/epoch, calls
acceptance once, refuses a blind retry, and permits only explicit accepted state
plus freshly verified exact-identity resume after transfer TTL. Core/physical
facts remain mocked in this layer; PostgreSQL and transport evidence remain
separate. Eight lease cases pass; combined suite: 307 passed, zero skips (17.83s).
Initial producer/controller integration, complete handover crashes, multi-hop and
changed boot, actual immutable deployment and native qualification remain open.

Pending state requests now also include the exact grant hash, checked against the
Core ledger for pending and accepted responses; an echoed unknown claim hash is
not sufficient initial scope binding. PostgreSQL foreign-grant refusals and strict
shared request/native/route changes pass; OpenAPI/TS were regenerated with freshness
verified. Full Ruff/strict mypy passed across 1208 sources before this test-only slice.

## Authority and observation

- Original job/generation/plan, deadlines, receipts and failure history stay intact.
- Only live administrators accept a separate idempotent intent. A new capture has
  one 1–300 second budget; exact replay returns its original timestamps.
- Immutable support must declare exact Source compatibility and match platform/kind.
- A support observer uses verified installed bytes and fixed existing private roots.
  It reads canonical claim/plan/native/scopes twice; it creates no Source paths.
- Retained restoring/source-selected/source-verified markers can be expired and
  unfinalized. They are metadata, not fresh serving or pair qualification.
- Core commits its snapshot transaction before bounded authenticated observer I/O,
  then rechecks authority, complete fences and catalog during audited acceptance.

### Passive support startup context

A trusted fixed-node support-context reader reauthorizes the initiating human/key,
original nonterminal active plan, immutable support catalog, and captured recovery
epoch before returning the accepted intent. Conservative reversible assumption:
require fully acknowledged active original maintenance even for helper startup.
This stricter prerequisite avoids starting a recovery helper before drain ownership
is settled; it does not waive or substitute transfer/consumption/physical gates.
The reader creates no paths, renews no deadline and grants no engine effect. Its
concrete authenticated transport/passive-process producer remain prerequisites.

## Pending transfer, consumption and stop

1. Capture exact old guard and distinct successor process identities. Persist the
   short transfer window (at most 30 seconds within the separate intent budget).
2. Prepare and consume once with live administrative/credential reauthorization,
   unchanged original authority, acknowledged active fences and immutable catalog.
   Audit and each immutable payload commit in the same transaction.
3. The node authenticates fixed Core consumption callback; exact receipt SHA,
   successor/node, current authority and captured window are checked without renewal.
4. Local preflight verifies current PID/birth/support interpreter and bytes, old
   runtime bytes, fresh guard identity and exact owned old helper. Absence alone
   authorizes nothing. Kernel exclusion remains held through native effects.
5. Managed stop requires a positive unique fixed launchd service/PID observation,
   live `abandon process group` property, and exact safe installed definition.
   Do not infer unmanaged ownership from missing launchd services. A standalone
   legacy helper needs its own supported positive supervisor/ownership protocol;
   the managed adapter must refuse it rather than silently treating it as managed.
6. Retain write-ahead consumption/service/definition/**Source process** preimages
   before the fixed stop. Source descendants are preserved. No shell, arbitrary
   service target or privilege escalation is permitted. Missing privilege is a
   concrete prerequisite, never a reason to widen permissions.
7. Stop receipt requires acknowledged service unload, old identity absent and the
   exact Source process unchanged. It is not exclusive successor ownership,
   complete physical cleanup, qualification or permission to release maintenance.
8. A retained intent without receipt is uncertain: reconcile, never repeat stop.
   Historical receipt replay checks canonical preimages and captured epoch. Replay
   is not a fresh observation and cannot bypass the next effect's authority gate.

## Successor and exact Source handback (remaining producer work)

The exact Source node must remain serving from its sealed original environment.
Legacy node readiness recognizes the fixed `coire_node.upgrade_guard` module, so
support cannot pretend a differently named module is the original helper. Preserve
this compatibility using an explicitly scoped support mode in the immutable new
helper, with default legacy behavior unchanged; do not patch the old environment.
Verify the actual process/module/bytes and publish ownership/status only after
positive old-owner retirement and exclusive scoped claim, not from absence alone.

Required remaining boundaries include support claim/publication, crashes with
partial metadata, cancellation, fresh serving/physical cleanup/exact-pair/data
proofs, and a linked resolution/handback commit. Starting the original Source
helper must use its verified unchanged definition/runtime. Its old claim and
original budget may not be fabricated as successful. Any supported native claim
release must be linked to the new positive recovery proofs and recorded through
its producer; no manual journal edits.

Core's original admission fence and active operation slot remain until exact Source
helper ownership/readiness and linked after-deadline resolution are verified. A
local helper handback must not create an interval in which ordinary new work is
admitted before Core commits resolution. Preserve original failure/budget history;
never relabel the original upgrade as successful or recycle old qualification.

## Crash succession observation boundary

Initial same-identity resume remains separate from different-process succession.
The first succession observation contract is conservatively same-kernel only:
distinct succession ID, complete unchanged predecessor owner, different PID/birth,
exact verified support bytes and preserved Source preimage, positive predecessor
retirement, and observed time within the original separate recovery budget. It
accepts no caller deadline and does not renew that budget. This is a reversible
scope assumption for the first implementation slice, not a waiver of changed-boot
coverage. A changed boot needs its own supported fresh Source/process evidence;
it cannot be disguised as same-kernel succession. DTO shape/absence alone grants
nothing: trusted physical observation, separately audited succession acceptance,
actual lifetime exclusion and fresh effect gates are still unimplemented here.
Do not deploy this observation contract as a takeover or alter predecessor claims.

## Evidence and shipping gate

Synthetic process/launchd/plist/acknowledgement fixtures test contracts and producer
ordering, not actual ownership or native qualification. PostgreSQL tests prove
atomic audit/intent/transfer/consumption retention separately. Real isolated Mac
and authorized Studio acceptance, every crash boundary, complete fresh physical
inventories and required Source/pair/preservation gates remain prerequisites.
Neither this document nor the unit tests authorize live release approval.
