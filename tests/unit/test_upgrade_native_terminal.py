"""Actual hash-linked journals and custody moves; kernel/service facts are stubbed."""

from datetime import UTC, datetime
from unittest.mock import Mock
from uuid import uuid4

import pytest
from test_upgrade_recovery_cold_handback import perform_source_handback_fixture

from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrade_native_terminal import (
    UpgradeNativeTerminalGrant,
    UpgradeNativeTerminalState,
)
from coire_core.models.upgrade_recovery import (
    UpgradeExpiredRecoveryColdCollectiveArchiveReceipt,
    UpgradeExpiredRecoveryColdResolution,
    UpgradeExpiredRecoveryColdResolutionObservation,
    UpgradeExpiredRecoveryColdSupervisionIntent,
    UpgradeExpiredRecoveryColdSupervisionReceipt,
)
from coire_core.models.upgrades import (
    NodeAgentProcessIdentity,
    NodeMaintenanceAck,
    UpgradeEvent,
    UpgradeEvidence,
    UpgradeMaintenance,
    UpgradeOwnerInventory,
    UpgradePhase,
    UpgradeReceipt,
)
from coire_core.upgrade_journal import PrivateUpgradeStore, UpgradeReceiptJournal
from coire_node import upgrade_native_terminal as native_ack


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "verification_lost",
        "terminal_lost",
        "verification_ack_lost",
        "terminal_ack_lost",
        "intent_lost",
        "custody_lost",
        "release_lost",
        "wrong_authority",
        "unknown_scope",
        "retired_fixture_provenance",
    ],
)
def test_native_acknowledgement_preserves_history_and_references_canonical_resolution(
    tmp_path, monkeypatch, fault
):
    from coire_node import expired_recovery_cold_closure as close
    from coire_node.expired_recovery_cold_owner import observe_fixture_supervisor as observe_real

    def exercise(settings, owner, bootstrap, run, handback, control):
        now = datetime.now(UTC)
        supervision = UpgradeExpiredRecoveryColdSupervisionReceipt(
            supervision=UpgradeExpiredRecoveryColdSupervisionIntent(
                handback=handback, started_at=now
            ),
            process=NodeAgentProcessIdentity(
                pid=98767,
                create_time_s=now.timestamp(),
                environment_sha256=owner.runtime.environment_sha256,
            ),
            kind="fixture",
            observed_at=now,
        )
        run.write("cold.supervision.receipt.json", supervision)
        monkeypatch.setattr(close, "verify_fixture_supervisor", lambda *_: None)
        control.accept_cold_recovery_handback_receipt = lambda *_: handback
        archive = run.read(
            "cold.collective.archive.receipt.json",
            UpgradeExpiredRecoveryColdCollectiveArchiveReceipt,
        )
        assert archive is not None
        control.local_inventory = lambda *a, **kw: archive.archive.local.model_copy(
            update={
                "owners": tuple(
                    o.model_copy(update={"entries": ()}) for o in archive.archive.local.owners
                ),
                "observed_at": datetime.now(UTC),
            }
        )
        closure = close.close_cold_source(settings, supervision, control)
        plan = owner.intent.original_plan
        maintenance = PrivateUpgradeStore(tmp_path / "state/upgrades").read(
            "maintenance.json", UpgradeMaintenance
        )
        before = plan.operation.model_copy(
            update={"phase": UpgradePhase.RECOVERY_REQUIRED, "receipt_sequence": 1}
        )
        evidence = UpgradeEvidence(
            source_verified=True,
            cleanup_verified=True,
            release_sha256=plan.source.manifest_sha256,
            runtime_sha256=plan.source.manifest.environment_sha256,
            peer_runtime_sha256=plan.peer_runtime_sha256,
            collective_verified=False,
        )
        event = UpgradeEvent(
            job_id=plan.operation.job_id,
            generation=plan.operation.generation,
            sequence=2,
            effect_id="expired-recovery-" + owner.intent.recovery_id,
            phase=UpgradePhase.ROLLED_BACK,
            occurred_at=closure.observed_at,
            outcome="complete",
            reason="expired_operation_recovered",
            restoration="verified",
            observed_sha256=plan.source.manifest_sha256,
            evidence=evidence.model_dump_json(),
        )
        after = before.model_copy(
            update={
                "phase": UpgradePhase.ROLLED_BACK,
                "receipt_sequence": 2,
                "reason": "expired_operation_recovered",
                "finished_at": event.occurred_at,
                "updated_at": event.occurred_at,
                "cancellation_allowed": False,
            }
        )
        result = UpgradeExpiredRecoveryColdResolution(
            closure=closure,
            observation=UpgradeExpiredRecoveryColdResolutionObservation(
                closure_sha256=closure.sha256(),
                status=closure.status,
                node=closure.node,
                maintenance=maintenance,
                observed_at=closure.observed_at,
            ),
            peer_release=NodeMaintenanceAck(
                job_id=plan.operation.job_id,
                generation=plan.operation.generation,
                node="coire-edge-b",
                applied_at=closure.observed_at,
                released=True,
            ),
            operation=after,
            before_operation=before,
            event=event,
            resolved_at=event.occurred_at,
        )
        from coire_core.models.upgrade_recovery import UpgradeColdClosureSupervision
        from coire_node.expired_recovery_source_supervisor import linked_supervision_matches

        assert linked_supervision_matches(result, supervision)
        compact = result.model_copy(
            update={
                "closure": result.closure.model_copy(
                    update={"supervision": UpgradeColdClosureSupervision.from_receipt(supervision)}
                )
            }
        )
        assert linked_supervision_matches(compact, supervision)
        foreign = supervision.model_copy(
            update={"process": supervision.process.model_copy(update={"pid": 98768})}
        )
        assert not linked_supervision_matches(compact, foreign)
        run.write("cold.resolution.json", result)
        operation = PrivateUpgradeStore(bootstrap.root / "operations" / plan.operation.job_id)
        journal = UpgradeReceiptJournal(operation.root / "receipts", plan)
        initial = UpgradeReceipt(
            job_id=plan.operation.job_id,
            generation=plan.operation.generation,
            sequence=1,
            effect_id="retained-failure",
            phase=UpgradePhase.RECOVERY_REQUIRED,
            occurred_at=datetime.now(UTC),
            outcome="unknown",
        )
        journal.append(initial)
        original = journal.store.path(journal.key(1)).read_bytes()
        grant = UpgradeNativeTerminalGrant(
            resolution=result, approved_by=uuid4(), approved_at=datetime.now(UTC)
        )
        import os
        import threading
        from pathlib import Path
        from types import SimpleNamespace

        import psutil

        from coire_core.models.upgrade_native_terminal import (
            UpgradeNativeTerminalSupervisorIntent,
            UpgradeNativeTerminalSupervisorReceipt,
        )
        from coire_node import upgrade_native_terminal_supervisor as adopter

        records = PrivateUpgradeStore(bootstrap.root.parent / "fixture-records")
        adopter_intent = UpgradeNativeTerminalSupervisorIntent(
            grant=grant, runtime=owner.runtime, started_at=datetime.now(UTC)
        )
        run.write("native-terminal.supervisor.intent.json", adopter_intent)
        from coire_node.recovery_lease import recovery_supervision_descriptor

        descriptor = os.dup(
            recovery_supervision_descriptor(
                bootstrap, owner.sha256(), records.path("fixture-supervisor.lock")
            )
        )
        with monkeypatch.context() as patch:
            configured = settings.model_copy(
                update={
                    "upgrade_recovery_id": owner.intent.recovery_id,
                    "upgrade_recovery_supervision_fd": descriptor,
                }
            )
            python = (
                Path(settings.upgrade_environment_root)
                / owner.runtime.environment_id
                / "bin/python3"
            )
            patch.setattr(
                adopter,
                "sys",
                SimpleNamespace(prefix=str(python.parent.parent), executable=str(python)),
            )
            patch.setattr(adopter, "current_children", lambda *_: (handback.status, handback.node))
            patch.setattr(adopter, "_owned_native_process", lambda *_: Mock())
            stopped = threading.Event()
            stopped.set()
            adopter.adopt_fixture(configured, stopped)
        os.close(descriptor)
        adopted = run.read(
            "native-terminal.supervisor.receipt.json", UpgradeNativeTerminalSupervisorReceipt
        )
        assert adopted is not None and adopted.process.pid == os.getpid()
        assert adopted.process.create_time_s == psutil.Process(os.getpid()).create_time()
        if fault == "retired_fixture_provenance":
            from coire_core.models.upgrade_native_terminal import UpgradeNativeFixtureRetirement
            from coire_node import expired_recovery_cold_owner as physical

            definition = bootstrap.root.parent / "fixture-supervisor.py"
            definition.write_bytes(b"unchanged private fixture definition")
            definition.chmod(0o600)
            import hashlib

            claim = owner.original_claim
            from coire_core.models.upgrades import NativeUpgradeClaim

            previous_claim = bootstrap.read("claim.json", NativeUpgradeClaim)
            current_native = owner.original_native.model_copy(update={"phase": "recovery_required"})
            snapshot = UpgradeNativeFixtureRetirement(
                claim=claim,
                native=current_native,
                guard=handback.status,
                node_process=handback.node,
                adoption=adopted,
                definition_sha256=hashlib.sha256(definition.read_bytes()).hexdigest(),
                observed_at=datetime.now(UTC),
                retired_at=datetime.now(UTC),
            )
            bootstrap.write("claim.json", claim)
            key = "retired-adopted-" + claim.job_id + ".json"
            records.write(key, snapshot)
            configured = settings.model_copy(
                update={
                    "upgrade_environment_root": str(bootstrap.root.parent / "envs"),
                    "node_state_dir": str(bootstrap.root.parent / "state"),
                }
            )
            checked = []
            with monkeypatch.context() as patch:
                patch.setattr(physical, "verify_prepared_runtime", lambda *_: python)
                patch.setattr(physical, "verify_retired_identity", checked.append)
                proof = observe_real(configured, handback.status, handback.node)
                assert proof.pid == adopted.process.pid
                assert checked == [adopted.process, handback.status.process, handback.node.process]
                records.write(key, snapshot.model_copy(update={"retired_at": None}))
                with pytest.raises(UpgradeConflict):
                    observe_real(configured, handback.status, handback.node)
                records.write(key, snapshot)
                changed = handback.status.model_copy(
                    update={"process": handback.status.process.model_copy(update={"pid": 98769})}
                )
                with pytest.raises(UpgradeConflict):
                    observe_real(configured, changed, handback.node)
                definition.write_bytes(b"foreign fixture definition")
                with pytest.raises(UpgradeConflict):
                    observe_real(configured, handback.status, handback.node)
                definition.write_bytes(b"unchanged private fixture definition")
            records.path(key).unlink()
            assert previous_claim is not None
            bootstrap.write("claim.json", previous_claim)
        # This CPU fixture adopted no real child and spawns no replacement.
        run.path("native-terminal.supervisor.receipt.json").unlink()
        run.path("native-terminal.supervisor.intent.json").unlink()
        transport = Mock()
        transport._read.return_value = UpgradeNativeTerminalState(
            grant=grant, observed_at=datetime.now(UTC)
        )
        monkeypatch.setattr(native_ack, "verify_fixture_supervisor", lambda *_: None)
        monkeypatch.setattr(native_ack, "_owned_native_process", lambda *_: None)
        monkeypatch.setattr(
            native_ack.SupportCancellationControl, "observed", lambda _: closure.node.selected
        )
        monkeypatch.setattr(native_ack.SupportCancellationControl, "_guard_ready", lambda *_: True)
        verified = []
        monkeypatch.setattr(
            native_ack.SupportCancellationControl,
            "verify_terminal",
            lambda self, job, generation: verified.append((job, generation)),
        )
        smoke = handback.helper.handback.verification.request.smoke
        scope_name = f"{plan.operation.job_id}-{plan.operation.generation}-{plan.source.manifest.environment_sha256}-source"
        origins = PrivateUpgradeStore(run.root / "smoke-state/upgrades/smokes")
        origin = PrivateUpgradeStore(origins.root / scope_name)
        origin.write("result.json", smoke.attempt.evidence)
        origin.write(
            "owner.json", UpgradeOwnerInventory(owner="engines", status="complete", entries=())
        )
        PrivateUpgradeStore(tmp_path / "state/upgrades/smokes")
        from pathlib import Path

        from coire_node.upgrade_executor import NativeDiskReservation

        reservations = PrivateUpgradeStore(
            Path(settings.upgrade_environment_root) / ".upgrade-reservations"
        )
        reservation = NativeDiskReservation(reservations.root / plan.operation.job_id, plan)
        reservation.reserve(1024 * 1024)
        if fault == "wrong_authority":
            transport._read.return_value = transport._read.return_value.model_copy(
                update={"grant": grant.model_copy(update={"approved_by": uuid4()})}
            )
        elif fault == "unknown_scope":
            origin.write(
                "owner.json",
                UpgradeOwnerInventory(
                    owner="engines", status="unknown", entries=(), reason="unresolved"
                ),
            )
        append = UpgradeReceiptJournal.append
        interrupted = False

        def lose(self, receipt, **kwargs):
            nonlocal interrupted
            wanted = 2 if fault == "verification_lost" else 3
            if receipt.sequence == wanted and not interrupted:
                interrupted = True
                raise UpgradeUnavailable("lost append response")
            return append(self, receipt, **kwargs)

        if fault in {"verification_lost", "terminal_lost"}:
            monkeypatch.setattr(UpgradeReceiptJournal, "append", lose)
        elif fault in {"verification_ack_lost", "terminal_ack_lost"}:

            def lost_ack(self, receipt, **kwargs):
                nonlocal interrupted
                result = append(self, receipt, **kwargs)
                wanted = 2 if fault == "verification_ack_lost" else 3
                if receipt.sequence == wanted and not interrupted:
                    interrupted = True
                    raise UpgradeUnavailable("lost append acknowledgement")
                return result

            monkeypatch.setattr(UpgradeReceiptJournal, "append", lost_ack)
        elif fault == "custody_lost":
            rename = type(origin.root).rename

            def lost_custody(self, target):
                nonlocal interrupted
                result = rename(self, target)
                if self == origin.root and not interrupted:
                    interrupted = True
                    raise UpgradeUnavailable("lost custody acknowledgement")
                return result

            monkeypatch.setattr(type(origin.root), "rename", lost_custody)
        elif fault == "release_lost":
            release = NativeDiskReservation.release

            def lost_release(self):
                nonlocal interrupted
                result = release(self)
                if not interrupted:
                    interrupted = True
                    raise UpgradeUnavailable("lost release acknowledgement")
                return result

            monkeypatch.setattr(NativeDiskReservation, "release", lost_release)
        elif fault == "intent_lost":
            write = PrivateUpgradeStore.write

            def lost_intent(self, key, value, **kwargs):
                nonlocal interrupted
                result = write(self, key, value, **kwargs)
                if (
                    self.root == operation.root / "guard"
                    and key == "intent.json"
                    and not interrupted
                ):
                    interrupted = True
                    raise UpgradeUnavailable("lost native intent acknowledgement")
                return result

            monkeypatch.setattr(PrivateUpgradeStore, "write", lost_intent)
        if fault in {"wrong_authority", "unknown_scope"}:
            with pytest.raises((UpgradeConflict, UpgradeUnavailable)):
                native_ack.acknowledge_native_terminal(settings, grant, transport)
            assert journal.latest() == initial
            assert origin.root.exists()
            return
        if fault and fault != "retired_fixture_provenance":
            with pytest.raises(UpgradeUnavailable, match="lost"):
                native_ack.acknowledge_native_terminal(settings, grant, transport)
        ack = native_ack.acknowledge_native_terminal(settings, grant, transport)
        replay = native_ack.acknowledge_native_terminal(settings, grant, transport)
        assert replay == ack and ack.resolution_sha256 == result.sha256()
        assert journal.store.path(journal.key(1)).read_bytes() == original
        assert journal.latest() == ack.terminal and ack.terminal.phase is UpgradePhase.ROLLED_BACK
        assert ack.terminal.reason == "expired_operation_recovered"
        assert ack.verification.evidence == ack.terminal.evidence
        assert not origin.root.exists() and verified
        assert result.original_upgrade_succeeded is False

    perform_source_handback_fixture(tmp_path, monkeypatch, callback=exercise, supervision="fixture")
