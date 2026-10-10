# Feedback operations

Feature [018](../../specs/018-preference-optimisation/spec.md) adds prospective,
explicit contributions. Capture is disabled for each user until they accept the
`feedback-v1` disclosure. Enabling it creates a new generation; historical chats
and withdrawn generations cannot become contributions. Thumbs remain feedback
only. Preference datasets require a completed explicit comparison and an owner
or administrator judgement.

Disabling capture or deleting a conversation immediately excludes its unpublished
contributions from reads, review, replay and export. Maintenance purges the copied
content within 24 hours. Published datasets and trained adapters remain available;
registration is the publication boundary even while dataset analysis is pending.
The contribution controls disclose this limit before acceptance.

## Observe

Use the authenticated Training → Feedback view or `coire feedback exports` and
`coire feedback export-show EXPORT_ID`. History records publication, warnings,
dataset identity and cleanup state. Dataset readiness requires Studio analysis of
both responses using the exact recorded tokenizer/template identity. Core never
loads a tokenizer. Raw contribution rows have no download endpoint.

The Jobs dashboard includes export state counts, overdue exports, pending physical
cleanup and oldest withdrawn source age. The scheduler publishes an independent
baseline heartbeat and explicit zero counts during idle operation. These metrics
and Prometheus alerts continue when diagnostics and new admissions are disabled.
`CoireFeedbackBaselineUnavailable` means the sampler or collection path needs
repair; an absent age sample does not establish that withdrawal is healthy.

Review is administrator-only. The queue separates unreviewed, reviewed and
reviewer-specific skips. An administrator decision never changes the owner's
active answer or context. Conflicts require reading the current shared judgement
version before another explicit decision. Live owner eligibility and administrator
authority are checked again inside the decision transaction.

## Export

Submit a generated-contract request file with `coire feedback export REQUEST.json`.
Bind registered model/variant identities and choose `owner`, `admin`, or
`owner_preferred`. Owner precedence is applied before date and tag filters; an
excluded owner judgement does not fall back to an administrator judgement.
The export preserves exact source, reviewer, generation and version provenance.

An export admits at most 10,000 matching pairs. Zero matches publish nothing;
small samples carry a warning. The durable queue admits one active export and at
most 100 queued requests, with a one-hour queue deadline and a five-minute active
deadline. Final publication locks and revalidates current source versions and
owner eligibility. A changed source rebuilds at most three times. Files and
copied source bodies share the bounded private quota, at most 1 GiB.

## Cancel and recover

Use `coire feedback export-cancel EXPORT_ID`. Cancellation and history work with
training/preference admissions disabled. Do not delete private staging manually
or release its quota hold: scheduler reconciliation must prove physical cleanup.
An uncertain database publication is reconciled against its immutable dataset
identity before any file is removed. Already published dataset files are retained.

For overdue or cleanup alerts, inspect the export's current state and scheduler
health, restore its database/storage access, and let reconciliation finish. Keep
holds counted until removal is proven. The cleanup worker restarts independently
of durable export admission and settles interrupted comparison usage exactly once.
Logs, audit rows, metrics and durable workflow arguments contain metadata only;
do not paste prompts, answers or tag values into operational diagnostics.

## Purge

Owners disable capture in the chat disclosure/settings control or delete the
conversation through the authenticated chat API. The server increments the
capture version and invalidates eligibility in the same transaction. Re-enabling
creates a fresh generation, with no restoration of old copied content.

If `CoireFeedbackPurgeOverdue` fires, restore scheduler/database access immediately
and inspect the bounded maintenance lane. `COIRE_FEEDBACK_PURGE_BATCH_SIZE` defaults
to 100; `COIRE_FEEDBACK_STORAGE_QUOTA_BYTES` defaults to 1073741824. Both values
have those maxima. No feature flag disables withdrawal maintenance. Verify the
oldest age clears and cleanup receipts prove removal; do not treat disabling new
capture as completion of the physical purge.

## Rollback

Disable new preference/training admissions, cancel or drain queued exports and
comparisons, and leave current cleanup/reconciliation running until staging holds
are released. Drain all trainers and evaluation-owned pauses using
[preference training operations](preference-training.md#rollback). Preserve
published datasets, lineage, audit and adapter artifacts. Take the usual database
backup before any schema change. Binary rollback and migration downgrade are
separate operations; use the recorded isolated downgrade rehearsal rather than
dropping live contribution tables to recover an older binary.
