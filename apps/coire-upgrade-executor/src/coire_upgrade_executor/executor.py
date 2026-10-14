"""Fixed-plan execution using retained intentions rather than process-local progress."""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from uuid import UUID

from opentelemetry import trace

from coire_core.errors import UpgradeConflict, UpgradeForbidden, UpgradeUnavailable
from coire_core.models.upgrades import (
    COHORT_ORDER,
    TERMINAL_UPGRADE_PHASES,
    CoreExecutorEffect,
    CoreRuntimeSelection,
    CoreSelectedService,
    ServiceName,
    UpgradeActivationConsume,
    UpgradeDockerContainer,
    UpgradeMigrationExecution,
    UpgradeMigrationNotStarted,
    UpgradePhase,
    UpgradeReceipt,
    UpgradeSchemaObservation,
)

from .client import ExecutorClient
from .docker import UpgradeDocker
from .journal import ExecutorJournal
from .ownership import ExecutorAuthority

tracer = trace.get_tracer("coire.upgrade_executor.executor")


class CoreExecutor:
    def __init__(
        self,
        journal: ExecutorJournal,
        docker: UpgradeDocker,
        client: ExecutorClient,
        authority: ExecutorAuthority,
        prepared_root: Path,
    ) -> None:
        if journal.plan.authority_sha256() != docker.plan.authority_sha256():
            raise UpgradeConflict("Executor components belong to different accepted plans")
        authority.verify()
        client.bind(journal.plan)
        self.journal, self.docker, self.client = journal, docker, client
        self.authority, self.prepared_root = authority, prepared_root

    async def consume_boundary(self, *, migration: bool = False) -> UpgradeActivationConsume:
        """Fsync an exact consume request before HTTP; lost replies never request a new grant."""
        self.authority.verify()
        if self.authority.claim.state != "active":
            raise UpgradeForbidden("Committed runtime cannot consume another activation")
        boundary = "migration" if migration else "runtime"
        key = "consume-" + boundary + ".json"
        retained = self.journal.store.read(key, UpgradeActivationConsume)
        if retained is None:
            grant = await self.client.grant(migration=migration)
            latest = self.journal.receipts.latest()
            retained = UpgradeActivationConsume(
                grant=grant,
                intent=UpgradeReceipt(
                    job_id=self.journal.plan.operation.job_id,
                    generation=self.journal.plan.operation.generation,
                    sequence=latest.sequence + 1 if latest is not None else 1,
                    effect_id="core-" + boundary + "-activation",
                    phase=UpgradePhase.MIGRATING if migration else UpgradePhase.ACTIVATING,
                    occurred_at=datetime.now(UTC),
                    outcome="intent",
                    observed_sha256=self.journal.plan.operation.source_sha256,
                ),
            )
            self.authority.verify()
            self.journal.store.write(key, retained, replace=False)
        if retained.grant.boundary != boundary or (
            retained.intent.job_id,
            retained.intent.generation,
            retained.intent.observed_sha256,
        ) != (
            self.journal.plan.operation.job_id,
            self.journal.plan.operation.generation,
            self.journal.plan.operation.source_sha256,
        ):
            raise UpgradeConflict("Retained activation consume request differs from accepted plan")
        existing = next(
            (item for item in self.journal.state().activations if item.grant.boundary == boundary),
            None,
        )
        if existing is not None and existing != retained:
            raise UpgradeConflict("Retained consumed activation identity changed")
        if (
            existing is not None
            and self.journal.receipts.acknowledged_sequence() >= retained.intent.sequence
        ):
            page = self.journal.receipts.page(retained.intent.sequence - 1)
            if not page or page[0] != retained.intent:
                raise UpgradeConflict("Consumed activation differs from retained receipt bytes")
            # No renewed HTTP grant or mutation authority on completed replay.
            return retained
        ack = await self.client.consume(retained)
        if ack.sequence != retained.intent.sequence:
            raise UpgradeConflict("Activation acknowledgment differs from exact consumed intent")
        self.authority.verify()
        try:
            self.journal.activation(retained)
        except UpgradeForbidden:
            # A matching consumed replay acknowledgment permits Source recovery
            # after reply loss, never a fresh or renewed candidate activation.
            self.journal.activation(retained, recovery_ack=ack)
        self.journal.receipts.append(retained.intent)
        self.journal.receipts.acknowledge(ack)
        return retained

    def service_effect(
        self,
        action: str,
        service: ServiceName,
        *,
        container_id: str | None = None,
    ) -> CoreExecutorEffect:
        if (
            action
            not in {
                "stop_source",
                "create_candidate",
                "start_candidate",
                "stop_candidate",
                "remove_candidate",
                "start_source",
            }
            or service not in self.journal.plan.operation.cohort
        ):
            raise UpgradeForbidden("Service dispatch is outside the accepted changed cohort")
        effect_id = "core-" + action.replace("_", "-") + "-" + service
        state = self.journal.state()
        release = (
            self.journal.plan.source
            if action in {"stop_source", "start_source"}
            else self.journal.plan.target
        )
        role = next(item for item in release.manifest.services if item.service == service)
        previous = self.journal.effects.read(effect_id + ".json", CoreExecutorEffect)
        if previous is not None:
            if (previous.action, previous.service, previous.container_id, previous.image_id) != (
                action,
                service,
                container_id,
                role.image_id,
            ):
                raise UpgradeConflict("Retained service effect identity differs")
            return self.journal.intent(previous)
        deadline = (
            state.recovery_deadline
            if action in {"stop_candidate", "remove_candidate", "start_source"}
            else self.journal.service_window(service).deadline_at
        )
        if deadline is None:
            raise UpgradeForbidden("Service dispatch has no retained activation deadline")
        return self.journal.intent(
            CoreExecutorEffect.model_validate(
                {
                    "job_id": state.job_id,
                    "generation": state.generation,
                    "plan_sha256": state.plan_sha256,
                    "effect_id": effect_id,
                    "action": action,
                    "service": service,
                    "container_id": container_id,
                    "image_id": role.image_id,
                    "captured_at": datetime.now(UTC),
                    "deadline": deadline,
                }
            )
        )

    async def reconcile_lifecycle(
        self,
        effect: CoreExecutorEffect,
        dispatch: Callable[[], Awaitable[UpgradeDockerContainer]],
    ) -> UpgradeDockerContainer:
        """An uncertain dispatch can only repeat inspection, never its external mutation."""
        while True:
            self.authority.verify()
            state = self.journal.state()
            deadline = (
                min(effect.deadline, state.outage_deadline)
                if state.outage_deadline
                else effect.deadline
            )
            remaining = (deadline - datetime.now(UTC)).total_seconds()
            if remaining <= 0:
                raise UpgradeUnavailable("Service outcome exceeded its retained deadline")
            try:
                async with asyncio.timeout(remaining):
                    return await dispatch()
            except (UpgradeConflict, UpgradeUnavailable, TimeoutError):
                recorded = self.journal.effects.read(effect.effect_id + ".json", CoreExecutorEffect)
                if recorded is None or recorded.outcome != "unknown":
                    raise
                state = self.journal.state()
                deadline = (
                    min(effect.deadline, state.outage_deadline)
                    if state.outage_deadline
                    else effect.deadline
                )
                remaining = (deadline - datetime.now(UTC)).total_seconds()
                if remaining <= 0:
                    raise UpgradeUnavailable(
                        "Service outcome remains uncertain at its retained deadline"
                    ) from None
                await asyncio.sleep(min(1, remaining))

    async def replace_cohort(self, *, dependency_states: dict[str, str]) -> None:
        """Replace in fixed order while preserving every exact Source container reference."""
        with tracer.start_as_current_span("coire.upgrade_executor.replace_cohort"):
            snapshot = self.journal.source_snapshot()
            if self.journal.plan.needs_migration:
                proof = self.journal.store.read("migration-proof.json", UpgradeMigrationExecution)
                if proof is None or proof.state != "complete":
                    raise UpgradeForbidden(
                        "Runtime replacement requires completed migration evidence"
                    )
            sources = {item.service: item for item in snapshot.services}
            for service in COHORT_ORDER:
                if service not in self.journal.plan.operation.cohort:
                    continue
                if service in {"coire-scheduler", "coire-api", "coire-web"}:
                    self.journal.capture_outage()
                stopped = self.service_effect(
                    "stop_source", service, container_id=sources[service].container_id
                )
                await self.reconcile_lifecycle(
                    stopped,
                    partial(
                        self.docker.stop_source,
                        stopped,
                        self.journal,
                        self.authority,
                        prepared_root=self.prepared_root,
                        dependency_states=dependency_states,
                    ),
                )
                create = self.service_effect("create_candidate", service)
                created = await self.reconcile_lifecycle(
                    create,
                    partial(
                        self.docker.create_candidate,
                        create,
                        self.journal,
                        self.authority,
                        prepared_root=self.prepared_root,
                        dependency_states=dependency_states,
                    ),
                )
                start = self.service_effect(
                    "start_candidate", service, container_id=created.container_id
                )
                await self.reconcile_lifecycle(
                    start,
                    partial(
                        self.docker.candidate_lifecycle,
                        start,
                        self.journal,
                        self.authority,
                        prepared_root=self.prepared_root,
                        dependency_states=dependency_states,
                    ),
                )

    async def restore_cohort(self, *, dependency_states: dict[str, str]) -> None:
        """Restore changed Source roles in reverse order; uncertain effects retain maintenance."""
        with tracer.start_as_current_span("coire.upgrade_executor.restore_cohort"):
            self.journal.capture_recovery()
            snapshot = self.journal.source_snapshot()
            sources = {item.service: item for item in snapshot.services}
            for service in reversed(COHORT_ORDER):
                if service not in self.journal.plan.operation.cohort:
                    continue
                create = self.journal.effects.read(
                    "core-create-candidate-" + service + ".json", CoreExecutorEffect
                )
                if create is not None:
                    if create.outcome != "complete" or create.observed_container_id is None:
                        raise UpgradeUnavailable(
                            "Candidate creation remains uncertain; maintenance remains"
                        )
                    stop = self.service_effect(
                        "stop_candidate", service, container_id=create.observed_container_id
                    )
                    await self.reconcile_lifecycle(
                        stop,
                        partial(
                            self.docker.candidate_lifecycle,
                            stop,
                            self.journal,
                            self.authority,
                            prepared_root=self.prepared_root,
                            dependency_states=dependency_states,
                        ),
                    )
                stopped = self.journal.effects.read(
                    "core-stop-source-" + service + ".json", CoreExecutorEffect
                )
                if stopped is not None:
                    start = self.service_effect(
                        "start_source", service, container_id=sources[service].container_id
                    )
                    await self.reconcile_lifecycle(
                        start,
                        partial(
                            self.docker.start_source,
                            start,
                            self.journal,
                            self.authority,
                            prepared_root=self.prepared_root,
                            dependency_states=dependency_states,
                        ),
                    )
            for source in snapshot.services:
                observed = await self.docker.inspect_verified_source(
                    source.name, self.prepared_root, dependency_states=dependency_states
                )
                if (
                    observed.container_id,
                    observed.image_id,
                    observed.running,
                    observed.health,
                ) != (source.container_id, source.image_id, True, "healthy"):
                    raise UpgradeConflict(
                        "Whole-cohort Source restoration is not positively verified"
                    )

    async def verify_schema(self, *, restored: bool) -> UpgradeSchemaObservation:
        """A running compatible binary cannot substitute for fresh structural DB evidence."""
        observed = await self.client.schema()
        plan = self.journal.plan
        proof = self.journal.store.read("migration-proof.json", UpgradeMigrationExecution)
        migrated = any(
            item.grant.boundary == "migration" for item in self.journal.state().activations
        )
        not_started: UpgradeMigrationNotStarted | None = None
        if migrated and restored and proof is None:
            state = self.journal.state()
            if state.recovery_deadline is None or datetime.now(UTC) >= state.recovery_deadline:
                raise UpgradeUnavailable("Unstarted migration recovery deadline elapsed")
            if any(
                self.journal.effects.read(name + ".json", CoreExecutorEffect) is not None
                for name in ("core-migration", "core-create-migration", "core-start-migration")
            ):
                raise UpgradeUnavailable(
                    "Migration dispatch uncertainty retains recovery ownership"
                )
            self.authority.verify()
            not_started = await self.client.fence_migration_not_started()
            activation = next(
                item for item in state.activations if item.grant.boundary == "migration"
            )
            if not_started.grant_id != activation.grant.grant_id:
                raise UpgradeConflict("Unstarted migration fence grant differs")
            await self.docker.verify_migration_candidate_absent()
            self.authority.verify()
            if datetime.now(UTC) >= state.recovery_deadline:
                raise UpgradeUnavailable("Unstarted migration proof exceeded recovery deadline")
            self.journal.store.write("migration-not-started.json", not_started)
            observed = await self.client.schema()
        if migrated and (
            (proof is None and not_started is None)
            or (
                proof is not None
                and (
                    not (proof.state == "complete" or (restored and proof.failed_unchanged))
                    or (proof.job_id, proof.generation, proof.plan_sha256)
                    != (plan.operation.job_id, plan.operation.generation, plan.authority_sha256())
                )
            )
        ):
            raise UpgradeUnavailable("Uncertain schema migration retains recovery ownership")
        if proof is not None and not migrated:
            raise UpgradeConflict("Schema proof lacks its retained migration activation")
        if migrated and restored and proof is not None and proof.failed_unchanged:
            effect = self.journal.effects.read("core-migration.json", CoreExecutorEffect)
            if effect is None or effect.outcome != "complete":
                raise UpgradeUnavailable(
                    "Failed migration has no positive terminal container proof"
                )
            removal = self.journal.effects.read("core-remove-migration.json", CoreExecutorEffect)
            if removal is not None and removal.outcome != "intent":
                await self.docker.remove_failed_migration(
                    removal, self.journal, self.authority, prepared_root=self.prepared_root
                )
            else:
                terminal = await self.docker.inspect_migration(
                    prepared_root=self.prepared_root, dependency_states={}
                )
                self.journal.complete_migration(effect.effect_id, terminal, proof)
        completed = migrated and proof is not None and proof.state == "complete"
        expected_head = plan.schema_head_after if completed else plan.initial_schema_head
        expected_digest = (
            proof.schema_after_sha256
            if completed and proof is not None
            else plan.initial_schema_sha256
        )
        release = plan.source if restored else plan.target
        if (
            observed.schema_head != expected_head
            or observed.schema_sha256 != expected_digest
            or observed.schema_head not in release.manifest.compatibility.readable_schema_heads
        ):
            raise UpgradeConflict("Selected Core runtime has a changed or incompatible schema")
        if not 0 <= (datetime.now(UTC) - observed.observed_at).total_seconds() <= 5:
            raise UpgradeConflict("Schema observation expired during terminal verification")
        self.authority.verify()
        return observed

    async def cleanup_candidates(
        self, *, dependency_states: dict[str, str]
    ) -> CoreRuntimeSelection:
        """Remove stopped owned candidates only after positive whole-Source restoration."""
        await self.restored_runtime(dependency_states=dependency_states)
        for service in reversed(COHORT_ORDER):
            if service not in self.journal.plan.operation.cohort:
                continue
            created = self.journal.effects.read(
                "core-create-candidate-" + service + ".json", CoreExecutorEffect
            )
            if created is None:
                continue
            if created.outcome != "complete" or created.observed_container_id is None:
                raise UpgradeUnavailable("Candidate cleanup retains uncertain creation ownership")
            effect = self.service_effect(
                "remove_candidate", service, container_id=created.observed_container_id
            )
            await self.docker.remove_candidate(
                effect, self.journal, self.authority, prepared_root=self.prepared_root
            )
        proof = self.journal.store.read("migration-proof.json", UpgradeMigrationExecution)
        if proof is not None and proof.failed_unchanged:
            await self.restored_runtime(dependency_states=dependency_states)
            terminal = self.journal.effects.read("core-migration.json", CoreExecutorEffect)
            state = self.journal.state()
            if (
                terminal is None
                or terminal.outcome != "complete"
                or terminal.observed_container_id is None
                or state.recovery_deadline is None
            ):
                raise UpgradeUnavailable("Migration cleanup retains uncertain terminal ownership")
            removal = self.journal.effects.read("core-remove-migration.json", CoreExecutorEffect)
            if removal is None:
                removal = self.journal.intent(
                    CoreExecutorEffect(
                        job_id=state.job_id,
                        generation=state.generation,
                        plan_sha256=state.plan_sha256,
                        effect_id="core-remove-migration",
                        action="remove_migration",
                        container_id=terminal.observed_container_id,
                        image_id=terminal.image_id,
                        captured_at=datetime.now(UTC),
                        deadline=state.recovery_deadline,
                    )
                )
            await self.docker.remove_failed_migration(
                removal, self.journal, self.authority, prepared_root=self.prepared_root
            )
        return await self.restored_runtime(dependency_states=dependency_states)

    async def commit_runtime(self, *, dependency_states: dict[str, str]) -> CoreRuntimeSelection:
        """Commit a fresh physical release after its acknowledged selection decision."""
        operation = await self.client.status()
        latest = self.journal.receipts.latest()
        decision = (
            operation.phase is UpgradePhase.COMMITTING
            and not operation.cancel_requested
            and latest is not None
            and latest.phase is UpgradePhase.COMMITTING
            and latest.outcome == "complete"
            and latest.sequence == operation.receipt_sequence
            and self.journal.receipts.acknowledged_sequence() == latest.sequence
        )
        if not decision and (
            operation.phase not in TERMINAL_UPGRADE_PHASES or operation.finished_at is None
        ):
            raise UpgradeConflict("Runtime commit requires ingested terminal progress")
        selected = (
            await self.selected_runtime(dependency_states=dependency_states, terminal_replay=True)
            if operation.phase in {UpgradePhase.SUCCEEDED, UpgradePhase.COMMITTING}
            else await self.restored_runtime(
                dependency_states=dependency_states, terminal_replay=True
            )
        )
        # Re-read after physical verification; cancellation/terminal state must not
        # be inferred from an earlier response while Docker observations run.
        confirmed = await self.client.status()
        if confirmed != operation:
            raise UpgradeConflict("Terminal progress changed during physical runtime verification")
        self.journal.commit_runtime_selection(selected, confirmed, self.authority)
        return selected

    def _retained_selection_in_window(self, release_id: UUID, deadline: datetime) -> bool:
        retained = self.journal.store.read(
            "runtime-selection-" + str(release_id) + ".json", CoreRuntimeSelection
        )
        return bool(
            retained is not None
            and retained.selected_at < deadline
            and all(
                item.container.observed_at <= retained.selected_at for item in retained.services
            )
            and (
                retained.migration is None
                or retained.migration.container.observed_at <= retained.selected_at
            )
        )

    async def selected_runtime(
        self, *, dependency_states: dict[str, str], terminal_replay: bool = False
    ) -> CoreRuntimeSelection:
        """Produce fresh whole-cohort identity; this is not a commit or maintenance release."""
        self.authority.verify()
        snapshot = self.journal.source_snapshot()
        sources = {item.service: item for item in snapshot.services}
        prior = (
            {item.container.service: item for item in self.docker.source_selection.services}
            if self.docker.source_selection is not None
            else {}
        )
        selected: list[CoreSelectedService] = []
        for role in self.journal.plan.target.manifest.services:
            if role.service in self.journal.plan.operation.cohort:
                effect = self.journal.effects.read(
                    "core-start-candidate-" + role.service + ".json", CoreExecutorEffect
                )
                if effect is None or effect.outcome != "complete" or effect.container_id is None:
                    raise UpgradeUnavailable("Selected candidate has no completed owned start")
                name = next(
                    name
                    for name, service in self.docker.candidate_names.items()
                    if service.service == role.service
                )
                observed = await self.docker.inspect_verified_candidate(
                    name, self.prepared_root, dependency_states=dependency_states
                )
                if observed.container_id != effect.container_id:
                    raise UpgradeConflict("Selected candidate differs from its owned start")
                item = CoreSelectedService(
                    container=observed,
                    origin_job_id=self.journal.plan.operation.job_id,
                    origin_generation=self.journal.plan.operation.generation,
                    origin_plan_sha256=self.journal.plan.authority_sha256(),
                )
            else:
                source = sources.get(role.service)
                if source is None:
                    raise UpgradeConflict("Selected cohort introduces an unsupported service")
                observed = await self.docker.inspect_verified_source(
                    source.name, self.prepared_root, dependency_states=dependency_states
                )
                if observed.container_id != source.container_id:
                    raise UpgradeConflict("Selected unchanged Source container differs")
                item = (
                    CoreSelectedService.model_validate(
                        prior[role.service].model_dump() | {"container": observed}
                    )
                    if role.service in prior
                    else CoreSelectedService(container=observed)
                )
            if (
                observed.image_id != role.image_id
                or not observed.running
                or observed.health != "healthy"
            ):
                raise UpgradeConflict("Selected whole cohort is not healthy at its exact images")
            selected.append(item)
        migration = (
            await self.docker.inspect_selected_migration(
                self.prepared_root, candidate=self.journal.plan.needs_migration
            )
            if self.docker.migration_source_name in self.docker.migration_roles
            or self.journal.plan.needs_migration
            else None
        )
        await self.verify_schema(restored=False)
        now = datetime.now(UTC)
        if any(
            not 0 <= (now - item.container.observed_at).total_seconds() <= 5 for item in selected
        ):
            raise UpgradeUnavailable("Selected whole-cohort observations are not fresh")
        state = self.journal.state()
        last_service = next(
            service
            for service in reversed(COHORT_ORDER)
            if service in self.journal.plan.operation.cohort
        )
        last_window = next(
            (item for item in state.service_windows if item.service == last_service), None
        )
        deadlines = [
            value
            for value in (
                last_window.deadline_at if last_window is not None else None,
                state.outage_deadline,
            )
            if value is not None
        ]
        if (
            last_window is None
            or state.recovery_captured_at is not None
            or (
                now >= min(deadlines)
                and not (
                    terminal_replay
                    and self._retained_selection_in_window(
                        self.journal.plan.target.release_id, min(deadlines)
                    )
                )
            )
        ):
            raise UpgradeUnavailable("Candidate selection exceeded its retained activation window")
        manifest = self.journal.plan.target.manifest
        if manifest.compose_sha256 is None or manifest.host_release_key is None:
            raise UpgradeConflict("Selected candidate has no frozen host release binding")
        self.authority.verify()
        selection = CoreRuntimeSelection(
            project=self.authority.claim.project,
            release_id=self.journal.plan.target.release_id,
            manifest_sha256=self.journal.plan.target.manifest_sha256,
            compose_sha256=manifest.compose_sha256,
            host_release_key=manifest.host_release_key,
            owner_identity=self.authority.claim.owner_identity,
            generation=self.authority.claim.generation,
            selected_at=now,
            services=tuple(selected),
            migration=migration,
        )
        return self.journal.retain_runtime_selection(selection, self.authority)

    async def restored_runtime(
        self, *, dependency_states: dict[str, str], terminal_replay: bool = False
    ) -> CoreRuntimeSelection:
        """Retain fresh exact Source identities after restoration, keeping ownership held."""
        self.authority.verify()
        snapshot = self.journal.source_snapshot()
        prior = (
            {item.container.service: item for item in self.docker.source_selection.services}
            if self.docker.source_selection is not None
            else {}
        )
        selected: list[CoreSelectedService] = []
        for source in snapshot.services:
            observed = await self.docker.inspect_verified_source(
                source.name, self.prepared_root, dependency_states=dependency_states
            )
            if (
                observed.container_id,
                observed.image_id,
                observed.running,
                observed.health,
            ) != (source.container_id, source.image_id, True, "healthy"):
                raise UpgradeConflict(
                    "Source selection differs from the retained healthy container"
                )
            selected.append(
                CoreSelectedService.model_validate(
                    prior[source.service].model_dump() | {"container": observed}
                )
                if source.service in prior
                else CoreSelectedService(container=observed)
            )
        migration_proof = self.journal.store.read("migration-proof.json", UpgradeMigrationExecution)
        migration = (
            await self.docker.inspect_selected_migration(
                self.prepared_root,
                candidate=self.journal.plan.needs_migration
                and migration_proof is not None
                and migration_proof.state == "complete",
            )
            if self.docker.migration_source_name in self.docker.migration_roles
            else None
        )
        await self.verify_schema(restored=True)
        now = datetime.now(UTC)
        state = self.journal.state()
        if state.recovery_deadline is None or (
            now >= state.recovery_deadline
            and not (
                terminal_replay
                and self._retained_selection_in_window(
                    self.journal.plan.source.release_id, state.recovery_deadline
                )
            )
        ):
            raise UpgradeUnavailable("Source selection exceeds its retained recovery deadline")
        release = self.journal.plan.source
        if release.manifest.compose_sha256 is None or release.manifest.host_release_key is None:
            raise UpgradeConflict("Source selection has no frozen host release binding")
        self.authority.verify()
        return self.journal.retain_runtime_selection(
            CoreRuntimeSelection(
                project=self.authority.claim.project,
                release_id=release.release_id,
                manifest_sha256=release.manifest_sha256,
                compose_sha256=release.manifest.compose_sha256,
                host_release_key=release.manifest.host_release_key,
                owner_identity=self.authority.claim.owner_identity,
                generation=self.authority.claim.generation,
                selected_at=now,
                services=tuple(selected),
                migration=migration,
            ),
            self.authority,
        )

    def migration_effect(
        self,
        action: str,
        *,
        container_id: str | None = None,
    ) -> CoreExecutorEffect:
        """Retain the original effect and deadline across delivery and executor restart."""
        if action not in {"migration", "create_migration", "start_migration"}:
            raise UpgradeForbidden("Migration dispatch action is unsupported")
        state = self.journal.state()
        activation = next(
            (item for item in state.activations if item.grant.boundary == "migration"), None
        )
        if activation is None:
            raise UpgradeForbidden("Migration has no retained consumed activation")
        role = self.docker.migration_roles.get(self.docker.migration_candidate_name)
        if role is None:
            raise UpgradeUnavailable("Migration image has no verified prepared role")
        effect_id = "core-" + action.replace("_", "-")
        existing = self.journal.effects.read(effect_id + ".json", CoreExecutorEffect)
        if existing is not None:
            if (
                existing.action != action
                or existing.container_id != container_id
                or existing.image_id != role.image
            ):
                raise UpgradeConflict("Retained migration effect identity differs")
            return self.journal.intent(existing)
        effect = CoreExecutorEffect.model_validate(
            {
                "job_id": state.job_id,
                "generation": state.generation,
                "plan_sha256": state.plan_sha256,
                "effect_id": effect_id,
                "action": action,
                "container_id": container_id,
                "image_id": role.image,
                "captured_at": datetime.now(UTC),
                "deadline": activation.intent.occurred_at
                + timedelta(seconds=self.journal.plan.operation.timing.readiness_s),
            }
        )
        return self.journal.intent(effect)

    async def migrate(self, *, dependency_states: dict[str, str]) -> UpgradeMigrationExecution:
        completion = self.migration_effect("migration")
        if completion.outcome == "complete":
            return await self._migrate(dependency_states=dependency_states)
        remaining = (completion.deadline - datetime.now(UTC)).total_seconds()
        if remaining <= 0:
            raise UpgradeUnavailable(
                "Migration exceeded its retained deadline; maintenance remains"
            )
        try:
            async with asyncio.timeout(remaining):
                return await self._migrate(dependency_states=dependency_states)
        except TimeoutError:
            raise UpgradeUnavailable(
                "Migration exceeded its retained deadline; maintenance remains"
            ) from None

    async def _migrate(self, *, dependency_states: dict[str, str]) -> UpgradeMigrationExecution:
        """Finish a single accepted one-shot attempt; uncertainty never permits another DDL run."""
        if not self.journal.plan.needs_migration:
            raise UpgradeForbidden("Accepted plan does not require a migration")
        with tracer.start_as_current_span("coire.upgrade_executor.migrate"):
            self.authority.verify()
            completion = self.migration_effect("migration")
            if completion.outcome == "complete":
                proof = self.journal.store.read("migration-proof.json", UpgradeMigrationExecution)
                if proof is None:
                    raise UpgradeUnavailable("Completed migration has no retained schema proof")
                observed = await self.docker.inspect_migration(
                    prepared_root=self.prepared_root, dependency_states=dependency_states
                )
                self.journal.complete_migration(completion.effect_id, observed, proof)
                if proof.state != "complete":
                    raise UpgradeConflict(
                        "Migration failed with positively unchanged Source schema"
                    )
                return proof
            created = await self.docker.create_migration(
                self.migration_effect("create_migration"),
                self.journal,
                self.authority,
                prepared_root=self.prepared_root,
                dependency_states=dependency_states,
            )
            self.journal.uncertain(completion.effect_id)
            await self.docker.start_migration(
                self.migration_effect("start_migration", container_id=created.container_id),
                self.journal,
                self.authority,
                prepared_root=self.prepared_root,
                dependency_states=dependency_states,
            )
            while datetime.now(UTC) < completion.deadline:
                self.authority.verify()
                observed = await self.docker.inspect_migration(
                    prepared_root=self.prepared_root, dependency_states=dependency_states
                )
                if not observed.running:
                    if observed.status != "exited":
                        raise UpgradeConflict("Migration did not terminate successfully")
                    proof = await self.client.migration()
                    self.journal.complete_migration(completion.effect_id, observed, proof)
                    if proof.state != "complete":
                        raise UpgradeConflict(
                            "Migration failed with positively unchanged Source schema"
                        )
                    return proof
                remaining = (completion.deadline - datetime.now(UTC)).total_seconds()
                if remaining > 0:
                    await asyncio.sleep(min(1, remaining))
            raise UpgradeUnavailable(
                "Migration exceeded its retained deadline; maintenance remains"
            )
