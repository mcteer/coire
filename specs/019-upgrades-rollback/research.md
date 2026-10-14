# Research: 019 upgrades and rollback

Date: 2026-10-10. Baseline: merged 018, `3a872ef8f82b2a0c73969bc433b4616d079227c4`.

## 1. Extend the installed release mechanisms

**Decision:** Reuse immutable Studio installation and source-independent Core deployment records. Add full SHA-256 content manifests, compatibility and managed-operation receipts.

**Evidence:** `apps/coire-node/install.sh:132` already hashes locks, wheels and interpreter identity and supports stage-only installation. `install_runtime.py:95` performs import/CLI smoke and atomic selection. `scripts/coire-deploy.py:51` freezes resolved Compose configuration, cluster files and first-party image IDs; `packages/coire-core/src/coire_core/deploy.py:124` reuses unchanged credential generations.

**Rationale:** Rebuilding installation would discard existing integrity, project-isolation and secret-preservation work. The short environment directory suffix is a display label, not sufficient execution identity. Installed package version is also insufficient.

**Alternatives:** In-place package updates; floating tags; source-checkout-dependent recovery. All fail reproducibility or reliable restoration. Preserve current installer support for controlled interpreter symlinks; reject escaping artifact links rather than rejecting every virtualenv symlink.

## 2. Independent recovery owners

**Decision:** Add a hardened `coire-upgrade-executor` control-plane container, separate from the application cohort it changes. It executes accepted fixed plans and recovers from its durable journal across API, scheduler, executor and host restart. DBOS remains solely in the scheduler. On Studios, a minimal coire-node recovery helper runs from a retained bootstrap interpreter through a launchd plist, before ordinary node resumption admits work.

**Rationale:** The process being replaced cannot be its only rollback owner. Container placement complies with Principles II and II-a. Studio helper state is a recoverable execution receipt, not an independent authority; PostgreSQL retains accepted operation authority. Core bootstrap uses existing administrative deployment tooling.

**Alternatives:** API background tasks and scheduler-only execution disappear during their own upgrade. A persistent host daemon conflicts with containerized orchestration. A detached old-node child survives agent death but not reboot. A bounded host script alone cannot promise reboot recovery.

**Bootstrap decision:** Managed upgrades stay unavailable until recovery support is installed and verified. Initial installation has no predecessor to restore and must fail closed if interrupted. Subsequent executor replacement retains the incumbent executor as an independent supervisor until the candidate proves health, journal access and fenced ownership; both have restart policies. The candidate starts passive. A durable handover record selects the sole writer and authorizes the incumbent to restore its own known-good image if handover fails. This small bootstrap protocol is separate from application rollouts; it must itself pass restart-boundary tests.

## 3. Constrained Docker operations

**Decision:** Use the existing proxy endpoint with an executor-enforced exact method/path/target allowlist. Support container inspect/create/start/stop/remove, bounded logs, and local image inspection only. No arbitrary Compose, Docker exec, image pulls/builds, registry authentication, volume/network administration or pruning.

**Evidence:** `deploy/compose/compose.yaml:397` enables container/image prefixes and write methods while disabling unrelated API families. Research verified the pinned proxy's prefix filtering: `EXEC=0` does not exclude `/containers/{id}/exec`, so the executor needs its own stricter allowlist. Existing-network attachments can be supplied in ContainerCreate; aliases must preserve gateway routing. The Docker Engine v1.47 [API specification](https://docs.docker.com/reference/api/engine/version/v1.47.yaml) defines these creation fields; isolated tests must prove compatibility with the deployed daemon.

**Rationale:** No proxy flag needs widening. Translate an explicitly supported frozen Compose subset; reject unknown fields, topology changes and unprepared images. Verify mounts against retained source containers. Retain those containers until commit to pin named-volume references; use no-copy volume mounts and reject new volume names. Structured bind mounts must not create missing host paths.

**Permission boundary:** Adding the executor to `coire-docker`, its API control network and telemetry is a concrete new network membership. The existing AGENTS.md rule says “Do not widen a CORS, network, capability, or firewall setting to make something work; ask.” Planning records the exact proposed memberships; implementation must obtain authorization before applying them. No proxy flag, public listener, host socket mount, capability or firewall expansion is proposed.

## 4. Shared deployment ownership

**Decision:** Replace the host-PID-only project lock with typed host/executor ownership, boot/process identity, operation generation and a durable release-selection receipt. All `coire-up`, `coire-down`, cleanup, bootstrap and executor mutations honor it.

**Evidence:** `deploy/compose/coire-up:32` and `coire-down:15` share a mkdir/PID lock. `scripts/coire-deploy.py:197` atomically selects `current`. A container PID is not a host PID.

**Rationale:** The executor must not race later Compose commands or secret cleanup. Mount only a dedicated coordination directory read-write and frozen release records read-only; do not mount the secret-generation root into the executor. Executor commit writes the selected release receipt atomically. Host commands reconcile that receipt to `current` under ownership before any operation. Retained release references protect source credentials until no active or rollback record needs them.

**Alternative:** Let the executor run `docker compose up` or write the entire project root. This exceeds the fixed-operation boundary and could replace unrelated services.

## 5. Maintenance is an admission protocol

**Decision:** Persist a cluster operation fence and per-target maintenance generations before strict inventory. Core admission and every node side-effect owner check the same operation/generation; affected nodes acknowledge the fence before drain. A missing acknowledgement blocks activation. Expiry does not silently reopen admission. Recovery reconciles authoritative and local state first.

**Evidence:** Core canonical admission locks are in `coire_api/placement/service.py`. Node engine/image/reservation/training owners share an RLock at `agent.py:669`, but acquisition, runs and shards have separate owners. `metrics.py:410` catches collection failures and returns empty health inventories; this is unsuitable as proof of idle. Training unreleased records remain active (`training/journal.py:97`), analysis uses stop-before-release (`analysis_supervisor.py:69`), imports are restart-visible (`importer.py:131`), and reservation cleanup requires checked release.

**Rationale:** A sampled health check cannot close races. Include engines/orphans, requests and renewal obligations, shards, training, measurements, analysis, extraction/import/transfer, evaluation, image workers, harnesses, workspace/tokenizer work, acquisition/conversion/replication, probes, feedback export/erasure and failover. Standby reservations are classified explicitly and remain held; unexplained holds block. Core maintenance drains workloads whose safety depends on scheduler renewal before restarting the scheduler. A cohort that changes only unrelated services uses its explicit dependency scope, keeping unrelated chat live. Never clear unknown holds to make progress.

## 6. Real smoke and qualification identity

**Decision:** Use an already admin-acquired, registry-verified tiny text model (at most 1 GB) and explicit ten-token generation, with owned PID/port/memory/disk accounting and positive cleanup. Post-activation repeats serving smoke. A configured paired capability requires the existing real two-rank collective; both participants are fenced briefly for it. A failure restores the changed Studio; the peer's release is untouched.

**Evidence:** `link_probes.py:47` launches bounded collective work. Current runtime hashing (`training/datasets.py:373`, `rendering_identity.py:81`) uses only dependency versions, and hardware identity includes the same package version (`agent.py:253`). Same-version wheel changes can therefore preserve stale measured readiness.

**Rationale:** Bind qualification to the full installed release digest plus existing model/hardware identities. Preserve historical rows and published artifacts. Successful basic smoke allows ordinary measured-capability-independent serving; training/evaluation/Apply and paired placement retain their existing qualification gates. Full remeasurement is a separate existing workflow, not something the upgrade fabricates.

## 7. Rollback compatibility and preservation

**Decision:** Release metadata declares supported source digests, schema heads readable after migration by source and target, native journal protocol ranges, data/privacy semantics, bootstrap protocol and required smoke. Automated rollback never runs production Alembic downgrade or restores a backup over newer data. Backup preflight requires a protected, readable, current pre-migration backup receipt matching project and source schema, with successful validation evidence.

**Evidence:** Migrations 0031, 0032 and 0033 deliberately reject destructive downgrades while history remains. `docs/runbooks/preference-training.md:97` explains why 018→017 additionally requires preserving v3 native records and audited privacy quarantine; those manual transformations are outside automatic rollback.

**Rationale:** A version number is not compatibility proof. Keep published dataset/adapter bytes, capture opt-out generations, unexported purges, private erasure obligations and training/evaluation history. Backup existence is an operational recovery prerequisite, not permission to destroy newer state. Unknown partial migration outcome stays fenced for an explicit runbook recovery.

## 8. Availability and failover

**Decision:** Use the user-confirmed bounded maintenance policy in A1 with proposed defaults: 15-minute drain, 120-second readiness per service/target, 300-second restoration and successful gateway-unavailability budgets. Timings are typed configuration, captured on acceptance; bounded overrides cannot relax safety prerequisites. Start an explicit maintenance state before drain, return retry guidance while ingress is up, reconnect durable progress when it returns.

**Rationale:** Single gateway replacement cannot promise uninterrupted availability. Existing core/Studio election and hand-back remain authoritative (`coire_api/failover/participant.py`, `coire_node/failover/`). An active emergency frontend blocks Studio activation. Core recovery reopens admission only after valid lease/hand-back; no bypass or disabled failover timer is introduced.

**Alternative:** Zero-downtime gateway migration would require a separately specified traffic handoff architecture; it is not silently inferred from per-service Compose independence.

## 9. Baseline and validation

**Decision:** Plan against merged 018 with a new migration after `0033_preference_feedback` (currently 0034), retain all older-worktree edits untouched, and rerun every required gate on the implementation branch. No new third-party dependency is needed; use existing Python, Pydantic, httpx, SQLAlchemy and DBOS. New container packaging is a new workspace service with its own image.

**Evidence:** `/Users/mcteer/Projects/coire-019` has substantial uncommitted work on obsolete `3676e975`, including an upgrade preflight that reports zero training jobs unconditionally and migration numbering now occupied by 016–018. Its old completion marks and tests cannot validate this design.

**Validation:** CPU contracts and race/recovery tests; disposable PostgreSQL and Compose tests; isolated native tiny-model CI; real Studios for measured drain, exact runtime rollback, paired collective and post-upgrade qualification. Diagnostics-off audit/alerts, secret-generation preservation, project isolation and executor image build/scan are release gates.
