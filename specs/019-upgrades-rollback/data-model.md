# Data Model: 019 upgrades and rollback

All wire models live in `packages/coire-core/src/coire_core/models/upgrades.py`, forbid unknown fields, use timezone-aware UTC timestamps and bounded collections/strings, and reuse existing node/model/job identifiers. The names below are design contracts. DB tables are additive in migration 0034, subject to verifying the current migration head.

## Release and compatibility

**UpgradeRelease**: opaque `release_id`; `kind` (`studio` or `control_plane`); display version; full `manifest_sha256`; source revision; platform; preparation time/actor; preparation receipt; status (`prepared`, `invalid`, `retired`); compatibility; required smoke checks. Content is immutable after registration. Retirement excludes selection and never deletes referenced artifacts.

Studio manifest: lock/wheel/interpreter digests, expected dependency versions, full environment digest, bootstrap/journal protocols and internal artifact keys. Core manifest: project, frozen Compose digest, first-party service names with exact local image IDs/content digests, expected schema head, supported service configuration subset, source release compatibility and bootstrap protocol. Server-resolved artifact keys never become caller-provided paths.

**UpgradeCompatibility**: explicit supported source manifest digests; source and target readable schema sets; native training/evaluation/feedback protocol ranges; privacy semantics version; bootstrap/executor protocol range; required link/smoke capabilities; evidence digest. Both old and new binaries must accept retained post-migration state. Reject unspecified compatibility. Do not infer it from semver.

**BackupEvidence**: opaque protected backup ID, project, source schema, created time, content hash/size, validation receipt and validation time. The migration preflight requires a successful restore/read validation in an isolated database and a backup at most 24 hours old matching the source schema and project. Backup paths and bytes are never exposed in API responses. Extend the existing host deployment tool with a separate backup-and-validation command that does not change database credentials. Preparation completes before operation admission; absent or stale evidence refuses the upgrade. The receipt discloses backup age and does not authorize automatic restoration over newer data.

## Installed target and qualification

**UpgradeTargetStatus**: node ID or fixed control-plane cohort; desired/observed/source release IDs and full digests; observed per-service image identities; readiness (`ready`, `maintenance`, `recovering`, `not_ready`, `drifted`); blocking reasons; latest observation time; retained rollback eligibility; qualified runtime identities; executor/bootstrap readiness.

A runtime digest participates in current qualification lookup. Historical training measurements, evaluation results, adapter rows, published dataset hashes and link observations remain immutable. Existing qualifications without the required identity cannot authorize newly changed runtime work. Rollback to an exact old identity can reuse evidence only when the existing model/hardware/OS/recipe validity rules also pass; otherwise requalification is required.

## Operation and stages

**UpgradeOperation** (joins the existing job envelope): ULID `job_id`; kind (`upgrade`, `rollback`, `bootstrap_handover`); target; source/target release IDs and digests; initiating user/key IDs; actor entitlement generation; idempotency key plus canonical request hash; operation generation; selected cohort order; immutable timing policy; accepted/started/finished times; current phase; outcome; cancellation request; blocking reason; active maintenance IDs; last reconciled receipt sequence. Bootstrap handover is an internal deployment subtype, not an arbitrary UI target.

A unique partial database index permits one nonterminal cluster operation. An idempotency record is unique per actor and key: same request returns the original operation, different request returns conflict. Rows and audit survive retirement/cleanup. Compare-and-set phase changes require the expected operation generation.

**Phases**:

`accepted → preflight → fencing → draining → staged → smoke_before → activating → verifying → qualifying → committing → succeeded`

- Node staging may prepare immutable files before the admission fence, with a disk reservation; Metal smoke and activation always follow drain. `staged` denotes verified staging at the durable operation boundary.
- Core follows `draining → staged → migrating → activating → verifying → committing`. `migrating` is omitted when schema is unchanged. Core `smoke_before` is replaced by artifact/config verification and migration gates.
- Before any runtime activation intent: failure produces `failed_unchanged`; cancel produces `cancelled_unchanged`, after owned cleanup and source reconciliation. This outcome means binaries were unchanged: a separate migration outcome explicitly reports any retained additive schema change. Unknown schema state cannot use this outcome.
- Once runtime activation intent exists: failure/cancel produces `restoring → verifying_source → rolled_back`. `rolled_back` records the failed/cancelled upgrade reason separately; it is never an upgrade success.
- Uncertain external effects, schema state or failed source verification produce nonterminal `recovery_required`. Keep maintenance and ownership; autonomous recovery may resume only with positive evidence. An authenticated administrator can acknowledge an alert but cannot release the fence through this feature without verified recovery.
- Manual rollback is a new operation with a retained compatible release as its destination; successful requested rollback ends `succeeded`, distinguishing it from automatic recovery of a failed upgrade.

## Maintenance and strict inventory

**UpgradeMaintenance**: target scope (cluster, node or fixed service-dependency scope); job ID; generation; state (`fencing`, `draining`, `active`, `recovering`, `released`); desired and acknowledged node generations; initiator; phase deadlines; last reconciliation. Generations increase monotonically. Lease expiry alone never releases maintenance. Order canonical node locks deterministically before persisting target fences.

**UpgradeInventory**: observation time; exact node/runtime identity; maintenance generation; owner reports, work IDs, kind, active/terminal-but-cleaning state, renewal obligations, process identities and resource holds. Each owner reports `complete`, `blocked` or `unknown`; all required owners must be complete. Standing reservations are explicit non-work entries and remain reserved. Collection exceptions cannot become empty/idle reports.

## Execution plans, journals and receipts

**UpgradeExecutionPlan**: protocol version; job/project/target; owner generation; manifest digests; fixed action enum; source observed identities; allowed service set/order; smoke asset registry identity; maintenance generation; authorization expiry; captured deadlines; bounded evidence limit. No command strings, source paths or caller-supplied environment overrides. The plan is immutable and authenticated before it is journaled.

**UpgradeJournalEntry**: monotonic sequence; job/generation; phase; effect ID; intended or observed effect; actual source/candidate container or process identity; content hashes; deadline; previous entry digest. Writes use private ownership, atomic replace and fsync; source and candidate must not share mutable runtime files. Corrupt or truncated state blocks normal admission and preserves all evidence.

**UpgradeReceipt / UpgradeEvent**: job/generation; monotonically increasing sequence; stable effect ID; phase; occurred time; outcome/reason; bounded redacted evidence reference; installed identities; restoration status. The receipt uniqueness key is `(job_id, generation, sequence)`; same sequence/different content is a conflict and alert. Each source journal retains unacknowledged receipts until durable database acknowledgment. Audit ingestion is idempotent.

**DeploymentOwnership**: protocol version; project; host/executor owner kind; operation ID/generation; host boot ID and process birth identity or immutable container ID; journal reference; source and committed release digests. No cross-namespace PID liveness assumptions. A matching terminal commit receipt, not merely owner death, authorizes `current` pointer reconciliation or credential pruning. Source containers and their existing volumes stay pinned until rollout commits.

**BootstrapHandover**: incumbent/candidate executor image and container IDs; expected protocol; source/candidate health observations; sole-writer epoch; passive/start/claim/verify/commit phases; stability deadline; restoration authority. Both sides read the same durable record. The incumbent retains bounded recovery supervision until commit, including when the host restarts. Split ownership or unavailable journals yields maintenance, never two executors.

## Retention and migration

Keep current and last compatible release plus every nonterminal-operation reference; preserve credential generations and local image/environment artifacts until all references clear. Cleanup only deletes owned staging remnants after positive inactivity. Persist audit and terminal operation summaries according to existing audit retention; bounded diagnostic payloads may expire independently.

Migration 0034 creates release, operation-specific, maintenance, stage/receipt and ownership metadata with foreign keys, uniqueness and bounded enum constraints. Extend existing jobs/audit tables through their established patterns. Downgrade is reversible in an empty disposable database but refuses to erase populated operational or audit history. Production rollback retains the additive schema. No changes to dataset, adapter or feedback retention policy are authorized.
