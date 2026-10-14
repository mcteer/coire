"""Real local maintenance closure and write-ahead fault replay; native facts stubbed."""

from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import pytest
from test_upgrade_recovery_cold_handback import perform_source_handback_fixture

from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrade_recovery import (
    UpgradeExpiredRecoveryColdClosure,
    UpgradeExpiredRecoveryColdSupervisionIntent,
    UpgradeExpiredRecoveryColdSupervisionReceipt,
)
from coire_core.models.upgrades import (
    NodeAgentProcessIdentity,
    UpgradeInventoryEntry,
    UpgradeMaintenance,
)
from coire_core.upgrade_journal import PrivateUpgradeStore


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "intent",
        "maintenance",
        "receipt",
        "revoked",
        "work",
        "slow_post_release",
        "compact_tamper",
    ],
)
@pytest.mark.parametrize(
    "continuation",
    [False, True, "post_handback", "inherited"],
    ids=["cold", "continuation", "post_handback", "inherited"],
)
def test_source_closure_retains_core_authority_and_reconciles_exact_local_release(
    tmp_path, monkeypatch, fault, continuation
):
    from coire_node import expired_recovery_cold_closure as worker

    def exercise(settings, owner, bootstrap, run, handback, control):
        if continuation or fault == "slow_post_release":
            now = handback.observed_at + timedelta(seconds=1)

            class Clock(datetime):
                @classmethod
                def now(cls, tz=UTC):
                    return now

            monkeypatch.setattr(__import__(__name__), "datetime", Clock)
            monkeypatch.setattr(worker, "datetime", Clock)
            from coire_node import maintenance

            monkeypatch.setattr(maintenance, "datetime", Clock)
        supervision = UpgradeExpiredRecoveryColdSupervisionReceipt(
            supervision=UpgradeExpiredRecoveryColdSupervisionIntent(
                handback=handback, started_at=datetime.now(UTC)
            ),
            process=NodeAgentProcessIdentity(
                pid=98767,
                create_time_s=datetime.now(UTC).timestamp(),
                environment_sha256=owner.runtime.environment_sha256,
            ),
            kind="fixture",
            observed_at=datetime.now(UTC),
        )
        run.write("cold.supervision.receipt.json", supervision)
        delayed = False

        def verify_supervisor(*_):
            nonlocal now, delayed
            if fault == "slow_post_release" and not delayed:
                current = PrivateUpgradeStore(tmp_path / "state/upgrades").read(
                    "maintenance.json", UpgradeMaintenance
                )
                if current.state == "released":
                    now += timedelta(seconds=8)
                    from coire_core.models.upgrades import NativeGuardStatus

                    status = bootstrap.read("status.json", NativeGuardStatus)
                    bootstrap.write("status.json", status.model_copy(update={"observed_at": now}))
                    delayed = True

        monkeypatch.setattr(worker, "verify_fixture_supervisor", verify_supervisor)
        control.accept_cold_recovery_handback_receipt = Mock(side_effect=lambda value: value)
        from coire_core.models.upgrade_recovery import (
            UpgradeExpiredRecoveryColdCollectiveArchiveReceipt,
        )

        archive = run.read(
            "cold.collective.archive.receipt.json",
            UpgradeExpiredRecoveryColdCollectiveArchiveReceipt,
        )
        inventory = archive.archive.local
        control.local_inventory = Mock(
            side_effect=lambda *a, **kw: inventory.model_copy(
                update={
                    "owners": tuple(
                        o.model_copy(
                            update={
                                "entries": (
                                    UpgradeInventoryEntry(
                                        owner="requests",
                                        kind="admission",
                                        work_id="inflight",
                                        state="active",
                                    ),
                                )
                                if fault == "work" and o.owner == "requests"
                                else ()
                            }
                        )
                        for o in inventory.owners
                    ),
                    "observed_at": datetime.now(UTC),
                }
            )
        )
        node = PrivateUpgradeStore(tmp_path / "state/upgrades", create=False)
        before = node.read("maintenance.json", UpgradeMaintenance)
        if fault == "revoked":
            control.accept_cold_recovery_handback_receipt.side_effect = UpgradeUnavailable(
                "revoked"
            )
        from pathlib import Path

        from coire_node.upgrade_executor import NativeDiskReservation

        reservations = PrivateUpgradeStore(
            Path(settings.upgrade_environment_root) / ".upgrade-reservations"
        )
        staging = NativeDiskReservation(
            reservations.root / owner.intent.request.original_job_id, owner.intent.original_plan
        )
        staging.reserve(1024 * 1024)
        original_write = PrivateUpgradeStore.write
        lost = False

        def write(store, key, value, **kw):
            nonlocal lost
            result = original_write(store, key, value, **kw)
            expected = {
                "intent": "cold.closure.intent.json",
                "maintenance": "maintenance.json",
                "receipt": "cold.closure.receipt.json",
            }.get(fault)
            if key == expected and not lost:
                lost = True
                raise UpgradeUnavailable("fixture lost closure response")
            return result

        monkeypatch.setattr(PrivateUpgradeStore, "write", write)
        if fault in {"revoked", "work"}:
            with pytest.raises(UpgradeUnavailable):
                worker.close_cold_source(settings, supervision, control)
            assert node.read("maintenance.json", UpgradeMaintenance) == before
            return
        if fault in {"intent", "maintenance", "receipt"}:
            with pytest.raises(UpgradeUnavailable, match="lost closure"):
                worker.close_cold_source(settings, supervision, control)
        result = worker.close_cold_source(settings, supervision, control)
        assert worker.close_cold_source(settings, supervision, control) == result
        assert run.read("cold.closure.receipt.json", UpgradeExpiredRecoveryColdClosure) == result
        after = node.read("maintenance.json", UpgradeMaintenance)
        assert after.state == "released"
        assert staging.current().state == "released"
        assert staging._file(staging.current()) is None
        assert before.model_dump(exclude={"state", "updated_at"}) == after.model_dump(
            exclude={"state", "updated_at"}
        )
        assert result.local_release.released
        if fault == "slow_post_release":
            assert delayed
            assert (result.observed_at - result.local_release.applied_at).total_seconds() == 8
            assert result.local_observation.maintenance == after
            assert after.updated_at == result.local_release.applied_at
        from coire_core.models.upgrade_recovery import UpgradeColdClosureSupervision

        assert (
            result.handback == handback
            and result.supervision == UpgradeColdClosureSupervision.from_receipt(supervision)
        )
        from coire_core.upgrade_journal import MAX_DOCUMENT_BYTES

        assert MAX_DOCUMENT_BYTES == 512 * 1024
        compact_bytes = result.model_dump_json().encode()
        legacy = UpgradeExpiredRecoveryColdClosure.model_validate(
            result.model_copy(update={"supervision": supervision}).model_dump(mode="json")
        )
        assert len(compact_bytes) < len(legacy.model_dump_json().encode()) < MAX_DOCUMENT_BYTES
        assert UpgradeExpiredRecoveryColdClosure.model_validate_json(compact_bytes) == result
        assert (
            UpgradeExpiredRecoveryColdClosure.model_validate_json(legacy.model_dump_json())
            == legacy
        )
        if fault == "compact_tamper":
            run.write(
                "cold.closure.receipt.json",
                result.model_copy(
                    update={
                        "supervision": result.supervision.model_copy(
                            update={"receipt_sha256": "a" * 64}
                        )
                    }
                ),
            )
            with pytest.raises(UpgradeConflict, match="retained history"):
                worker.close_cold_source(settings, supervision, control)
            return
        if (
            owner.intent.local_recovery_maintenance is not None
            or owner.intent.inherited_local_maintenance is not None
        ):
            from pydantic import ValidationError

            from coire_core.models.upgrade_recovery import (
                UpgradeExpiredRecoveryColdResolutionObservation,
                UpgradeExpiredRecoveryColdResolutionRequest,
            )

            observation = UpgradeExpiredRecoveryColdResolutionObservation(
                closure_sha256=result.sha256(),
                status=result.status,
                node=result.node,
                maintenance=after,
                observed_at=result.observed_at,
            )
            UpgradeExpiredRecoveryColdResolutionRequest(closure=result, observation=observation)
            old = (
                owner.continuation.post_handback.released_maintenance
                if owner.continuation.post_handback is not None
                else owner.intent.original_plan.maintenance.model_copy(update={"state": "released"})
            )
            with pytest.raises(ValidationError):
                UpgradeExpiredRecoveryColdResolutionRequest(
                    closure=result, observation=observation.model_copy(update={"maintenance": old})
                )
        with pytest.raises(UpgradeConflict), bootstrap.ownership("recovery-transfer.lock"):
            pass

    perform_source_handback_fixture(
        tmp_path,
        monkeypatch,
        callback=exercise,
        supervision="fixture",
        continuation=continuation,
        post_handback=continuation == "post_handback",
        inherited=continuation == "inherited",
    )
