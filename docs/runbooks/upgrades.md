# Managed upgrades and recovery

Managed upgrades default to disabled. Feature 019 is still being validated.
The independent Core executor, bootstrap handover, all six Core service failure
recoveries and the nine-case native Studio suite have passed isolated acceptance.
Remaining workload, failover and isolated CI gates still prevent production enablement.
Use the feature execution record for the actual tested build and acceptance state.

## Observe

Use the admin console Upgrades page or the authenticated `coire upgrades` CLI to
inspect an operation by job ID. Retain that job ID when reconnecting; resubmitting
with a new request key creates a different request. Events, receipts and audit rows
remain authoritative with diagnostics disabled.

Prometheus loads the upgrade rules in its baseline image. The optional diagnostics
Grafana dashboard is **Coire upgrades and recovery** (`coire-upgrades`). It shows
retained operations, progress age, uncertain restoration, executor readiness,
heartbeat age and receipt outcomes. Metrics contain bounded kind/outcome labels;
inspect authenticated job status for identities and evidence.

An absent or stale health snapshot means observation has failed. A missing executor
heartbeat during a retained Core operation means independent recovery is unavailable.
Neither observation authorizes clearing maintenance. A progress alert indicates a
retained obligation; inspect its stage and original deadline before taking action.

## Recover

For a retained pre-recovery `0034_upgrades` database, inspect
`GET /api/v1/admin/upgrades/recovery-schema/{original_job_id}` and use the matching
typed `POST /recovery-schema` preparation before accepting a new intent. Pin the
observed structural digest and original job/generation/plan/Source. The supported
one-shot migration utility invokes these same authenticated API routes without
opening a listener, using the mounted admin credential. Preparation is atomic
with its audit and leaves Alembic history, original rows, deadlines, receipts,
slots and fences untouched. Partial protocols and unknown constraint shapes
refuse. Never run the modified migration over an already recorded head, edit the
revision table, or submit ad-hoc SQL to reproduce this transition.

Use `GET /api/v1/admin/upgrades/recoveries/{recovery_id}` for the durable status
returned by a separate recovery acceptance's `Location` header. This read requires
a live administrator and exposes the captured intent, phase, active obligation,
owner digest and retained cancellation; it grants no native execution authority.
Expired and cancelled intents retain their original operation's maintenance.

An expired cold attempt that never received transfer/owner/effect authority may
use the fixed installed `coire_node.expired_recovery_unstarted_observer` module.
Its bounded stdin is `UpgradeExpiredRecoveryUnstartedObserverInput`; it verifies
its installed runtime, the exact retired startup actor, locked original preimages
and unchanged data, and retains a metadata proof only in the separate namespace.
The fixed metadata context/publication routes grant no native recovery window.
An authenticated administrator may submit its intent/proof digests and a stable
request key to `POST /recoveries/{recovery_id}/close-unstarted`. Audit and closure
commit together. Only that attempt becomes `cancelled_unchanged`; the original
operation, slot, maintenance, deadlines and failure/dispatch history stay held.
Issued authority, live startup, drift, stale samples and unknown state refuse.
The audit stores digests and a safe summary without bypassing credential
redaction. Lost response replay requires no new observation or budget.

A cancelled, expired cold attempt may also be closed before Source projection
when its exact retained cold owner and activation are positively retired. The
installed observer verifies these preimages and rejects every downstream or
unknown effect record. This closure becomes `superseded`, preserves the cold
claim, activation and cancellation, and records their owner digest in its
resolution receipt. Original maintenance and deadlines remain held. Any Source
projection, serving, archive, transfer or handback evidence requires the
appropriate continuation protocol instead.

An accepted recovery with no original collective dispatches retains positive
absence intent, effect and receipt evidence. Both original and archived job
namespaces must remain absent; no old result or history directory is synthesized.
Fresh local and peer ownership observations still must prove complete cleanup.

The typed SDK and `coire upgrades` CLI expose `expired-recovery`,
`recovery-status`, `recovery-cancel`, `recovery-close-unstarted`, `recovery-schema`
and `recovery-schema-prepare`. Acceptance, schema preparation and unstarted
closure read bounded strict JSON from stdin. Never generate a new acceptance
automatically after expiry; another budget is a separate explicit administrator
decision under FR-032. Observe **Unstarted recovery closures; original holds
retained (5m)** and `CoireUpgradeUnstartedRecoveryClosed`.

After stage-only installation and authenticated admin acceptance, invoke the
installed support interpreter with `-I -B -m coire_node.expired_recovery_startup`.
Its bounded stdin is `UpgradeExpiredRecoverySupportStartInput` containing only
the accepted intent and installed runtime identity. Roots and fixed control URLs
come from documented settings; the node credential comes from Keychain. The
launcher verifies its own immutable interpreter and fresh Core context, records
the launch boundary, and starts only the fixed `coire_node.upgrade_guard` module.
The receipt records its actual PID/birth and grants no effect authority itself.
The guard initially waits for the recorded launcher to retire and its cold
physical scope to clear within the captured launch subwindow. This observation
produces no ownership or Source effect; normal cold authority/lock gates follow.
Replay requires the exact live actor. An uncertain launch or absent actor refuses
and requires explicit succession; never remove a launch marker to force a retry.
Observe `coire_upgrade_recovery_support_launches_total{outcome="recorded"}` and
the scoped structured launch log. Supervisor acknowledgment has a captured
30-second subwindow inside the recovery deadline; retries cannot reset it.

Explicit `recovery_mode="cold"` requires positive retirement of retained helper,
node and supervisor identities, verified immutable Source/support graphs and
continuous original writer/recovery exclusion. Missing processes alone grant
nothing. Fixed Source selection and launch have separate immutable write-ahead
records and exact before/after reconciliation; unknown identities/pointers,
revocation and exhausted budgets refuse. A recorded launch without an identified
live child requires supported failed-launch reconciliation rather than a duplicate
spawn. Guard readiness under maintenance is separate from serving qualification.

For a sealed Source predating collective cleanup, the tested compatibility
producer archives the exact original dispatch directory only after fresh trusted
local and peer inventories prove physical cleanup. The protected recovery history
retains every original scope/result/lock byte and its digest. It never creates a
result for an interrupted probe or relaunches it. A rename interruption reconciles
the recorded original/archive locations. The old Core obligation remains active
until fresh serving, new exact-pair qualification, preservation and handback are
linked by the resolution protocol. Never perform this move by hand.

Cold effects emit `coire_upgrade_cold_recovery_effects_total` with bounded node/
outcome labels and scoped job/recovery/generation/user logs. The cold-effects panel
distinguishes selection, launch and guard observations from serving qualification;
`CoireUpgradeColdRecoveryRefused` alerts on refused effect gates.

Source handback releases only the original writer descriptor to the exact sealed
Source guard. Recovery exclusion stays held. The fixed fixture supervisor inherits
the already-held fixture descriptor and adopts the actual Source children before
local closure. Its heartbeat is `coire_upgrade_source_supervisor_heartbeat_seconds`;
`CoireUpgradeSourceSupervisorMissing` alerts when required supervision is stale.
Never manufacture a descriptor number, adoption receipt or Source process record.

Core transitions emit `coire_upgrade_cold_recovery_transitions_total` only after
the audit commits. The separate resolution's `recovered_after_deadline` outcome
and `original_upgrade_succeeded=false` record the failed upgrade and subsequent
Source recovery. The original operation and linked event retain the reader-compatible
`rolled_back` phase with reason `expired_operation_recovered`. The resolution
retains the full pre-resolution operation, including its old failure reason.
Original plans, deadlines, earlier receipts and archived dispatch bytes
remain retained. The immutable local closure receipt is distinct from fresh final
physical observations. A lost Core response uses the fixed read-only resolution
state path; it cannot trigger another release or recapture the recovery deadline.
The current controller connects the retained fixture's rank-zero cold path;
managed registration, crash succession and complete native acceptance remain
required before deploying it operationally.

### Distinct successor holding (implementation incomplete)

The successor controller writes only a separate scoped `ready=false` heartbeat;
its descriptor lifetime and fresh Core/physical gates are not serving, pair or
data qualification. Observe the dashboard **Successor controller outcomes (5m;
not qualification)** and `CoireUpgradeSuccessorControllerRefused`. A refusal
retains original maintenance/history; inspect linked audited grant/claim hashes
and structured `job_id`, `recovery_id`, `succession_id` logs without dumping raw
launchd/configuration or credentials. Core errors never authorize initial fallback.
Cancel the supported controller lifetime rather than signalling unrelated PIDs;
cancellation during a fresh gate stops before heartbeat publication. Lost initial
acceptance acknowledgement requires explicit authenticated accepted-state inspection
and exact same-identity resume within the original recovery budget, never blind
initial retry, renewed epochs, manual claim edits or maintenance release. Main
selection, journalled legacy publication, immutable deployment and qualification
remain unfinished: **do not deploy partial support or resolve the retained job**.


Keep the existing Source containers, frozen Compose releases, native environments,
private journals, backup receipts and credential mount generation intact. The last
completed migration may belong to an older release than the selected binaries;
retain that release too. Never downgrade the production database to recover binaries.

Cancellation requests recovery of the accepted operation. It does not forcibly stop
training, agent, image or other admitted user work. Unknown processes, unfinished
cleanup and lease renewal obligations continue to block replacement and fence release.

Existing authenticated owner image cancellation, admin run termination and
admin API-key revocation remain available at their canonical DELETE resource
paths during maintenance. Their ordinary authorization, ownership and audit
checks still apply. New submissions and malformed or unrelated paths continue
to receive the maintenance retry response.

A successful node KILL command is also a cleanup receipt: it removes the runner,
relay and private network through the same path as REMOVE. A requested kill or
failed command does not prove cleanup. Pending commands and unknown native
inventory continue to block replacement.

If recovery is required, inspect the authenticated operation and the namespace-local
recovery owner. Repair the concrete missing retained artifact or unavailable service
without deleting its ownership files. Process death, elapsed deadlines and a healthy
container at a different image are insufficient proof. Host deployment commands must
honor the same deployment mutex and managed operation hold; do not bypass them with
ad hoc Compose mutations.

The independent owner must verify every selected Source image/runtime, compatible
schema, serving check and positive owned cleanup before maintenance can reopen.
During implementation, use isolated acceptance fixtures rather than production
activation to validate each recovery boundary.

For disposable acceptance fixture retirement, first require authenticated complete,
empty work inventories. Stop every old coordinator before exposing fresh native
state on reused test ports. A configured Compose service name can miss a container
restored under an executor-owned name: verify exact project and service labels,
record the actual container IDs, and confirm that no old scheduler remains running.
Retain volumes, sealed runtimes, operation journals and unreleased foreign fences.
This retirement procedure applies to owned test fixtures; managed production
recovery continues through the authenticated protocol and deployment mutex.

For a failed native serving child, retain its operation's stage attempt and private
smoke scope. The scope's typed `progress.json` records the last phase reached
(validation, engine start, generation, cleanup or final asset verification). It
contains no prompt or generated text and is diagnostic evidence only. A `complete`
progress phase cannot replace `result.json`, verified cleanup or the authenticated
operation receipts. An empty engine journal with a stale outer owner is reconciled
only after the exact child and recorded engine birth identity are positively absent,
the native census is complete, and accepted ownership and the original deadline
still hold. Missing generation evidence keeps recovery required; never delete the
owner or extend its deadline to make a failed serving check pass.

A consumed migration grant does not prove DDL ran. If no migration dispatch intent
was retained, recovery can request the executor-only `migration-not-started` fence.
The API serializes it against the runner's pre-DDL database claim, permanently
refuses delayed starts and records an unchanged Source schema. The executor also
requires an explicit Docker 404 for the exact candidate, a fresh unchanged schema
and the original recovery deadline. An existing dispatch intent, execution claim,
changed schema or unavailable observation keeps maintenance closed. The fence is
separate from migration completion and never supplies an invented exit code.

## Observed managed retirement reconciliation

`coire_upgrade_recovery_stop_reconciliation_total` counts attempts that retained
observed history (`outcome="observed"`, including exact replay) and authority or
physical refusals (`outcome="refused"`). Neither outcome reports acknowledged
`bootout`, new ownership, serving readiness or completed cleanup. The upgrades
dashboard shows these separately; `CoireUpgradeRetirementReconciliationRefused`
alerts on a refusal in five minutes, also including the separately plotted
`coire_upgrade_recovery_stop_budget_total{outcome="refused"}`. That counter means
the once-captured local stop budget refused a gate; it cannot prove whether an
already dispatched bootout executed. The span is
`coire.node.recovery_legacy_stop_budget`. Retain uncertain intent and use supported
reconciliation, never retry bootout or renew deadlines. Correlate structured logs using `job_id`,
`recovery_id` and original `generation`; never dump raw service definitions.

A retained uncertain stop intent selects the supported observation producer,
never another stop command. It requires the same actual initial consumer under
its unextended transfer window, current Core authority, actual private exclusion,
unchanged installed definition, unloaded managed service, positive old identity
retirement and exact Source preservation. A separate immutable reconciliation
record is not an acknowledged-stop receipt. Corrupt/ambiguous history, loaded
service, live/uncertain old PID, changed Source or expiry refuses and retains
original maintenance. Do not manually edit journals, renew deadlines, infer
retirement from access denial or signal whole groups containing serving Source.

Cancel the separately captured intent with a live human administrator using
`POST /api/v1/admin/upgrades/recoveries/{recovery_id}/cancel` and the strict body
`{"intent_sha256":"<exact accepted intent SHA256>"}`. The initiating credential
may have been revoked: another live administrator can revoke the intent. The
operation must still own its active slot; resolved authority cannot be cancelled.
An identical retry returns the original immutable cancellation and timestamp;
wrong bindings refuse before any change. No new epoch or native stop is issued.

Cancellation commits an audit row and changes only the separate intent to
`recovery_required`. It revokes live support, consumption and successor effect
gates, including accepted owners, while retaining both active slots, all receipts,
claims, original deadlines and admission fences. It cannot prove a previously
dispatched native effect did or did not occur. A captured-budget expiry also
refuses further effects; expiry is not automatic cleanup or release.
The response explicitly reports `maintenance_retained: true`. Use the original
job/recovery/generation in structured logs; the cancellation panel and
`CoireUpgradeRecoveryCancelled` alert count only newly committed cancellations,
not replay or failed audit. Keep original admission fences held until a separately
supported recovery proves fresh linked qualification and complete handback.
There is no manual journal rollback or emergency activation command here. Actual
immutable installation, standalone supervision, changed-process/boot succession
and full native acceptance remain prerequisites; do not deploy an unqualified
holding controller or treat these operational panels as release approval.

## Interrupted collective cleanup

Use only a tested immutable deployment that supports the cleanup protocol. Keep
original scope, job, generation and deadline unchanged. The authenticated control
route `POST /node/upgrades/{job_id}/collective-cleanup` accepts a shared
`NodeUpgradeCollectiveCleanup` document, never caller inventory, a URL or a command.
Its fixed Core cleanup-peer callback authenticates rank-zero Studio A and rechecks
accepted paired maintenance before/after observing Studio B. Unknown/live owners,
foreign authority and a held dispatch lock refuse closure; do not renew the epoch,
hand-edit journals or turn process absence into qualification.

Observe the interrupted-collective cleanup dashboard panel,
`coire_upgrade_collective_cleanup_total{node,outcome}` and scoped node logs. A
refusal raises `CoireUpgradeCollectiveCleanupRefused`; inspect complete owner
inventories and exact process provenance rather than clearing its logical hold.
Successful closure retains both canonical inventory preimages and a separate
interrupted receipt, not a measured probe result. Exact lost-response replay
returns that same receipt without new observation. A partial write retains the
hold until retry obtains fresh evidence. Missing/tampered retained proof blocks
canonical inventory. Physical owners are always scanned independently.

Cleanup alone never releases maintenance or finishes recovery. Require the
ordinary Source-serving, physical cleanup and paired-readiness proofs under the
accepted operation's original budgets. If the installed immutable Source lacks
this protocol, do not patch its files or replace an active helper using idle
refresh: that path requires released ownership and positive terminal cleanup.
Retain the recovery-required operation and resolve the supported deployment path
before any live mutation. Preserve journals, secrets and active workloads.

## Refresh a native recovery owner

After a successful Studio change, the idle helper verifies the completed operation,
prepared release, exact live Source node PID/birth and runtime graph. It retains a
handover receipt before publishing the new helper runtime, stops its heartbeat,
releases the writer lock and executes that Source's fixed guard module. The node
keeps serving. Released operations report unready until that owner refresh.

Launchd retains the original sealed launcher interpreter in `launcher-runtime.json`.
On restart it delegates only to the runtime qualified by a committed refresh and
positive terminal cleanup history. An active operation must name that runtime as
Source. Neither the current pointer nor a caller-supplied command selects the helper.
Keep the original launcher and completed operation journals retained. A published
handover replay does not require its originally captured node PID to remain alive;
the restarted owner still verifies startup and serving independently.

Keep the node process serving. Stop only the exact upgrade-guard service, then
use the existing installer bootstrap command with the current Source's protected
release-registration request. The command requires released operation ownership,
positive terminal cleanup, the exact live node PID/birth and a verified selected
runtime. It acquires both native kernel locks, retains an intent/commit refresh
receipt, and preserves completed operations and their selection history. Install
the rendered guard plist pinned to that current Source, start the guard and check
fresh authenticated readiness. An interrupted refresh is replayed with the same
request; do not delete bootstrap or operation journals.

Automatic handover passed the native Studio acceptance suite. The complete release
gates remain pending. Use isolated feature fixtures for validation.

## Rollback limits

Rollback is available only for a retained compatible release and its recorded forward
operation. Published datasets and trained adapters remain published; unexported feedback
withdrawal and purge obligations continue. Historical training/evaluation records and
credentials must retain their identities and bytes across a transition. A changed
native environment invalidates current qualification until the exact runtime, and
exact peer pair where required, pass fresh checks.

Schema-changing Core upgrades require a protected source-schema backup no older than
24 hours with a successful isolated restore receipt. The backup preparation mode is
documented in the feature quickstart; it never restores production or rotates secrets.

Prepare the backup before publishing initial executor bootstrap intent when possible.
An idle qualified executor continuously holds the deployment lease while observing
Source. If backup preparation reports that ownership is not committed, first verify
there is no accepted upgrade or handover and no retained recovery obligation. Gracefully
stop that project's exact executor container through its frozen Compose configuration;
its normal shutdown must commit its effect-free ownership. Run trusted backup
preparation, then restart the same container and wait for fresh bootstrap readiness.
If graceful shutdown leaves active ownership, retain the journal and use its existing
recovery owner. Never edit ownership to make backup preparation proceed.

## Core bootstrap intent publication

Once the approved executor topology and its private catalog/state volumes exist,
finish the normal host-owned bring-up before qualifying the independent executor.
An application-network database credential preflight refusal leaves the current
release unchanged and releases the effect-free host claim. After reviewing the
retained release and backup location, retry with `coire-up --recover-db-role`.
Once role recovery or service activation begins, a failure retains host ownership
for runtime reconciliation; do not clear that ownership manually.
Retain a mode-0600 `CoreBootstrapSeed` document outside Git containing the sealed
registered Source, exact Source container IDs, and the independent executor's
runtime, container and immutable Linux ARM64 image identities. Publish that
administrative intent with:

```sh
uv run python scripts/coire-deploy.py prepare-bootstrap /private/operator/bootstrap-seed.json
```

The fixed publisher runs without a network, shell, Docker socket or coordination
mount. It reads the existing prepared catalog and writes only the service-owned
bootstrap request. The executor then checks its actual container namespace,
Source and executor configuration, sealed catalogs, health and kernel ownership
before submitting a fresh bootstrap heartbeat. Publishing the request does not
make the executor ready. Inspect authenticated readiness and the baseline
bootstrap metrics; retain the request and catalog when a check fails. Conflicting
retained bootstrap history must be reconciled through its existing owner.

## Core serving smoke cleanup

Core serving verification creates a private tiny-model instance through ordinary
scheduler placement, counted memory reservations and node engine startup. Normal
chat resolution excludes this instance. The maintenance exemption permits only
the accepted job's fixed prompt, immutable model/variant/manifest, exact Studio
runtime and original deadline. Startup and generation use separate lifecycle
checks; revocation prevents a delayed load from reopening an old engine.

Before pinning a Studio, the serving probe checks the existing image/chat
coexistence measurement gate for its verified tiny-model variant. A Studio
with an unmeasured resident image mix is ineligible; an eligible verified peer
may serve the probe. If neither is eligible, the operation stays fenced rather
than forcefully stopping image work or bypassing measured admission. Placement
rechecks the same gate under the ordinary scheduler admission locks.

A dropped gateway response replays an immutable, fresh hash-only result when one
exists. An interrupted attempt is cleaned before a distinct retry, within the
same captured deadline. Source recovery closes interrupted Target attempts for
that same job. Unknown native processes, another target, unfinished stop or an
unverified cleanup receipt keep the counted reservation and maintenance hold.
Inspect the correlated operation, private instance, placement command and node
lifecycle before retrying; never clear their journals or release memory by hand.

Training remains busy after its trainer exits while checkpoint extraction,
replication or adapter validation is unfinished. Native maintenance reads that
lifecycle through the authenticated, node-scoped lease snapshot before unloading
idle engines. Missing or stale lifecycle evidence blocks drain. If completion
requires a new engine load that the maintenance fence prevents, the original
drain budget expires and the upgrade must restore unchanged admission; it must
not unload a validation engine or terminate training to make progress.
When any training job is pending, acceptance captures a drain budget of at most
15 seconds, under the same lock used by new Core admissions. This leaves time
to restore unchanged admission before the trainer's original 60-second checkpoint
commit wait expires. An idle cluster retains its configured drain budget. Replays
retain the accepted budget; a later observation never extends it. Wait for training
to complete before retrying an upgrade that aborts unchanged.

Harness scorecards retain the native environment SHA observed by the Studio
identity worker, separately from the existing tokenizer/harness evaluation digest.
With managed upgrades enabled, Apply requires a passed scorecard for a freshly
observed matching Studio runtime. Placement, container dispatch and run-token
validation recheck the selected Studio. Changed code with identical version
strings, unknown runtime identity, and stale health observations block writes;
reevaluate on the selected runtime. Historical scorecards remain visible, and
infrastructure-error reports do not revoke the last measured pass. Evaluation
workspace preparation and container launch also reject a changed native runtime.
Image coexistence approvals similarly include the observed native SHA and require
fresh health; old profiles remain historical evidence.

The executor's authenticated `/live` check reports only that its supervisor task
is running. Docker uses this process health check during passive handover; it
cannot authorize a managed upgrade. `/ready` separately requires fresh verified
bootstrap evidence and the held kernel writer. A healthy passive candidate or
incumbent therefore remains an independent process without becoming an accepted
recovery owner. A completed supervisor task fails liveness immediately.

Executor image handover has a separate admin initiation/status surface:
`POST /api/v1/admin/upgrades/executor-handover` and the returned status Location.
Initiation requires a registered prepared release, the exact retained incumbent
seed digest and an idempotency key. It returns bounded metadata and retains an
audited initiating actor; it does not grant candidate start or writer transfer.
The API-only CLI exposes the same fixed operations:

```bash
uv run coire upgrades executor-handover --release RELEASE_UUID --expected-incumbent-seed SEED_SHA256 --idempotency-key REQUEST_UUID --json
uv run coire upgrades executor-status HANDOVER_UUID --json
```

Read the incumbent seed digest from the protected retained bootstrap record using
the existing administrative deployment workflow. Reuse the exact request key and
seed digest after an uncertain response; never generate a fresh key to bypass a
pending handover. Status reads do not grant container effects.

Application upgrades remain excluded while the accepted handover is active.
The candidate's authenticated `/passive` proof explicitly reports no recovery
readiness and no writer ownership. Missing preparation/seed, expired observations,
or absent incumbent kernel ownership fail the proof. Retain all journals and both
containers when that check fails. The supervisor retains the original passive,
transfer, stability and recovery epochs across restart. A durable SQL restoration
decision precedes any candidate stop and excludes a competing final commit. The
incumbent is stopped only after the accepted candidate holds the writer and passes
its authenticated, physically attested readiness check. Its container and image
remain restartable. A dispatched create or stop with an uncertain reply is read
back through its exact retained identity rather than dispatched again. Only
positive physical stop observation closes predecessor retirement.

The current four-case composed bootstrap/healthy-handover/candidate-failure/
candidate-restart repeat passed with the original bounds. The full application
restart-boundary, failover and hosted-native CI matrix remains outstanding;
executor network access was approved on 2026-10-11. Feature enablement alone is
insufficient to use executor handover operationally.

#### Metric interpretation

`coire_upgrade_failure_observations_total` counts non-replayed failed receipt observations, and `coire_upgrade_operation_duration_seconds` measures captured acceptance-to-terminal duration. These are diagnostic observations made before transaction commit; use the audited operation and durable receipt journal for authoritative outcomes. Export retry or absence never grants readiness.

`coire_upgrade_maintenance_age_seconds` bounds the age of retained obligations from acceptance, while `coire_upgrade_recovery_age_seconds` measures time since the current recovery-required transition. The dashboard groups these series by bounded kind/phase values. Identifiers belong in scoped structured logs and receipts, not metric labels.

## OrbStack host maintenance

Update the application only during an authorized idle window. Check authenticated production and acceptance node inventories and Core histories first; do not terminate training, images or agents to obtain a window. Preserve private before/after container identities, restart policies, VM state and Docker volume inventories. Use the vendor updater (`orbctl update`); its current interactive updater opens a GUI window. For a manually installed application in an authorized headless update, use the verified Homebrew cask to replace only OrbStack, without reset or zap. Gracefully run `orbctl stop` and `orbctl start`; preserve existing data.

After restarting, verify OrbStack/Docker versions, all retained volumes, Core readiness and service health, and authenticated Studio inventories. Restore previously running containers only after checking exact IDs/images/configuration, and reconcile private SSH bridges using PID birth/UID/argv ownership before removing stale Unix sockets. Do not restore disposable test databases or CI prototypes merely because they were running before the restart: their actual owning validation process and original obligation must still exist. Do not reset historical SQL counters or resource holds to manufacture idle proof. Record the actual tested Docker and Compose versions, since an OrbStack update also replaces those components.

The shared disposable database helper uses `coire-test-postgres-*` names, a
`com.coire.test=postgres` label, owner PID/birth labels, a 512 MiB memory limit,
a 128-PID limit and no restart policy. Normal and exceptional context exit remove
only that exact test database and its disposable anonymous volume. Abrupt process
or host loss still requires an explicit owner-aware post-run census; labels and
expired timestamps alone do not authorize deleting another active fixture.
Verify no unused test databases or local QEMU prototypes remain after validation.
Keep the Mini's production baseline lightweight; optional diagnostics and temporary
acceptance infrastructure must not become undeclared permanent services.

Cross-host lease timestamps remain strict. The lease reader may wait for an already received snapshot's timestamp inside its original two-second fetch budget; it does not issue a replacement observation, adjust timestamps, accept future authority, or extend its original expiry. A frozen or persistently future clock remains unknown and blocks activation.

## Repeat representative workload acceptance

After provisioning an isolated authenticated native fixture and acquiring its
measured assets, prepare private mode-0600 `image-drain-request.json`
(`ImageSubmitRequest`) and `agent-drain-request.json` (`AgentRunCreate`) beside
its protected `fixture.json`. The agent request must reference a fresh existing
workspace/output pair, an eligible measured Studio model and a read-only task
with no tools. Prepare that workspace through the fixture's normal owner; the
test cannot create arbitrary remote paths or bypass model verification.

Run sequentially, with no other upgrade observer mutating the same fixture:

```sh
COIRE_UPGRADE_NATIVE_FIXTURE=/private/operator/native/api/fixture.json \
  uv run pytest -q tests/integration/test_upgrade_native_workloads.py --tb=short
```

Both cases require actual active work before acceptance, a positive authenticated
busy owner census, normal completion before activation, and compatible manual
rollback to the exact Source. New unique private evidence files retain outcomes;
a failed check is not a completed gate. Scoped observer keys are revoked only
after their work is positively terminal. If work or its submission is uncertain,
a private pending-revocation receipt retains the key ID for ordinary cleanup;
revoking an initiating key must not manufacture drain. An observer failure never
grants permission to kill work, clear maintenance or
rewrite a sealed Source. Inspect the accepted job through its normal authority
before running another case.

The native recovery owner reads the kernel session UUID through Darwin's fixed
read-only `sysctlbyname` API, using a 37-byte buffer and the existing one-second
inspection limit within the intent deadline. It does not spawn a kernel-inspection
subprocess. Native command timeout logs include only inspection kind, timing,
exit code and byte counts; argv, executable paths and captured output stay private.
A timeout before ownership still requires the expired unstarted closure protocol;
it never authorizes a new deadline or maintenance release.

A cold owner that retired after Source launch and before archive needs the distinct
cold continuation protocol. Repair the existing ordinary Core and Docker
connections first; an unavailable owner inventory is never idle. Preserve the
original Source child, selector, claim, launch records, probe history and deadlines.
The completed-archive boundary is also supported when its original bytes and
typed intent/effect/receipt agree, current local and peer inventories are idle,
and any prior smoke has a complete receipt matching retained Core authority.
Completed pair results and qualification are preserved as history. An uncommitted
verification request is bound to its exact old smoke, archive and qualification;
it grants no authority. Unresolved pair dispatches and partial or uncommitted
handback boundaries still refuse continuation. The next intent
transfers archived bytes once and runs fresh smoke and distinct pair commands.

After committed Source handback, continuation requires the exact private
verification, handback intent/receipt, supervision receipt and closure intent,
with no closure receipt or resolution. The old support owner and supervisor must
be positively retired. The recorded idle Source guard must still hold its actual
writer lock, and the recorded Source node must remain live. The new grant binds
the released claim and local maintenance preimages. A durable, authenticated
retirement step sends SIGTERM only to that verified sealed Source guard before
acquiring the native writer lock; it never terminates the Source node. Lost
responses reconcile the exact retained retirement marker and positive process
identity. A foreign writer, uncertain retirement or changed preimage refuses.
The released claim, prior maintenance timestamps and original deadlines remain intact.
The legacy Source API cannot inventory a released maintenance epoch. Late metadata
therefore explicitly omits local inventory and grants no idle qualification.
Acceptance reserves a distinct local recovery fence from the global generation
sequence, under the new recovery ID and captured deadline. The native owner saves
the exact previous released fence, installs this separately authorized fence under
actual exclusion, and reads fresh inventory from the live Source API before work.
Local inventories identify this new fence; peer inventories identify the original
held peer fence. Cancellation or uncertainty retains the new fence. Closure
releases only the new local recovery epoch after fresh qualification.
Fresh smoke, new pair commands, data verification and Source supervision are
required again. Closure records a fresh observation of the actual released fence
separately from its original release-effect timestamp.

Use the authenticated admin SDK/CLI to inspect and prepare only the known recovery
schema. `coire upgrades recovery-cold-schema OLD_RECOVERY_ID --json` reads bounded
JSON stdin with `previous_intent_sha256` and `previous_owner_sha256`.
`coire upgrades recovery-cold-schema-prepare --json` takes those digests, the
previous recovery ID, exact observed schema digest and an opaque SHA-256
idempotency value. Preparation changes only the positively matched separate
recovery constraints, retains an audit receipt and changes no operation, fence,
slot, deadline or original phase constraint. Unknown schema refuses.

Run the verified installed support interpreter with
`-I -B -m coire_node.expired_recovery_continuation_observer`, using private bounded
JSON containing the predecessor ID/read digests and catalog support release/runtime.
It reads the exact recorded Source process, verifies its bytes/birth/argv, positively
proves old owner retirement, obtains actual fixture/native exclusion, compares both
data baselines and samples canonical local/peer inventories. Only exact retained
interrupted logical probes are excluded; physical, foreign and unknown owners
still block. Metadata has a 15-second I/O bound and a five-second Core relay. It
creates no execution budget and cannot release maintenance.

After explicit FR-032 approval, `coire upgrades recovery-continue-cold --json` takes
`previous_recovery_id`, previous intent/owner digests, fresh observation digest and
nested ordinary recovery request with one new idempotency key and bounded budget.
Acceptance atomically stores a new intent/grant and truthful predecessor
`superseded` receipt. Original maintenance and active slot remain held; original
history and both prior deadlines stay unchanged. Repeating the exact request
returns the same intent even if the metadata cache vanished. Changed input under
the same key refuses. No automatic retry obtains a budget.

The installed fixed startup producer then starts the new guard. It must obtain
actual lifetime/recovery/fixture locks and verify the immutable continuation
binding again. A recorded `retained_process` is explicit continuation provenance,
not a fresh spawn: PID birth and Source selection time stay unchanged. Missing,
foreign or uncertain children never cause a fallback spawn. Fresh archive,
ten-token serving, exact pair qualification, preservation and handback under the
new intent remain mandatory. A superseded attempt is a failed recovery attempt;
it does not mean Source is serving or the original upgrade succeeded.

For new cold-pair dispatches, the installed support controller retains a typed
private diagnostic at `expired-recovery/<recovery-id>/pair-diagnostics/<command-id>.json`.
It records the actual launcher exit, complete output digest/byte count and at most
8192 characters of output tail. Read it through the existing authenticated
operator host access; keep it outside Git and shared reports. API responses and
structured logs do not include the raw output. A diagnostic is not rank,
qualification, remote cleanup or ownership evidence. Its presence must not be
used to replay that command; a missing typed result retains uncertainty.

`CoireUpgradeColdContinuationAccepted` and the continuation dashboard panel report
an accepted new intent while original maintenance remains held. Schema and intent
metrics are emitted after committed audits. Cancellation and errors retain
obligations; do not delete journals, change PIDs manually or clear fences.


If a cold continuation never reached ownership, its accepted budget and grant
remain historical custody records. Cancel the expired attempt through the admin
API. A subsequent installed observer may traverse at most sixteen immutable
supersession links to the exact previously verified physical Source owner. The
current custodian must retain the active separate slot, have an audited
cancellation, and contain no issued owner, smoke, verification or handback effects.
Both its recorded launcher and support child must be positively retired. The
private namespace permits only the typed startup, launch intent/receipt, bounded
log and launch lock; any native effect marker or unknown state refuses.

Known schema preparation adds only the typed unstarted supersession case to the
separate recovery constraint. It requires cancellation, exact startup intent
equality and absent Core effects; missing JSON evidence rejects. A new once-only
intent binds that startup evidence, atomically supersedes the unstarted custodian
and preserves the physical owner's earlier supersession unchanged. Its startup
barrier allows the recorded Source guard only after checking sealed argv/runtime,
fresh status and its actual writer lock. No old budget or owner is revived.

### Terminal recovery with a retained deployment hold

If a resolved expired recovery reports all database and native maintenance
released but targets still show `cluster_operation_active`, inspect the retained
deployment hold. `CoireUpgradeTerminalHostHold` alerts on a terminal operation
that retains this host hold. Reconcile through the authenticated admin
`POST /api/v1/admin/upgrades/recoveries/{recovery_id}/host-hold`, supplying
`resolution_sha256` from the exact durable resolution. The CLI command is
`coire upgrades recovery-host-hold RECOVERY_ID --json`, with the typed request
on bounded standard input; the SDK provides `reconcile_recovery_host_hold`.
The route requires terminal original history and all released Core fences and
uses the ordinary deployment exclusion locks. Exact replay returns the original
audited receipt. It creates no recovery budget and launches no workload.
Never edit the coordination journal manually. Confirm all three targets are
ready after reconciliation.

Cold closure may carry compact supervision evidence that binds the retained full
supervision receipt and handback by digest and exact process identity. Native
resolution re-reads that full receipt before physical supervision verification.
Both forms retain the fixed 512 KiB transport and journal limit; do not increase
this limit to accommodate duplicated proofs.

### Unpublished native preparation

A new operation that remains accepted with zero receipts and no consumed node
activation command may have an unpublished preparation. Unknown preparation
bytes and disk holds keep maintenance fenced. Request its exact cancellation
through `POST /api/v1/admin/upgrades/{job_id}/unclaimed-cancellation` with the
accepted `plan_sha256`. This authenticated admin action records audit and marks
cancellation requested before returning authority. The native immutable support
producer verifies that authority through the node-authenticated internal read,
positively verifies live Source and its unchanged selection, and archives only
the strict prepared-only namespace under the control lock. Actual bytes and a
captured digest manifest remain under `unclaimed-preparations`; activation,
accepted ownership, receipts, disk holds or unknown processes refuse archival.
The producer then uses the ordinary unstarted cancellation closure. Reconcile
with the regular admin cancellation route and confirm maintenance and host
holds released. `CoireUpgradeUnclaimedCancellationPending` alerts on retained
unstarted cancellation. Do not delete journals or publish a native claim to
force the guard to adopt an unpublished preparation.

### Acknowledge canonical recovery for an older native reader

An older restored Source may retain its original recovery-required receipt and
source-selected intent even after canonical after-deadline resolution. It then
refuses the next native upgrade. Request `POST /api/v1/admin/upgrades/recoveries/{id}/native-terminal`
with the exact canonical `resolution_sha256`; the CLI is
`coire upgrades recovery-native-terminal ID --json`, using bounded JSON input.
This audited action grants acknowledgement of that existing resolution and
retirement of its exact owned staging allocation. It launches no probe, creates
no qualification and grants no new execution budget.

Immutable native support checks the canonical local/Core resolution, actual
Source process and supervision, released claim/fences and the exact mutable
native preimage. It transfers the completed source-serving scope once to the
legacy reader's standard custody, retaining the original attempt/deadline and
actual output evidence. If the original scope already contains different completed
Source evidence, support verifies both exact clean scopes and retired smoke
processes under their ownership locks and records `native-terminal.serving-history.json`
with both byte digests and the canonical resolution. Both namespaces and the
original serving attempt remain intact. Incomplete or changed custody refuses.
It retires only the scoped sparse staging allocation by
its recorded inode/owner. It preserves and hashes the original receipt prefix,
appends linked after-deadline verification/rollback acknowledgements and records
source verification without changing original deadline fields or failure rows.
Loss after custody transfer or either receipt append is replayable against the
same canonical digest. Unknown custody, ownership or altered history refuses
acknowledgement. Verify ordinary upgrade admission and Source rollback afterward.

If macOS renumbered the reservation device across reboot, ordinary release still
refuses deletion. The same terminal grant can preserve the file in
`native-terminal.staging-reservation.quarantine` beside a typed
`native-terminal.staging-quarantine.json` containing the unchanged original hold,
observed identity and boot time. Only a private, singly linked regular file with
the exact recorded inode, size and allocation qualifies; the reboot must postdate
the hold. The atomic move retains all bytes and both filesystem identities.
Replay verifies retained bytes' metadata before retiring the scoped hold. The
`CoireUpgradeStagingQuarantineRetained` alert reports retained disk usage; this
procedure does not reclaim that space. Changed or duplicated files refuse.

Cold recovery also preserves completed smoke history before starting a sealed
legacy Source that counts all smoke scopes against its original 64-scope limit.
When more than 60 scopes are present, the activated recovery owner may archive
at most 64 positively completed, cleaned scopes from earlier jobs into its private
`completed-smoke-history` namespace. It holds each smoke lock, verifies empty
engine journals and absence of actual engine processes, hashes every private
retained byte, and journals the exact move before changing namespace ownership.
Original-operation scopes stay in place. Lost replies reconcile the one remaining
namespace owner and exact bytes. Unknown ownership or changed evidence refuses;
sealed Source code and original receipts/deadlines are never edited.

A cancelled, expired recovery may have a positively ready Source guard after
handback effects but no completed old handback receipt. The fixed continuation
observer captures `ready_unreceipted` metadata only when Core retains the exact
verification and handback intent, the native helper record matches that intent,
both exact Source processes are live, its independent writer lock is held, the
claim is released, original maintenance remains held, and data/qualification
history is unchanged. Later handback, supervision, closure or resolution records
refuse this path. The proof preserves the observed current readiness timestamp;
it never claims the old deadline was met. An explicit authenticated audited new
continuation may retire only that recorded Source guard, install its newly
captured local fence, retain the Source node, and run fresh qualification and
handback under its own budget. Old failure history and deadlines remain intact.

If an explicitly accepted continuation retired that exact keeper but stopped
before cold claim and before applying its new fence, the observer must retain
those declared effects. `keeper_retired` metadata binds the completed retirement
receipt to its audited grant and exact startup, requires positive retirement of
both launch actors and keeper, acquires the free writer lock, and verifies the
original held fence and live Source node. The only allowed fence record is the
unapplied intent; a fence receipt, cold claim, Source projection or unknown file
refuses this preclaim path. The new acceptance supersedes only the failed
custodian and preserves its retirement history. Applying the next local fence
uses the captured old held preimage, under one maintenance lock, with a higher
generation and single-node target; ordinary apply still refuses a different held
owner. No intermediate released state or reopening is published.

The independent guard can reconcile retained expired failure receipts through
`/api/v1/internal/upgrades/{job_id}/native-failure-receipts` even when its Node API
cannot start. The node credential, exact accepted plan/Source/generation and
native expired intent are required. The route accepts only failure history and
uses ordinary atomic receipt/audit ingestion; it grants no success, fence release
or recovery budget. Lost responses replay retained bytes. The dashboard and
`CoireUpgradeNativeFailureReconciled` alert identify this reconciliation.
