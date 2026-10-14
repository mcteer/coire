"""Independent fixed-deadline executor intent data; replay always requires reality reconciliation."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

from coire_core.errors import UpgradeConflict, UpgradeForbidden, UpgradeUnavailable
from coire_core.models.upgrades import (
    TERMINAL_UPGRADE_PHASES,
    CoreExecutorEffect,
    CoreExecutorEpoch,
    CoreRuntimeSelection,
    CoreServiceWindow,
    CoreSourceSnapshot,
    DeploymentOwnership,
    DeploymentSelectionReceipt,
    ServiceName,
    UpgradeActivationConsume,
    UpgradeComposeMigration,
    UpgradeDockerContainer,
    UpgradeDockerMigration,
    UpgradeEvidence,
    UpgradeExecutionPlan,
    UpgradeKind,
    UpgradeMigrationExecution,
    UpgradeOperation,
    UpgradePhase,
    UpgradeReceiptAck,
)
from coire_core.upgrade_artifacts import read_prepared_compose
from coire_core.upgrade_journal import PrivateUpgradeStore, UpgradeReceiptJournal

from .ownership import ExecutorAuthority


class ExecutorJournal:
    def __init__(
        self, root: Path, plan: UpgradeExecutionPlan, *, prepared_root: Path | None = None
    ) -> None:
        if plan.operation.kind is not UpgradeKind.CONTROL_PLANE:
            raise UpgradeForbidden("Executor journal requires an accepted Core plan")
        self.prepared_root = prepared_root
        self.store = PrivateUpgradeStore(root)
        self.receipts = UpgradeReceiptJournal(self.store.path("receipts"), plan)
        self.effects = PrivateUpgradeStore(self.store.path("effects"))
        self.plan = self.receipts.plan
        with self.store.ownership():
            state = self.store.read("epoch.json", CoreExecutorEpoch)
            if state is None:
                state = CoreExecutorEpoch(
                    job_id=plan.operation.job_id,
                    generation=plan.operation.generation,
                    plan_sha256=plan.authority_sha256(),
                )
                self.store.write("epoch.json", state, replace=False)
            self._validate(state)

    def _validate(self, state: CoreExecutorEpoch | CoreExecutorEffect) -> None:
        if (state.job_id, state.generation, state.plan_sha256) != (
            self.plan.operation.job_id,
            self.plan.operation.generation,
            self.plan.authority_sha256(),
        ):
            raise UpgradeConflict("Executor journal belongs to another accepted epoch")

    def state(self) -> CoreExecutorEpoch:
        state = self.store.read("epoch.json", CoreExecutorEpoch)
        if state is None:
            raise UpgradeUnavailable("Executor epoch is absent")
        self._validate(state)
        return state

    def retain_runtime_selection(
        self, selection: CoreRuntimeSelection, authority: ExecutorAuthority
    ) -> CoreRuntimeSelection:
        """Fsync the observed cohort without committing ownership or reopening admission."""
        selection = CoreRuntimeSelection.model_validate(selection.model_dump(mode="json"))
        authority.verify()
        if authority.plan.authority_sha256() != self.plan.authority_sha256():
            raise UpgradeForbidden("Runtime selection belongs to another accepted authority")
        release = next(
            (
                item
                for item in (self.plan.source, self.plan.target)
                if item.release_id == selection.release_id
            ),
            None,
        )
        now = datetime.now(UTC)
        if (
            release is None
            or (
                selection.project,
                selection.manifest_sha256,
                selection.compose_sha256,
                selection.host_release_key,
                selection.owner_identity,
                selection.generation,
            )
            != (
                authority.claim.project,
                release.manifest_sha256,
                release.manifest.compose_sha256,
                release.manifest.host_release_key,
                authority.claim.owner_identity,
                authority.claim.generation,
            )
            or not 0 <= (now - selection.selected_at).total_seconds() <= 5
        ):
            raise UpgradeConflict("Runtime selection differs from the accepted release or owner")
        if {item.container.service: item.container.image_id for item in selection.services} != {
            item.service: item.image_id for item in release.manifest.services
        } or any(
            not 0 <= (now - item.container.observed_at).total_seconds() <= 5
            for item in selection.services
        ):
            raise UpgradeConflict("Runtime selection lacks a fresh complete cohort")
        if selection.migration is not None and not (
            0 <= (now - selection.migration.container.observed_at).total_seconds() <= 5
        ):
            raise UpgradeConflict("Runtime selection lacks fresh completed migration evidence")
        key = "runtime-selection-" + str(release.release_id) + ".json"
        with self.store.ownership():
            previous = self.store.read(key, CoreRuntimeSelection)
            if previous is not None:

                def identity(value: CoreRuntimeSelection) -> dict[str, object]:
                    return value.model_dump(
                        exclude={
                            "selected_at": True,
                            "generation": True,
                            "services": {"__all__": {"container": {"observed_at"}}},
                            "migration": {"container": {"observed_at"}},
                        }
                    )

                if previous.generation > selection.generation or identity(previous) != identity(
                    selection
                ):
                    raise UpgradeConflict("Retained runtime selection changed on replay")
                # A positively reconciled owner may advance the deployment
                # namespace generation without changing any physical identity.
                # Keep the first durable bytes, but return this fresh observation
                # for subsequent commit checks after a delayed reconnect/restart.
            else:
                self.store.write(key, selection, replace=False)
        # A gateway smoke must identify the actual serving API container from
        # fresh independent Docker observations, including before commit. This
        # observation does not select a runtime or release maintenance.
        authority.verify()
        authority.store.write("observed-epoch.json", self.state())
        authority.verify()
        authority.store.write("observed-runtime.json", selection)
        authority.verify()
        return selection

    def host_selection_receipt(
        self, selection: CoreRuntimeSelection, authority: ExecutorAuthority
    ) -> DeploymentSelectionReceipt:
        """Build retained host authority; this does not commit or release the runtime owner."""
        self.retain_runtime_selection(selection, authority)
        previous = authority.store.read("selection.json", DeploymentSelectionReceipt)
        if previous is not None and previous.project != selection.project:
            raise UpgradeConflict("Retained host selection belongs to another project")
        protected = {} if previous is None else dict(previous.protected_compose_sha256s)
        releases = [self.plan.source, self.plan.target]
        if selection.migration is not None:
            releases.append(selection.migration.release)
        for release in releases:
            key, digest = release.manifest.host_release_key, release.manifest.compose_sha256
            if key is None or digest is None or release.manifest.project != selection.project:
                raise UpgradeConflict("Runtime retention lacks an exact host Compose binding")
            if key in protected and protected[key] != digest:
                raise UpgradeConflict("Retained host release key changed content")
            if self.prepared_root is None:
                raise UpgradeUnavailable("Runtime retention requires verified prepared releases")
            read_prepared_compose(self.prepared_root, release)
            protected[key] = digest
        authority.verify()
        if authority.store.read("selection.json", DeploymentSelectionReceipt) != previous:
            raise UpgradeConflict("Retained host selection changed during verification")
        return DeploymentSelectionReceipt(
            project=selection.project,
            owner_identity=authority.claim.owner_identity,
            boot_identity=authority.claim.boot_identity,
            generation=authority.claim.generation,
            release_key=selection.host_release_key,
            compose_sha256=selection.compose_sha256,
            protected_release_keys=tuple(sorted(protected)),
            protected_compose_sha256s=protected,
            selected_at=selection.selected_at,
            managed_release_id=selection.release_id,
            manifest_sha256=selection.manifest_sha256,
        )

    def commit_runtime_selection(
        self,
        selection: CoreRuntimeSelection,
        operation: UpgradeOperation,
        authority: ExecutorAuthority,
    ) -> DeploymentSelectionReceipt:
        """Publish after an acknowledged commit decision; keep maintenance held.

        Ownership is written last. A crash between the two selection files leaves
        the active owner fenced, and replay revalidates the whole physical cohort.
        """
        latest = self.receipts.latest()
        decision = (
            operation.phase is UpgradePhase.COMMITTING
            and not operation.cancel_requested
            and operation.finished_at is None
            and latest is not None
            and latest.phase is UpgradePhase.COMMITTING
            and latest.outcome == "complete"
            and latest.sequence == operation.receipt_sequence
            and self.receipts.acknowledged_sequence() == latest.sequence
            and latest.observed_sha256 == self.plan.target.manifest_sha256
        )
        terminal = operation.phase in TERMINAL_UPGRADE_PHASES and operation.finished_at is not None
        if operation.authority_sha256() != self.plan.operation.authority_sha256() or not (
            decision or terminal
        ):
            raise UpgradeConflict(
                "Runtime commit requires matching ingested terminal progress or selection decision"
            )
        release = (
            self.plan.target
            if operation.phase in {UpgradePhase.SUCCEEDED, UpgradePhase.COMMITTING}
            else self.plan.source
        )
        if selection.release_id != release.release_id:
            raise UpgradeConflict("Runtime selection differs from the terminal selected release")
        if operation.phase is UpgradePhase.SUCCEEDED and authority.claim.state != "committed":
            raise UpgradeConflict("Target terminal replay requires already committed ownership")
        receipt = self.host_selection_receipt(selection, authority)
        if authority.claim.state == "committed":
            previous = authority.store.read("runtime-selection.json", CoreRuntimeSelection)
            prior_receipt = authority.store.read("selection.json", DeploymentSelectionReceipt)

            def identity(value: CoreRuntimeSelection) -> dict[str, object]:
                return value.model_dump(
                    exclude={
                        "selected_at": True,
                        "services": {"__all__": {"container": {"observed_at"}}},
                        "migration": {"container": {"observed_at"}},
                    }
                )

            if (
                previous is None
                or prior_receipt is None
                or identity(previous) != identity(selection)
                or prior_receipt.model_dump(exclude={"selected_at"})
                != receipt.model_dump(exclude={"selected_at"})
                or authority.claim.committed_sha256 != selection.compose_sha256
            ):
                raise UpgradeConflict("Committed runtime selection changed during replay")
            return prior_receipt
        committed = DeploymentOwnership.model_validate(
            authority.claim.model_dump()
            | {
                "state": "committed",
                "stage": "selected",
                "committed_sha256": selection.compose_sha256,
            }
        )
        authority.verify()
        authority.store.write("runtime-selection.json", selection)
        authority.verify()
        authority.store.write("selection.json", receipt)
        authority.verify()
        authority.store.write("ownership.json", committed)
        authority.claim = committed
        authority.verify()
        return receipt

    def source_snapshot(self) -> CoreSourceSnapshot:
        snapshot = self.store.read("source-snapshot.json", CoreSourceSnapshot)
        if snapshot is None:
            raise UpgradeUnavailable("Core recovery has no retained Source snapshot")
        if (
            snapshot.job_id,
            snapshot.generation,
            snapshot.plan_sha256,
            snapshot.project,
            snapshot.source_sha256,
        ) != (
            self.plan.operation.job_id,
            self.plan.operation.generation,
            self.plan.authority_sha256(),
            self.plan.source.manifest.project,
            self.plan.source.manifest_sha256,
        ) or {item.service: item.image_id for item in snapshot.services} != {
            item.service: item.image_id for item in self.plan.source.manifest.services
        }:
            raise UpgradeConflict("Retained Source snapshot differs from accepted authority")
        return snapshot

    def freeze_source(
        self,
        observations: tuple[UpgradeDockerContainer, ...],
        *,
        now: datetime | None = None,
    ) -> CoreSourceSnapshot:
        now = now or datetime.now(UTC)
        if any(not 0 <= (now - item.observed_at).total_seconds() <= 5 for item in observations):
            raise UpgradeConflict("Core Source snapshot observation is stale or from the future")
        from coire_core.models.upgrades import COHORT_ORDER

        snapshot = CoreSourceSnapshot(
            job_id=self.plan.operation.job_id,
            generation=self.plan.operation.generation,
            plan_sha256=self.plan.authority_sha256(),
            project=self.plan.source.manifest.project or "",
            source_sha256=self.plan.source.manifest_sha256,
            captured_at=now,
            services=tuple(sorted(observations, key=lambda item: COHORT_ORDER.index(item.service))),
        )
        with self.store.ownership():
            previous = self.store.read("source-snapshot.json", CoreSourceSnapshot)
            if previous is not None:
                retained = self.source_snapshot()
                if tuple(
                    item.model_dump(exclude={"observed_at"}) for item in retained.services
                ) != tuple(item.model_dump(exclude={"observed_at"}) for item in snapshot.services):
                    raise UpgradeConflict("Core Source snapshot changed on replay")
                return retained
            if {item.service: item.image_id for item in snapshot.services} != {
                item.service: item.image_id for item in self.plan.source.manifest.services
            }:
                raise UpgradeConflict(
                    "Core Source snapshot is incomplete or differs from its manifest"
                )
            self.store.write("source-snapshot.json", snapshot, replace=False)
            return snapshot

    def activation(
        self,
        consumed: UpgradeActivationConsume,
        *,
        now: datetime | None = None,
        recovery_ack: UpgradeReceiptAck | None = None,
    ) -> CoreExecutorEpoch:
        """Persist only the Core-consumed grant. An expired matching replay cannot renew it."""
        now = now or datetime.now(UTC)
        if recovery_ack is not None and (
            recovery_ack.job_id,
            recovery_ack.generation,
            recovery_ack.sequence,
        ) != (
            consumed.intent.job_id,
            consumed.intent.generation,
            consumed.intent.sequence,
        ):
            raise UpgradeConflict("Consumed recovery acknowledgment differs from its exact intent")
        with self.store.ownership():
            state = self.state()
            if (consumed.grant.job_id, consumed.grant.generation) != (
                state.job_id,
                state.generation,
            ):
                raise UpgradeConflict("Activation belongs to another executor epoch")
            existing = next(
                (
                    item
                    for item in state.activations
                    if item.grant.boundary == consumed.grant.boundary
                ),
                None,
            )
            if existing is not None:
                if existing != consumed:
                    raise UpgradeConflict("Executor activation replay differs")
                if recovery_ack is not None and state.recovery_captured_at is None:
                    capture = min(now, consumed.grant.expires_at)
                    state = CoreExecutorEpoch.model_validate(
                        state.model_dump()
                        | {
                            "recovery_captured_at": capture,
                            "recovery_deadline": capture
                            + timedelta(seconds=self.plan.operation.timing.recovery_s),
                        }
                    )
                    self.store.write("epoch.json", state)
                return state
            if consumed.intent.observed_sha256 != self.plan.operation.source_sha256:
                raise UpgradeConflict("Executor activation source differs")
            if consumed.intent.occurred_at > now or (
                now >= consumed.grant.expires_at and recovery_ack is None
            ):
                raise UpgradeForbidden("Executor activation authorization expired")
            if state.recovery_captured_at is not None and recovery_ack is None:
                raise UpgradeForbidden("Recovery cannot accept a candidate activation")
            if (
                consumed.grant.boundary == "runtime"
                and self.plan.needs_migration
                and not any(item.grant.boundary == "migration" for item in state.activations)
            ):
                raise UpgradeConflict("Runtime activation has no retained migration boundary")
            if consumed.grant.boundary == "runtime" and self.plan.needs_migration:
                proof = self.receipts.latest()
                if (
                    proof is None
                    or proof.phase is not UpgradePhase.MIGRATING
                    or proof.outcome != "complete"
                    or not proof.evidence
                ):
                    raise UpgradeConflict(
                        "Runtime activation requires retained terminal migration evidence"
                    )
                evidence = UpgradeEvidence.model_validate_json(proof.evidence)
                if (
                    evidence.migration_exit_code != 0
                    or evidence.schema_head != self.plan.schema_head_after
                    or evidence.schema_after_sha256 is None
                ):
                    raise UpgradeConflict(
                        "Runtime activation requires the expected zero-exit schema"
                    )
            changes: dict[str, object] = {"activations": (*state.activations, consumed)}
            if consumed.grant.boundary == "runtime":
                changes["readiness_deadline"] = consumed.intent.occurred_at + timedelta(
                    seconds=self.plan.operation.timing.readiness_s
                    * max(1, len(self.plan.operation.cohort))
                )
                if self.plan.operation.cohort:
                    changes["service_windows"] = (
                        CoreServiceWindow(
                            service=self.plan.operation.cohort[0],
                            captured_at=consumed.intent.occurred_at,
                            deadline_at=consumed.intent.occurred_at
                            + timedelta(seconds=self.plan.operation.timing.readiness_s),
                        ),
                    )
            if recovery_ack is not None and state.recovery_captured_at is None:
                capture = min(now, consumed.grant.expires_at)
                changes |= {
                    "recovery_captured_at": capture,
                    "recovery_deadline": capture
                    + timedelta(seconds=self.plan.operation.timing.recovery_s),
                }
            updated = CoreExecutorEpoch.model_validate(state.model_dump() | changes)
            self.store.write("epoch.json", updated)
            return updated

    def service_window(
        self, service: ServiceName, *, now: datetime | None = None
    ) -> CoreServiceWindow:
        now = now or datetime.now(UTC)
        with self.store.ownership():
            state = self.state()
            if service not in self.plan.operation.cohort:
                raise UpgradeForbidden("Service readiness is outside the accepted changed cohort")
            retained = next(
                (item for item in state.service_windows if item.service == service), None
            )
            if retained is not None:
                return retained
            if (
                state.readiness_deadline is None
                or now >= state.readiness_deadline
                or state.recovery_captured_at is not None
            ):
                raise UpgradeForbidden(
                    "Core service readiness cannot renew expired runtime authority"
                )
            deadline = min(
                now + timedelta(seconds=self.plan.operation.timing.readiness_s),
                state.readiness_deadline,
            )
            if state.outage_deadline is not None:
                deadline = min(deadline, state.outage_deadline)
            if deadline <= now:
                raise UpgradeForbidden("Core service readiness exceeds the captured outage budget")
            window = CoreServiceWindow(service=service, captured_at=now, deadline_at=deadline)
            self.store.write(
                "epoch.json",
                CoreExecutorEpoch.model_validate(
                    state.model_dump() | {"service_windows": (*state.service_windows, window)}
                ),
            )
            return window

    def capture_outage(self, *, now: datetime | None = None) -> CoreExecutorEpoch:
        now = now or datetime.now(UTC)
        with self.store.ownership():
            state = self.state()
            if state.outage_captured_at is not None:
                return state
            if (
                state.readiness_deadline is None
                or now >= state.readiness_deadline
                or state.recovery_captured_at is not None
            ):
                raise UpgradeForbidden(
                    "Executor cannot begin a new outage outside runtime activation"
                )
            updated = CoreExecutorEpoch.model_validate(
                state.model_dump()
                | {
                    "outage_captured_at": now,
                    "outage_deadline": now
                    + timedelta(seconds=self.plan.operation.timing.gateway_outage_s),
                }
            )
            self.store.write("epoch.json", updated)
            return updated

    def expired_forward_deadlines(self, *, now: datetime | None = None) -> tuple[datetime, ...]:
        """Read original failed boundaries without capturing or renewing an epoch."""
        now = now or datetime.now(UTC)
        state = self.state()
        latest = self.receipts.latest()
        if latest is not None and (
            (latest.phase is UpgradePhase.VERIFYING and latest.outcome == "complete")
            or latest.phase is UpgradePhase.COMMITTING
        ):
            # Serving already passed under its original budget. A later explicit
            # cancellation or revocation starts recovery at that new failure;
            # publication retry cannot revive a discharged readiness deadline.
            return ()

        deadlines = [
            value
            for value in (state.readiness_deadline, state.outage_deadline)
            if value is not None and value <= now
        ]
        migration = next(
            (item for item in state.activations if item.grant.boundary == "migration"), None
        )
        if migration is not None:
            proof = self.store.read("migration-proof.json", UpgradeMigrationExecution)
            if proof is not None and (proof.job_id, proof.generation, proof.plan_sha256) != (
                state.job_id,
                state.generation,
                state.plan_sha256,
            ):
                raise UpgradeConflict("Recovery migration proof belongs to another epoch")
            if proof is None or proof.state != "complete":
                failed_at = (
                    proof.completed_at
                    if proof is not None and proof.completed_at is not None
                    else migration.intent.occurred_at
                    + timedelta(seconds=self.plan.operation.timing.readiness_s)
                )
                failed_at = min(
                    failed_at,
                    migration.intent.occurred_at
                    + timedelta(seconds=self.plan.operation.timing.readiness_s),
                )
                if failed_at <= now:
                    deadlines.append(failed_at)
        for window in state.service_windows:
            completion = self.effects.read(
                "core-start-candidate-" + window.service + ".json", CoreExecutorEffect
            )
            if completion is not None:
                self._validate(completion)
                if completion.action != "start_candidate" or completion.service != window.service:
                    raise UpgradeConflict("Service readiness completion authority differs")
            from coire_core.models.upgrades import COHORT_ORDER

            last_service = next(
                service
                for service in reversed(COHORT_ORDER)
                if service in self.plan.operation.cohort
            )
            if window.deadline_at <= now and (
                completion is None
                or completion.outcome != "complete"
                or window.service == last_service
            ):
                deadlines.append(window.deadline_at)
        return tuple(deadlines)

    def capture_recovery(self, *, now: datetime | None = None) -> CoreExecutorEpoch:
        now = now or datetime.now(UTC)
        with self.store.ownership():
            state = self.state()
            if state.recovery_captured_at is not None:
                return state
            deadlines = self.expired_forward_deadlines(now=now)
            # Failure detection starts restoration's own budget (spec A1).
            # A restart past a known failed deadline cannot renew that budget.
            capture = min(now, *deadlines) if deadlines else now
            deadline = capture + timedelta(seconds=self.plan.operation.timing.recovery_s)
            updated = CoreExecutorEpoch.model_validate(
                state.model_dump()
                | {"recovery_captured_at": capture, "recovery_deadline": deadline}
            )
            self.store.write("epoch.json", updated)
            return updated

    def intent(
        self, effect: CoreExecutorEffect, *, now: datetime | None = None
    ) -> CoreExecutorEffect:
        now = now or datetime.now(UTC)
        self._validate(effect)
        with self.store.ownership():
            previous = self.effects.read(effect.effect_id + ".json", CoreExecutorEffect)
            if previous is not None:
                self._validate(previous)
                if previous.model_dump(
                    exclude={"outcome", "observed_container_id"}
                ) != effect.model_dump(exclude={"outcome", "observed_container_id"}):
                    raise UpgradeConflict("Executor effect replay differs")
                return previous
            state = self.state()
            recovery = effect.action in {
                "stop_candidate",
                "remove_candidate",
                "remove_migration",
                "start_source",
            }
            expected_deadline = state.recovery_deadline if recovery else state.readiness_deadline
            if not recovery and effect.service is not None:
                window = next(
                    (item for item in state.service_windows if item.service == effect.service), None
                )
                expected_deadline = window.deadline_at if window is not None else None
            if effect.action in {"migration", "create_migration", "start_migration"}:
                activation = next(
                    (item for item in state.activations if item.grant.boundary == "migration"), None
                )
                expected_deadline = (
                    activation.intent.occurred_at
                    + timedelta(seconds=self.plan.operation.timing.readiness_s)
                    if activation is not None
                    else None
                )
            if (
                expected_deadline is None
                or effect.deadline != expected_deadline
                or now >= effect.deadline
                or effect.captured_at > now
                or effect.outcome != "intent"
            ):
                raise UpgradeForbidden("Executor effect has no current captured deadline")
            if not recovery and state.recovery_captured_at is not None:
                raise UpgradeForbidden("Executor recovery forbids another candidate effect")
            if effect.action in {
                "migration",
                "create_migration",
                "start_migration",
                "remove_migration",
            }:
                if self.prepared_root is None:
                    raise UpgradeUnavailable(
                        "Migration requires the verified prepared artifact mount"
                    )
                document = read_prepared_compose(self.prepared_root, self.plan.target)
                services = document.get("services")
                configuration = (
                    services.get("coire-migrate") if isinstance(services, dict) else None
                )
                try:
                    migration = UpgradeComposeMigration.model_validate(configuration)
                except ValueError:
                    raise UpgradeConflict(
                        "Fixed migration service configuration is unsupported"
                    ) from None
                if migration.image != effect.image_id or migration.restart != "no":
                    raise UpgradeConflict("Migration image or one-shot policy differs")
            if effect.service is not None:
                if effect.service not in self.plan.operation.cohort:
                    raise UpgradeForbidden("Executor effect is outside the changed cohort")
                release = (
                    self.plan.source
                    if effect.action in {"stop_source", "start_source"}
                    else self.plan.target
                )
                expected = next(
                    item for item in release.manifest.services if item.service == effect.service
                )
                if effect.image_id != expected.image_id:
                    raise UpgradeConflict("Executor effect image differs from its accepted role")
            self.effects.write(effect.effect_id + ".json", effect, replace=False)
            return effect

    def observe(
        self, effect_id: str, observed: UpgradeDockerContainer, *, now: datetime | None = None
    ) -> CoreExecutorEffect:
        """Only fresh exact-resource observations close a durable lifecycle intent."""
        now = now or datetime.now(UTC)
        if not 0 <= (now - observed.observed_at).total_seconds() <= 5:
            raise UpgradeConflict("Executor effect observation is stale or from the future")
        with self.store.ownership():
            effect = self.effects.read(effect_id + ".json", CoreExecutorEffect)
            if effect is None:
                raise UpgradeConflict("Executor observation has no preceding durable intent")
            self._validate(effect)
            if effect.action in {"migration", "remove_candidate", "remove_migration"}:
                raise UpgradeConflict("This effect requires its dedicated completion protocol")
            if (observed.service, observed.project, observed.image_id) != (
                effect.service,
                self.plan.source.manifest.project,
                effect.image_id,
            ) or (effect.container_id is not None and observed.container_id != effect.container_id):
                raise UpgradeConflict("Executor observation belongs to another container identity")
            if effect.action in {"stop_source", "stop_candidate"} and observed.running:
                raise UpgradeConflict("Executor stop outcome remains running")
            if effect.action in {"start_source", "start_candidate"} and (
                not observed.running or observed.health != "healthy"
            ):
                raise UpgradeConflict("Executor start outcome is not freshly healthy")
            if effect.outcome == "complete":
                if effect.observed_container_id != observed.container_id:
                    raise UpgradeConflict("Executor completion replay differs")
                return effect
            updated = CoreExecutorEffect.model_validate(
                effect.model_dump()
                | {
                    "outcome": "complete",
                    "observed_container_id": observed.container_id,
                }
            )
            self.effects.write(effect_id + ".json", updated)
            return updated

    def observe_removed(
        self, effect_id: str, container_id: str, observed_at: datetime
    ) -> CoreExecutorEffect:
        """Close only the exact stopped candidate whose absence was freshly observed."""
        if not 0 <= (datetime.now(UTC) - observed_at).total_seconds() <= 5:
            raise UpgradeConflict("Candidate absence observation is stale or future")
        with self.store.ownership():
            effect = self.effects.read(effect_id + ".json", CoreExecutorEffect)
            if effect is None:
                raise UpgradeConflict("Candidate absence has no retained removal intent")
            self._validate(effect)
            stopped = self.effects.read(
                "core-stop-candidate-" + str(effect.service) + ".json", CoreExecutorEffect
            )
            if (
                effect.action != "remove_candidate"
                or effect.container_id != container_id
                or stopped is None
                or stopped.action != "stop_candidate"
                or stopped.service != effect.service
                or stopped.outcome != "complete"
                or stopped.observed_container_id != container_id
                or stopped.image_id != effect.image_id
            ):
                raise UpgradeConflict("Candidate removal lacks exact stopped ownership")
            self._validate(stopped)
            updated = effect.model_copy(
                update={"outcome": "complete", "observed_container_id": container_id}
            )
            self.effects.write(effect_id + ".json", updated)
            return updated

    def observe_migration_removed(
        self, effect_id: str, container_id: str, observed_at: datetime
    ) -> CoreExecutorEffect:
        """Only a positively unchanged failed attempt can be retired during recovery."""
        if not 0 <= (datetime.now(UTC) - observed_at).total_seconds() <= 5:
            raise UpgradeConflict("Migration absence observation is stale or future")
        with self.store.ownership():
            effect = self.effects.read(effect_id + ".json", CoreExecutorEffect)
            terminal = self.effects.read("core-migration.json", CoreExecutorEffect)
            proof = self.store.read("migration-proof.json", UpgradeMigrationExecution)
            if effect is None or terminal is None or proof is None:
                raise UpgradeConflict("Migration removal lacks retained terminal evidence")
            self._validate(effect)
            self._validate(terminal)
            if (
                effect.action != "remove_migration"
                or effect.container_id != container_id
                or terminal.action != "migration"
                or terminal.outcome != "complete"
                or terminal.observed_container_id != container_id
                or terminal.image_id != effect.image_id
                or not proof.failed_unchanged
                or (proof.schema_before_head, proof.schema_before_sha256)
                != (self.plan.initial_schema_head, self.plan.initial_schema_sha256)
                or (proof.job_id, proof.generation, proof.plan_sha256)
                != (effect.job_id, effect.generation, effect.plan_sha256)
                or self.state().recovery_captured_at is None
            ):
                raise UpgradeConflict("Migration removal differs from unchanged failed attempt")
            updated = effect.model_copy(
                update={"outcome": "complete", "observed_container_id": container_id}
            )
            self.effects.write(effect_id + ".json", updated)
            return updated

    def uncertain(self, effect_id: str) -> CoreExecutorEffect:
        """A lost response records uncertainty; it grants no retry or cleanup authority."""
        with self.store.ownership():
            effect = self.effects.read(effect_id + ".json", CoreExecutorEffect)
            if effect is None:
                raise UpgradeConflict("Executor uncertainty has no preceding durable intent")
            self._validate(effect)
            if effect.outcome != "intent":
                return effect
            updated = effect.model_copy(update={"outcome": "unknown"})
            self.effects.write(effect_id + ".json", updated)
            return updated

    def complete_migration(
        self,
        effect_id: str,
        observed: UpgradeDockerMigration,
        proof: UpgradeMigrationExecution,
        *,
        now: datetime | None = None,
    ) -> CoreExecutorEffect:
        """A terminal attempt requires expected success or positively unchanged failure."""
        now = now or datetime.now(UTC)
        if not 0 <= (now - observed.observed_at).total_seconds() <= 5:
            raise UpgradeConflict("Migration container observation is stale or from the future")
        with self.store.ownership():
            effect = self.effects.read(effect_id + ".json", CoreExecutorEffect)
            if effect is None:
                raise UpgradeConflict("Migration completion has no durable effect intent")
            self._validate(effect)
            activation = next(
                (item for item in self.state().activations if item.grant.boundary == "migration"),
                None,
            )
            if (
                effect.action != "migration"
                or activation is None
                or observed.project != self.plan.source.manifest.project
                or observed.name
                != f"{observed.project}-upgrade-{effect.job_id.lower()}-coire-migrate"
                or (
                    effect.container_id is not None and observed.container_id != effect.container_id
                )
                or observed.image_id != effect.image_id
                or observed.running
                or observed.status != "exited"
                or observed.exit_code != proof.exit_code
                or not (proof.state == "complete" or proof.failed_unchanged)
                or proof.completed_at is None
                or proof.completed_at > now
                or (proof.job_id, proof.generation, proof.plan_sha256)
                != (effect.job_id, effect.generation, effect.plan_sha256)
                or (proof.grant_id, proof.captured_at, proof.deadline_at)
                != (activation.grant.grant_id, activation.intent.occurred_at, effect.deadline)
                or (proof.schema_before_head, proof.schema_before_sha256)
                != (self.plan.initial_schema_head, self.plan.initial_schema_sha256)
                or proof.expected_schema_head != self.plan.schema_head_after
            ):
                raise UpgradeConflict(
                    "Migration requires exact terminal expected or unchanged schema evidence"
                )
            retained = self.store.read("migration-proof.json", UpgradeMigrationExecution)
            if retained is not None and retained != proof:
                raise UpgradeConflict("Migration completion proof changed on replay")
            if effect.outcome == "complete":
                if effect.observed_container_id != observed.container_id or retained is None:
                    raise UpgradeConflict("Migration completion identity or retained proof differs")
                return effect
            if retained is None:
                self.store.write("migration-proof.json", proof, replace=False)
            updated = effect.model_copy(
                update={"outcome": "complete", "observed_container_id": observed.container_id}
            )
            self.effects.write(effect_id + ".json", updated)
            return updated

    def observe_migration_container(
        self,
        effect_id: str,
        observed: UpgradeDockerMigration,
        *,
        now: datetime | None = None,
    ) -> CoreExecutorEffect:
        """Close create/start transport only; schema completion remains a separate proof."""
        now = now or datetime.now(UTC)
        if not 0 <= (now - observed.observed_at).total_seconds() <= 5:
            raise UpgradeConflict("Migration lifecycle observation is stale or from the future")
        with self.store.ownership():
            effect = self.effects.read(effect_id + ".json", CoreExecutorEffect)
            if effect is None:
                raise UpgradeConflict("Migration lifecycle has no durable intent")
            self._validate(effect)
            if (
                effect.action not in {"create_migration", "start_migration"}
                or observed.project != self.plan.source.manifest.project
                or observed.image_id != effect.image_id
                or observed.name
                != f"{observed.project}-upgrade-{effect.job_id.lower()}-coire-migrate"
                or (
                    effect.container_id is not None and observed.container_id != effect.container_id
                )
                or (
                    effect.action == "start_migration"
                    and observed.status not in {"running", "exited"}
                )
            ):
                raise UpgradeConflict("Migration lifecycle observation identity or state differs")
            if effect.outcome == "complete":
                if effect.observed_container_id != observed.container_id:
                    raise UpgradeConflict("Migration lifecycle completion replay differs")
                return effect
            updated = effect.model_copy(
                update={"outcome": "complete", "observed_container_id": observed.container_id}
            )
            self.effects.write(effect_id + ".json", updated)
            return updated

    def dispatch_intent(
        self,
        effect_id: str,
        authority: ExecutorAuthority,
        *,
        now: datetime | None = None,
    ) -> CoreExecutorEffect:
        """Persist transport uncertainty before dispatch; replay must observe reality first."""
        now = now or datetime.now(UTC)
        authority.verify()
        if authority.claim.state != "active":
            raise UpgradeForbidden("Committed runtime ownership cannot dispatch another effect")
        if authority.plan.authority_sha256() != self.plan.authority_sha256():
            raise UpgradeForbidden("Executor kernel owner belongs to another plan")
        with self.store.ownership():
            effect = self.effects.read(effect_id + ".json", CoreExecutorEffect)
            if effect is None:
                raise UpgradeConflict("Executor transport has no preceding durable intent")
            self._validate(effect)
            if effect.outcome != "intent":
                raise UpgradeConflict(
                    "Executor transport replay requires actual-state reconciliation"
                )
            state = self.state()
            recovery = effect.action in {
                "stop_candidate",
                "remove_candidate",
                "remove_migration",
                "start_source",
            }
            if (
                now >= effect.deadline
                or effect.captured_at > now
                or (state.recovery_captured_at is not None and not recovery)
            ):
                raise UpgradeForbidden("Executor transport deadline or recovery epoch changed")
            if (
                effect.action == "stop_source"
                and effect.service in {"coire-api", "coire-scheduler", "coire-web"}
                and state.outage_deadline is None
            ):
                raise UpgradeConflict("Gateway-affecting stop has no captured outage budget")
            if not recovery and state.outage_deadline is not None and now >= state.outage_deadline:
                raise UpgradeForbidden(
                    "Executor transport exceeds its captured gateway outage budget"
                )
            authority.verify()
            updated = effect.model_copy(update={"outcome": "unknown"})
            self.effects.write(effect_id + ".json", updated)
            return updated
