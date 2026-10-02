# Image storage preparation

Image admission stays disabled by default. The API is the sole owner of the
`coire-blobs` volume at `/opt/coire/blobs`. The file worker processes image inputs in
dedicated `images` subpaths under the existing chat original/derived volumes; it does not
mount final blobs. The scheduler has no blob mount.
On API startup, an existing blob volume owned by the API UID is tightened to
mode `0700` before maintenance begins. A symlink or foreign-owned mount is refused.
If startup refuses the mount, inspect its owner and type before correcting the
deployment volume; do not broaden permissions to make the sweep pass.

## Inspect

Render production and integration Compose configurations and verify `IMAGE_ENABLED=false`.
Check the API-only blob mount, existing file-worker original read-only mount, and the
route-specific Nginx body limits. Once services exist, inspect quota rows, transfer receipts
and disk safety floor before any admission change. Do not log or expose blob paths.
The image quota helper now initializes and locks global then owner rows under one
transaction advisory lock. A caller reserves worst-case bytes before publication,
then settles only confirmed stored bytes or releases a failed hold in that same
transaction as the owning input/job row. The physical disk floor counts existing
unwritten holds. Inspect both `held_bytes` and `stored_bytes` when diagnosing a
refusal; never reset them manually while a job or purge is uncertain. Local
cross-process PostgreSQL contention tests have passed; production placement and
coexistence acceptance remain open.
The gateway reconciles a ready legacy chat engine into the shared node ledger
before granting inference. It refuses the request if the node budget or an image
worker prevents that hold. The reconciler releases the hold after the node
reports a terminal engine state. If chat returns `chat_model_unavailable`, inspect
the engine row and node memory reservations together; do not clear a live hold.
Every five minutes, API maintenance compares global and owner `stored_bytes` with
unpurged published output rows and ready/deleting input rows under the quota lock.
Inspect `coire_image_quota_drift_bytes`, the Image stored quota reconciliation panel,
and `CoireImageStoredQuotaDrift` if the difference persists. This check only reports
drift; it never changes a quota or deletes bytes. Inspect output/input purge state and
any retained staging before repairing a counter through an audited operator procedure.
Every five minutes, maintenance also verifies one bounded page of retained published
output files against their size and SHA-256 receipt, private file mode and single-link
ownership. A missing or altered file increments
`coire_image_output_integrity_failures_total`; inspect the Image retained output
integrity panel and `CoireImageOutputIntegrityFailure`. The check is read-only and
does not free quota. Investigate the output ID in the structured log, preserve the
database row and inspect the paired blob backup before restoring bytes.
The image-job capacity helper now reserves one pending slot, daily output allowance
and the worst-case `n * 64 MiB` output hold in the same transaction as a future job
row. At start it moves held outputs to consumed outputs and frees the queue slot;
queued cancellation frees the slot, allowance and byte hold. UTC day rollover resets
consumed outputs while preserving in-flight held outputs. The native submit route
uses these helpers when image admission is enabled. Inspect owner/global `pending_jobs`, `held_outputs`,
`consumed_outputs` and `held_bytes` together when diagnosing admission refusal.
The scheduler also expires a job after its 30-minute queue deadline if it has no
selected node, instance, reservation, fence or cancellation intent. Expiry writes a
`queue_timeout` terminal event and audit row while releasing the same pending slot,
allowance and byte hold. Inspect `coire_image_queue_expiration_total` and the
`CoireImageQueueExpiryFailures` alert if a past-due job remains queued. A job with
uncertain placement retains its holds for fenced reconciliation; do not reset its
quota counters by hand.
The queued image-admission service now commits one job, generated seed, capacity hold,
first event and content-free audit row for a new owner/idempotency key; a matching
retry returns the existing job without charging again. It accepts only ready,
published Studio image bases with a measured profile and valid manifest digest,
plus ready hidden dependencies. The public native route exists, while the default
`COIRE_IMAGE_ENABLED=false` setting refuses new submissions. Inspect
`image_jobs.intent_sha256`, the `authorization_snapshot` policy and quota counters
together when investigating a replay conflict; never copy prompts from
`submitted_spec` or `resolved_spec` into logs or audit details.

The owner `POST /api/v1/image-inputs` path uses private staging for recipe,
init, mask and control purposes. It commits the input row and quota hold in
one admission transaction; `GET /api/v1/image-inputs/{id}` returns the owner's
processing/ready/failed state. Recipe processing extracts settings without
decoding image pixels. Admission still requires
`COIRE_IMAGE_ENABLED=true`, which remains false by default. The scheduler scans
committed processing rows and starts `coire.image.input.recipe` or
`coire.image.input.normalize`, according to purpose, with the row's processing
ULID. Successful processing stores the strict recipe or normalized input and settles
the hold once. Worker outages remain processing for retry. A stable parser refusal
sets `failed`; maintenance purges its original before releasing the hold. Do not
adjust counters manually. Inspect `coire_image_input_processing_total` and the
`coire.scheduler.image_input.parse` span without recording recipe text.
If the upload commit outcome is uncertain, the generated original is retained so a
committed processing row can recover; maintenance reconciles unreferenced files
before their bytes can be considered free.
The API maintenance loop now purges failed recipe input originals before releasing
their quota holds, while preserving the failed status for owner polling. It also
lets a live owner delete an input referenced by a job after that job's image-model
or explicit entitlement is revoked: the delete transaction requests fenced
cancellation of each owned active job, then tombstones the input. The input bytes
and their stored quota remain until every reference is released and physical
cleanup succeeds. Inspect the job's `cancelling` state before diagnosing a
deletion that has not reached `purged`. The maintenance loop also removes
generated-key originals and `.uploading` files older than one hour only
after a quota-lock-protected database check finds no committed input row. It skips
symlinks, unfamiliar names and recent files. Inspect
`coire_image_input_cleanup_total` and `coire_image_input_purge_oldest_seconds`; the
Images dashboard and alerts `CoireImageInputCleanupFailures` and
`CoireImageInputPurgeOverdue` cover repeated failures and the 24-hour deadline.
Input orphan inventory stops after 4,096 directory entries. Deleted and failed
input purges advance through 25-row keyset pages, so inspect the oldest pending
age rather than assuming every damaged row is retried in the same pass.
If cleanup fails, keep the row and hold for retry; do not remove the original volume.
Owners can also `DELETE /api/v1/image-inputs/{id}`. A 202 response commits the
tombstone before returning; subsequent status reads hide the input. Active owned
jobs receive a fenced cancel request in the same transaction. An inconsistent or
unbounded reference set returns 409 and retains the input for investigation. Maintenance
removes tombstoned recipe originals and releases held or settled storage only after
the generated regular file is absent. Inspect `coire_image_input_cleanup_total` with
`kind="deleted"` and the oldest pending input gauge. Repeated deletion is safe,
including after purge. Leave the original volume mounted until pending deletions
reach `purged`; restore `COIRE_IMAGE_ENABLED=false` to stop new uploads.

The isolated file worker has a `parse_recipe_png` helper for owner-scoped recipe
uploads. It accepts regular PNG files up to 64 MiB, validates PNG chunk
framing and CRCs, and extracts at most 64 KiB of uncompressed `coire.image` iTXt JSON into
the strict `ImageRecipe` model. It never decodes IDAT or promotes the upload to a generation
source. Inspect only its stable error codes (`invalid_png`, `invalid_recipe`,
`recipe_too_large`, `unsupported_recipe_encoding`, `duplicate_recipe`,
`missing_recipe`, `recipe_input_too_large`, `recipe_input_unavailable`). The API upload
path stages an immutable owner-scoped file and enforces purpose-specific limits before
calling it. The private staging primitive streams into a generated temporary key, checks actual bytes against
the 10 MiB generation or 64 MiB recipe cap and the declared count, then hashes and
fsyncs. Admission calls its exclusive `publish()` only after owner quota and DB
checks; it calls `discard()` after refusal. A failed or cancelled staging read removes
its temporary file. The orphan sweep removes only old unowned generated files after
checking durable input rows.
The file worker's private `POST /v1/image-recipes/parse` handoff requires its dedicated
service token and a generated input UUID, expected byte count and SHA-256. It reads the
configured read-only `images` namespace, verifies size and hash on one descriptor, and
returns the strict recipe without decoding pixels. It refuses concurrent file conversions.
Inspect `coire_image_recipe_parse_total` by its fixed outcome label and the
`coire.file_worker.image_recipe_parse` span. A 422 response carries only a stable parser
code, including `recipe_input_mismatch`; never log image metadata or source bytes.
The API's typed `FileWorkerClient.parse_image_recipe` call now checks that the returned
input UUID, byte count and SHA-256 match the request before a scheduler may persist
the recipe. A busy worker is retryable; a parser refusal is a stable input failure;
transport and malformed-response errors require investigation. The client never logs
the service token, recipe or worker response body.

Owners can restore direct settings from a ready recipe input with
`POST /api/v1/image-inputs/{id}/recipe`. The response reports missing original input
digests and model dependencies; optional replacements must be ready, owner-held
inputs with matching digest, dimensions and purpose. The response does not acquire
models or submit a job. It always reports `exact_reproduction_available=false` with
`runtime_environment_unverified` until the original runtime and hardware fingerprint
can be compared. Inspect `coire_image_requests_total` for the fixed `recipe_import`
operation; do not log the returned prompt or recipe. A later generation request must
pass current authorization and admission checks again.

The private output gallery metadata routes are now available at
`GET /api/v1/image-outputs` and `GET /api/v1/image-outputs/{id}`. They require a live
human or personal image key, return only the caller's published, nondeleted records,
and include no blob path or content URL. The opaque cursor carries owner and position;
it remains valid if the boundary record is deleted. The gallery remains readable when
new image admission is disabled. Inspect the `gallery` operation in
`coire_image_requests_total` for success/refusal rates and the `coire.api.image.gallery`
span for request timing.
After every transfer receipt and Studio scratch cleanup acknowledgment is durable, the
image scheduler rechecks the selected node, execution lease, current registry manifests,
owner/key access and each staged PNG. It then publishes all output rows and settles the
worst-case byte hold in one database transaction. The immutable private staging keys
become the final blob keys; no file rename can leave a partly visible batch. Standard
images remain tagged `unknown` until the classifier path is operational, and no shared
access is available for that tag. The same transaction writes an `image.complete` audit
row with the originating identity, entitlement names and output count; it excludes
prompt and image metadata. Inspect `coire_image_publication_total`, the
`coire.scheduler.image.publish` span and `CoireImagePublicationFailures` for retries.
If publication is uncertain, leave the job and held bytes intact for the deterministic
workflow to reconcile. Do not remove staged files or manually release a hold.
The API maintenance loop visits 25 job directories per pass and removes only `.uploading`
transfer files older than one hour under private staging directories; it leaves durable
`0.png`–`3.png` files for job recovery and published downloads. Inspect
`coire_image_purge_total` with
`kind="transfer_temp"`, the `coire.api.image.transfer_temp_sweep` span and
`CoireImageTransferTempCleanupFailures`. A symlink, unfamiliar directory or excessive
entry count stops that sweep for inspection without following external paths.
The same loop retries no-follow cleanup of every known staging attempt for
failed or cancelled jobs older than one hour. It pages through terminal jobs,
locks each job row and leaves any job with a published output untouched.
Inspect `coire_image_purge_total{kind="terminal_staging"}` for actual removals
or failures. A failing attempt remains for inspection; do not clear its quota
or delete an unfamiliar file manually.
It also pages through private staging directories older than one hour, deleting
recognized attempts only when neither a durable job nor an output row owns the
ULID. Recent files, unfamiliar entries and symlinks remain untouched. Inspect
`coire_image_purge_total{kind="orphan_staging"}` and
`CoireImageOrphanStagingCleanupFailures` if orphan removal fails.
If the originating key or an entitlement is revoked, or a pinned registry manifest is
retired before publication, the scheduler refuses publication. Once every node cleanup
acknowledgment is present, it removes the private transfer staging, releases the byte
hold and records a safe terminal failure plus an audit row. Inspect the `denied` outcome
on `coire_image_publication_total` and the job's `safe_failure_code`; a missing node ack
keeps the hold for recovery. Do not delete those files or quota rows by hand.
Owners can now DELETE an output. The API commits a tombstone before returning 202;
subsequent gallery, grant and content requests treat it as absent. The API maintenance
loop checks pending tombstones every 30 seconds, unlinks only regular files below its
private blob root without following symlinks, then records `purged_at` and releases
owner/global stored-byte counters. A missing file is retry-safe after an unlink-before-
commit crash. A failed purge remains pending; inspect `coire_image_purge_total`,
`coire_image_purge_oldest_seconds` and the `CoireImagePurgeOverdue` alert. The
output purge advances through 25-row keyset pages so one damaged early batch
does not starve later tombstones. It wraps after the last pending row and retries.
To stop new output creation keep `COIRE_IMAGE_ENABLED=false`; let maintenance
finish before removing the blob volume or rolling back the schema.

Owners can now issue five-minute download grants via
`POST /api/v1/image-outputs/{id}/download-grants`. The response URL has a `#grant=`
fragment. Clients must parse that fragment locally and send the token as
`X-Coire-Image-Grant` with their normal credentials to
`GET /api/v1/image-outputs/{id}/content`; navigating to the URL alone cannot fetch
bytes. Do not put the token in a query string or log/capture this header. The API
rechecks owner, current key and explicit entitlement at issue and redemption, then
opens and hashes the blob under the configured root without following symlinks.
Content and grant responses are `private, no-store`. Investigate failed downloads via
the fixed-label `download` operation metric and `coire.api.image.download` span; the
refusal details and blob key are never exposed. Keep image admission disabled to stop
new output; existing owner downloads remain available. A rollback removes these
routes but preserves existing grant rows until expiry.

## Stop and roll back

Keep or restore `COIRE_IMAGE_ENABLED=false`. Drain/cancel image workers and reconcile
pending transfers before changing mounts. Revert the Compose and Nginx change to restore
the previous topology; preserve the volume until owner records, tombstones and physical
purge are reconciled. Never remove the volume as a schema rollback shortcut.
