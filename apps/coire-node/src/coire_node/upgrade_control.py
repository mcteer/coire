"""Authenticated node transport persists authority; the independent guard owns effects."""

import os
import sys
from datetime import UTC, datetime
from pathlib import Path

import psutil
from opentelemetry import trace

from coire_core.errors import UpgradeConflict, UpgradeNotFound, UpgradeUnavailable
from coire_core.models.upgrades import (
    TERMINAL_UPGRADE_PHASES,
    NativeGuardStatus,
    NativeNodeProcess,
    NativeRecoveryIntent,
    NativeRuntimeSelection,
    NativeUnstartedClosure,
    NativeUpgradeClaim,
    NodeUpgradeCancel,
    NodeUpgradeCommand,
    NodeUpgradeStatus,
    PreparedNativeRuntime,
    StudioName,
    UpgradeEvidence,
    UpgradeExecutionPlan,
    UpgradePhase,
    UpgradeReceipt,
    UpgradeReleaseRegister,
    release_pair_compatible,
)
from coire_core.settings import Settings
from coire_core.upgrade_artifacts import verify_prepared_release
from coire_core.upgrade_journal import PrivateUpgradeStore
from coire_node.upgrade_guard import NativeGuardJournal, _owned_native_process

tracer = trace.get_tracer("coire.node.upgrade_control")


class NativeUpgradeControl:
    def __init__(self, settings: Settings) -> None:
        if settings.node_name not in {"coire-edge-a", "coire-edge-b"}:
            raise UpgradeUnavailable("Managed upgrade target is not a registered Studio")
        self.settings = settings
        self.node: StudioName = (
            "coire-edge-a" if settings.node_name == "coire-edge-a" else "coire-edge-b"
        )
        self.root = Path(settings.upgrade_bootstrap_root)

    def observed(self) -> NativeRuntimeSelection | None:
        state = Path(self.settings.node_state_dir) / "upgrades"
        if not state.exists():
            return None
        value = PrivateUpgradeStore(state).read("node-process.json", NativeNodeProcess)
        if value is None:
            return None
        if (
            value.selected.node != self.node
            or value.process.pid != os.getpid()
            or value.process.create_time_s != psutil.Process(os.getpid()).create_time()
        ):
            raise UpgradeUnavailable("Node upgrade process observation differs")
        envs = Path(self.settings.upgrade_environment_root)
        environment = envs / value.selected.runtime.environment_id
        if Path(sys.prefix).resolve(strict=True) != environment.resolve(strict=True) or Path(
            sys.executable
        ).resolve(strict=True) != (environment / "bin/python3").resolve(strict=True):
            raise UpgradeUnavailable("Node upgrade observation uses another environment")
        return value.selected

    def _guard_ready(self, observed: NativeRuntimeSelection | None) -> bool:
        if observed is None or self.settings.legacy_network_mode or not self.root.exists():
            return False
        store = PrivateUpgradeStore(self.root)
        status = store.read("status.json", NativeGuardStatus)
        bootstrap = store.read("runtime.json", PreparedNativeRuntime)
        if (
            status is None
            or bootstrap is None
            or status.bootstrap_runtime != bootstrap
            or not status.ready
            or status.node != self.node
            or not 0 <= (datetime.now(UTC) - status.observed_at).total_seconds() <= 5
        ):
            return False
        if (status.selected.runtime, status.selected.release_sha256) != (
            observed.runtime,
            observed.release_sha256,
        ):
            return False
        claim = store.read("claim.json", NativeUpgradeClaim)
        if claim is not None and claim.state == "released" and bootstrap != observed.runtime:
            # A subsequent operation must have a Source-owned helper. In
            # particular, the former Source may now be a rollback candidate.
            return False
        python = (
            Path(self.settings.upgrade_environment_root) / bootstrap.environment_id / "bin/python3"
        )
        return _owned_native_process(status.process, python, "coire_node.upgrade_guard") is not None

    def journal(self, job_id: str) -> NativeGuardJournal:
        if not self.root.exists():
            raise UpgradeNotFound("Native upgrade operation is absent")
        catalog = PrivateUpgradeStore(self.root)
        operations = catalog.root / "operations"
        if not operations.exists():
            raise UpgradeNotFound("Native upgrade operation is absent")
        path = PrivateUpgradeStore(operations).path(job_id)
        if not path.exists():
            raise UpgradeNotFound("Native upgrade operation is absent")
        store = PrivateUpgradeStore(path)
        plan = store.read("plan.json", UpgradeExecutionPlan)
        claim = catalog.read("claim.json", NativeUpgradeClaim)
        accepted = store.read("accepted.json", NativeUpgradeClaim)
        published = claim if claim is not None and claim.job_id == job_id else accepted
        if published is None:
            raise UpgradeNotFound("Native upgrade preparation has not been accepted")
        if plan is None or (
            published.node,
            published.job_id,
            published.generation,
            published.plan_sha256,
        ) != (self.node, job_id, plan.operation.generation, plan.authority_sha256()):
            raise UpgradeUnavailable("Native published operation authority differs")
        intent_path = store.root / "guard"
        if plan is None or not intent_path.exists():
            raise UpgradeUnavailable("Native upgrade operation authority is incomplete")
        intent = PrivateUpgradeStore(intent_path).read("intent.json", NativeRecoveryIntent)
        if intent is None or (intent.job_id, intent.plan_sha256) != (
            job_id,
            plan.authority_sha256(),
        ):
            raise UpgradeUnavailable("Native upgrade operation authority differs")
        return NativeGuardJournal(
            path, plan, intent.source, intent.candidate, intent.source_process
        )

    def status(self, job_id: str | None = None, after_sequence: int = 0) -> NodeUpgradeStatus:
        with tracer.start_as_current_span(
            "coire.node.upgrade_status", record_exception=False, set_status_on_exception=False
        ):
            observed = self.observed()
            ready = self._guard_ready(observed)
            claim = (
                PrivateUpgradeStore(self.root).read("claim.json", NativeUpgradeClaim)
                if self.root.exists()
                else None
            )
            if claim is not None and claim.node != self.node:
                raise UpgradeUnavailable("Native upgrade claim target differs")
            if job_id is None and claim is not None and claim.state == "active":
                job_id = claim.job_id
            values: dict[str, object] = {}
            if (
                job_id is None
                and claim is not None
                and claim.state == "released"
                and observed is not None
            ):
                journal = self.journal(claim.job_id)
                intent = journal.current()
                if (
                    intent.phase == "committed"
                    and observed.release_sha256 == journal.plan.target.manifest_sha256
                ):
                    from coire_node.upgrade_runtime import verify_prepared_runtime

                    envs = Path(self.settings.upgrade_environment_root)
                    try:
                        verify_prepared_runtime(
                            envs, envs.parent / "runtimes", intent.source.runtime
                        )
                    except UpgradeUnavailable:
                        values["reason"] = "retained_source_unavailable"
                    else:
                        values["retained_source_sha256"] = journal.plan.source.manifest_sha256
                        values["retained_job_id"] = claim.job_id
            if job_id is not None:
                closure = (
                    PrivateUpgradeStore(self.root).read(
                        "unstarted-" + job_id + ".json", NativeUnstartedClosure
                    )
                    if self.root.exists()
                    else None
                )
                if closure is None:
                    journal = self.journal(job_id)
                    receipts = journal.receipts.page(after_sequence)
                    latest = journal.receipts.latest()
                    generation = journal.plan.operation.generation
                    phase = latest.phase if latest else journal.plan.operation.phase
                    source_sha: str | None = journal.plan.source.manifest_sha256
                else:
                    if closure.node != self.node or closure.receipt.job_id != job_id:
                        raise UpgradeUnavailable("Native unstarted closure scope differs")
                    receipts = (closure.receipt,) if after_sequence < 1 else ()
                    latest = closure.receipt
                    generation = latest.generation
                    phase = latest.phase
                    source_sha = latest.observed_sha256
                values = {
                    "job_id": job_id,
                    "generation": generation,
                    "receipts": receipts,
                    "receipt_sequence": latest.sequence if latest else 0,
                    "phase": phase,
                    "source_sha256": source_sha,
                }
            return NodeUpgradeStatus.model_validate(
                {
                    "node": self.node,
                    "release_sha256": observed.release_sha256 if observed else None,
                    "runtime_sha256": observed.runtime.environment_sha256 if observed else None,
                    "environment_id": observed.runtime.environment_id if observed else None,
                    "bootstrap_ready": ready,
                    "reason": None if ready else "upgrade_bootstrap_unready",
                    "observed_at": datetime.now(UTC),
                    **values,
                }
            )

    def accept(self, command: NodeUpgradeCommand) -> NodeUpgradeStatus:
        operation = command.operation
        if operation.node != self.node:
            raise UpgradeConflict("Node upgrade command target differs")
        observed = self.observed()
        if not self.root.exists():
            raise UpgradeUnavailable("Independent native recovery bootstrap is not ready")
        store = PrivateUpgradeStore(self.root)
        with (
            tracer.start_as_current_span(
                "coire.node.upgrade_accept", record_exception=False, set_status_on_exception=False
            ),
            store.ownership("control.lock"),
        ):
            if (
                store.read("unstarted-" + operation.job_id + ".json", NativeUnstartedClosure)
                is not None
            ):
                raise UpgradeConflict("Native preparation was durably cancelled before acceptance")
            claim = store.read("claim.json", NativeUpgradeClaim)
            matching = claim is not None and (
                claim.job_id,
                claim.generation,
                claim.plan_sha256,
            ) == (operation.job_id, operation.generation, command.authority_sha256())
            if claim is not None and claim.state == "active" and not matching:
                raise UpgradeConflict("Previous native operation retains ownership")
            if matching and claim is not None:
                journal = self.journal(operation.job_id)
                if command.activation_grant is not None:
                    with journal.root.ownership("command.lock"):
                        latest = journal.receipts.latest()
                        existing = journal.store.read("activation.json", NodeUpgradeCommand)
                        if existing is not None and (
                            existing.activation_grant,
                            existing.activation_receipt,
                            existing.peer_inventory,
                        ) != (
                            command.activation_grant,
                            command.activation_receipt,
                            command.peer_inventory,
                        ):
                            raise UpgradeConflict("Native activation authority changed on replay")
                        if claim.state == "active" and (
                            latest is None or latest.phase not in TERMINAL_UPGRADE_PHASES
                        ):
                            journal.root.write("command.json", command)
                return self.status(operation.job_id)
            if not self._guard_ready(observed) or observed is None:
                raise UpgradeUnavailable("Independent native recovery bootstrap is not ready")
            if (
                command.activation_grant is not None
                or not 0
                < (command.authorization_expires_at - datetime.now(UTC)).total_seconds()
                <= 30
            ):
                raise UpgradeConflict(
                    "First native acceptance requires fresh preparation authority"
                )
            if claim is not None and operation.generation <= claim.generation:
                raise UpgradeConflict("Native operation generation is stale")
            # Refuse an unresolved predecessor before creating a new namespace.
            # Otherwise a refusal leaves an unpublished preparation that neither
            # the active guard nor the ordinary unstarted cancellation owns.
            if claim is not None:
                self.verify_terminal(claim.job_id, claim.generation)
            if (
                observed.release_sha256,
                observed.runtime.environment_id,
                observed.runtime.environment_sha256,
            ) != (
                command.source.manifest_sha256,
                command.source.manifest.environment_id,
                command.source.manifest.environment_sha256,
            ) or not release_pair_compatible(
                command.source, command.target, operation.action, operation.parent_job_id
            ):
                raise UpgradeConflict("Native source identity or persistent compatibility differs")
            for release in (command.source, command.target):
                verified = verify_prepared_release(
                    Path(self.settings.upgrade_prepared_dir),
                    UpgradeReleaseRegister(
                        prepared_artifact_id=release.prepared_artifact_id,
                        expected_sha256=release.manifest_sha256,
                    ),
                )
                if verified != release:
                    raise UpgradeConflict("Native prepared release authority differs")
            envs = Path(self.settings.upgrade_environment_root)
            environment_id = command.target.manifest.environment_id
            if environment_id is None:
                raise UpgradeUnavailable("Native prepared environment identity is absent")
            candidate = PrivateUpgradeStore(envs / ".identities").read(
                environment_id + ".json", PreparedNativeRuntime
            )
            if (
                candidate is None
                or candidate.environment_sha256 != command.target.manifest.environment_sha256
            ):
                raise UpgradeUnavailable("Native prepared candidate identity differs")
            process = PrivateUpgradeStore(Path(self.settings.node_state_dir) / "upgrades").read(
                "node-process.json", NativeNodeProcess
            )
            if process is None or process.selected != observed:
                raise UpgradeUnavailable("Native source process changed before acceptance")
            value = NativeUpgradeClaim(
                job_id=operation.job_id,
                generation=operation.generation,
                node=self.node,
                plan_sha256=command.authority_sha256(),
                updated_at=datetime.now(UTC),
            )
            operations = PrivateUpgradeStore(store.root / "operations")
            target = PrivateUpgradeStore(operations.path(operation.job_id))
            plan = UpgradeExecutionPlan.model_validate(
                command.model_dump(
                    mode="json",
                    exclude={"activation_grant", "activation_receipt", "peer_inventory"},
                )
            )
            previous_plan = target.read("plan.json", UpgradeExecutionPlan)
            if previous_plan is None:
                target.write("plan.json", plan, replace=False)
            elif previous_plan.authority_sha256() != plan.authority_sha256():
                raise UpgradeConflict("Native preparation changed on replay")
            intent_path = target.root / "guard"
            previous_intent = (
                PrivateUpgradeStore(intent_path).read("intent.json", NativeRecoveryIntent)
                if intent_path.exists()
                else None
            )
            journal = NativeGuardJournal(
                target.root,
                plan,
                observed,
                candidate,
                previous_intent.source_process if previous_intent else process.process,
            )
            if previous_intent is not None and previous_intent.source_process != process.process:
                from coire_node.upgrade_runtime import verify_prepared_runtime

                python = verify_prepared_runtime(envs, envs.parent / "runtimes", observed.runtime)
                journal.refresh_source_process(process.process, python)
            previous_command = target.read("command.json", NodeUpgradeCommand)
            if previous_command is None:
                target.write("command.json", command, replace=False)
            elif previous_command.authority_sha256() != command.authority_sha256():
                raise UpgradeConflict("Native preparation command differs")
            # Publication comes last: the guard ignores unclaimed preparation
            # directories, so a lost response can replay metadata safely.
            if store.read("claim.json", NativeUpgradeClaim) != claim:
                raise UpgradeConflict("Native prior owner changed before claim publication")
            store.write("claim.json", value)
            target.write("accepted.json", value)
            return self.status(operation.job_id)

    def cancel(self, job_id: str, command: NodeUpgradeCancel) -> NodeUpgradeStatus:
        try:
            journal = self.journal(job_id)
        except UpgradeNotFound:
            return self.cancel_unstarted(job_id, command)
        if command.generation != journal.plan.operation.generation:
            raise UpgradeConflict("Native cancellation generation differs")
        with (
            tracer.start_as_current_span(
                "coire.node.upgrade_cancel", record_exception=False, set_status_on_exception=False
            ),
            journal.root.ownership("command.lock"),
        ):
            latest = journal.receipts.latest()
            if latest is None or latest.phase not in TERMINAL_UPGRADE_PHASES:
                existing = journal.root.read("cancel.json", NodeUpgradeCancel)
                if existing is None:
                    journal.root.write("cancel.json", command, replace=False)
        return self.status(job_id)

    def cancel_unstarted(self, job_id: str, command: NodeUpgradeCancel) -> NodeUpgradeStatus:
        plan = command.unstarted_plan
        if plan is None or (plan.operation.job_id, plan.operation.node) != (job_id, self.node):
            raise UpgradeNotFound("Native cancellation has no accepted unstarted authority")
        if not self.root.exists():
            raise UpgradeUnavailable("Native cancellation bootstrap is unavailable")
        store = PrivateUpgradeStore(self.root)
        with store.ownership("control.lock"):
            existing = store.read("unstarted-" + job_id + ".json", NativeUnstartedClosure)
            if existing is not None:
                if existing.plan_sha256 != plan.authority_sha256() or existing.node != self.node:
                    raise UpgradeConflict("Native unstarted cancellation authority changed")
                return self.status(job_id)
            self.verify_unclaimed_resources(job_id)
            self.verify_unchanged_source(
                job_id, command.generation, plan.source.manifest.environment_sha256 or ""
            )
            from coire_core.models.upgrades import UpgradeMaintenance
            from coire_node.upgrade_runtime import verify_prepared_runtime

            state_root = Path(self.settings.node_state_dir) / "upgrades"
            if not state_root.exists():
                raise UpgradeConflict("Native unstarted cancellation has no persisted fence")
            maintenance = PrivateUpgradeStore(state_root).read(
                "maintenance.json", UpgradeMaintenance
            )
            if (
                maintenance is None
                or maintenance.state not in {"fencing", "active"}
                or (
                    maintenance.job_id,
                    maintenance.generation,
                )
                != (job_id, command.generation)
            ):
                raise UpgradeConflict("Native unstarted cancellation requires its persisted fence")
            if not maintenance.matches_plan(plan) or self.node not in maintenance.targets:
                raise UpgradeConflict("Native unstarted cancellation fence scope differs")
            observed = self.observed()
            if observed is None or observed.release_sha256 != plan.source.manifest_sha256:
                raise UpgradeConflict("Native unstarted cancellation selected Source differs")
            envs = Path(self.settings.upgrade_environment_root)
            verify_prepared_runtime(envs, envs.parent / "runtimes", observed.runtime)
            receipt = UpgradeReceipt(
                job_id=job_id,
                generation=command.generation,
                sequence=1,
                phase=UpgradePhase.CANCELLED_UNCHANGED,
                effect_id="unstarted-source-verified",
                occurred_at=datetime.now(UTC),
                outcome="complete",
                observed_sha256=plan.source.manifest_sha256,
                reason="authorization_revoked"
                if command.reason == "authorization_revoked"
                else "administrator_cancelled",
                evidence=UpgradeEvidence(
                    runtime_sha256=plan.source.manifest.environment_sha256,
                    source_verified=True,
                    cleanup_verified=True,
                ).model_dump_json(),
            )
            store.write(
                "unstarted-" + job_id + ".json",
                NativeUnstartedClosure(
                    node=self.node, plan_sha256=plan.authority_sha256(), receipt=receipt
                ),
                replace=False,
            )
        return self.status(job_id)

    def verify_terminal(self, job_id: str, generation: int) -> None:
        """Core's terminal assertion cannot release an unfinished local effect."""
        if not self.root.exists():
            raise UpgradeUnavailable("Native terminal bootstrap ownership is unavailable")
        claim = PrivateUpgradeStore(self.root).read("claim.json", NativeUpgradeClaim)
        if claim is None or claim.job_id != job_id:
            if claim is not None and claim.state == "active":
                raise UpgradeConflict("Fenced peer has another active native selection")
            self.verify_unclaimed_resources(job_id)
            return  # A fenced peer has positively absent local selection effects.
        if claim.generation != generation:
            raise UpgradeConflict("Native terminal reconciliation generation differs")
        journal = self.journal(job_id)
        latest = journal.receipts.latest()
        if (
            latest is None
            or latest.phase not in TERMINAL_UPGRADE_PHASES
            or latest.outcome != "complete"
        ):
            raise UpgradeConflict("Native operation has no positive terminal reconciliation")
        proof = UpgradeEvidence.model_validate_json(latest.evidence)
        intent = journal.current()
        success = latest.phase is UpgradePhase.SUCCEEDED
        unchanged = latest.phase in {
            UpgradePhase.FAILED_UNCHANGED,
            UpgradePhase.CANCELLED_UNCHANGED,
        }
        expected_phase = (
            "committed" if success else ("unchanged_verified" if unchanged else "source_verified")
        )
        if intent.phase != expected_phase:
            raise UpgradeConflict("Native recovery owner has not completed reconciliation")
        release = journal.plan.target if success else journal.plan.source
        observed = self.observed()
        if (
            observed is None
            or proof.cleanup_verified is not True
            or latest.observed_sha256 != release.manifest_sha256
            or (observed.release_sha256, observed.runtime.environment_sha256, proof.runtime_sha256)
            != (
                release.manifest_sha256,
                release.manifest.environment_sha256,
                release.manifest.environment_sha256,
            )
        ):
            raise UpgradeConflict("Native terminal release or cleanup proof differs")
        if not success and proof.source_verified is not True:
            raise UpgradeConflict("Native source reconciliation is unverified")
        if unchanged and intent.activation_started_at is not None:
            raise UpgradeConflict("Activated native ownership needs restoration proof")
        if not unchanged and (
            proof.generated_tokens is None
            or proof.generated_tokens < 10
            or proof.output_sha256 is None
            or (proof.model_id, proof.model_variant_id, proof.model_manifest_sha256)
            != (
                journal.plan.model_id,
                journal.plan.model_variant_id,
                journal.plan.model_manifest_sha256,
            )
        ):
            raise UpgradeConflict("Native terminal serving proof differs")
        if not unchanged:
            verification_phase = (
                UpgradePhase.VERIFYING if success else UpgradePhase.VERIFYING_SOURCE
            )
            history = journal.receipts.page(max(0, latest.sequence - 32))
            verification = next(
                (
                    item
                    for item in reversed(history)
                    if item.phase is verification_phase
                    and item.outcome == "complete"
                    and item.observed_sha256 == release.manifest_sha256
                    and item.evidence
                    and item.sequence < latest.sequence
                ),
                None,
            )
            if verification is None:
                raise UpgradeConflict("Native terminal has no durable serving verification")
            prior = UpgradeEvidence.model_validate_json(verification.evidence)
            binding_fields = (
                "check",
                "runtime_sha256",
                "model_id",
                "model_variant_id",
                "model_manifest_sha256",
                "generated_tokens",
                "output_sha256",
                "collective_verified",
                "peer_runtime_sha256",
            )
            if proof.check != "node" or any(
                getattr(prior, field) != getattr(proof, field) for field in binding_fields
            ):
                raise UpgradeConflict("Native terminal differs from durable serving verification")
        if (
            success
            and (
                journal.plan.source.manifest.paired_required
                or journal.plan.target.manifest.paired_required
            )
            and (
                proof.collective_verified is not True
                or proof.peer_runtime_sha256 != journal.plan.peer_runtime_sha256
            )
        ):
            raise UpgradeConflict("Native terminal lacks exact peer collective qualification")
        if not self._guard_ready(observed):
            raise UpgradeUnavailable("Independent native recovery is not reconciled")
        from coire_node.upgrade_cleanup import verify_local_cleanup

        verify_local_cleanup(
            self.settings,
            journal,
            None if unchanged else ("candidate-serving" if success else "source-serving"),
            proof,
        )

    def verify_unchanged_source(self, job_id: str, generation: int, runtime_sha256: str) -> None:
        """A source-preserving abort may retain known pre-existing work."""
        observed = self.observed()
        if (
            observed is None
            or observed.runtime.environment_sha256 != runtime_sha256
            or not self._guard_ready(observed)
        ):
            raise UpgradeUnavailable("Unchanged native source is not physically ready")
        claim = PrivateUpgradeStore(self.root).read("claim.json", NativeUpgradeClaim)
        if claim is None:
            self.verify_unclaimed_resources(job_id)
            return  # Core-authorized peer with positively absent local selection effects.
        if claim.state == "active" and (claim.job_id, claim.generation) != (job_id, generation):
            raise UpgradeConflict("Unchanged native source has another active operation")
        if claim.job_id != job_id:
            self.verify_unclaimed_resources(job_id)
            return
        journal = self.journal(job_id)
        latest = journal.receipts.latest()
        if latest is None or latest.phase not in {
            UpgradePhase.FAILED_UNCHANGED,
            UpgradePhase.CANCELLED_UNCHANGED,
        }:
            raise UpgradeConflict("Native operation did not terminate with an unchanged source")
        self.verify_terminal(job_id, generation)

    def verify_unclaimed_resources(self, job_id: str) -> None:
        """A missing claim cannot erase an unpublished journal or a physical disk hold."""
        root = PrivateUpgradeStore(self.root)
        operations = root.path("operations")
        if operations.exists() or operations.is_symlink():
            operation = PrivateUpgradeStore(operations).path(job_id)
            if operation.exists() or operation.is_symlink():
                raise UpgradeConflict("Unclaimed native preparation requires reconciliation")
        reservations = Path(self.settings.upgrade_environment_root) / ".upgrade-reservations"
        if reservations.exists() or reservations.is_symlink():
            reservation = PrivateUpgradeStore(reservations).path(job_id)
            if reservation.exists() or reservation.is_symlink():
                raise UpgradeConflict("Unclaimed native disk ownership requires reconciliation")

    def release_claim(self, job_id: str, generation: int) -> None:
        if not self.root.exists():
            return
        store = PrivateUpgradeStore(self.root)
        with store.ownership("control.lock"):
            claim = store.read("claim.json", NativeUpgradeClaim)
            if claim is None or claim.job_id != job_id:
                return
            if claim.generation != generation:
                raise UpgradeConflict("Native claim release epoch differs")
            if claim.state != "released":
                self.verify_terminal(job_id, generation)
            journal = self.journal(job_id)
            observed = self.observed()
            if observed is None:
                raise UpgradeUnavailable("Native claim closure has no physical selection")
            # Retain the exact terminal baseline before retiring the active claim.
            # A crash here still leaves the per-operation recovery owner authoritative.
            store.write("selection.json", observed)
            closed = (
                claim
                if claim.state == "released"
                else claim.model_copy(update={"state": "released", "updated_at": datetime.now(UTC)})
            )
            journal.root.write("accepted.json", closed)
            if claim.state != "released":
                store.write("claim.json", closed)
