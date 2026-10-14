"""Lost-create recovery only reads the original fixed name and preserves first detection."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import httpx
import pytest
from test_handover_creation_journal import preparation
from upgrade_bootstrap_fixtures import prepared_bootstrap

from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrades import CoreHandoverSummary
from coire_core.settings import Settings
from coire_upgrade_executor.client import ExecutorClient
from coire_upgrade_executor.docker import DockerRead
from coire_upgrade_executor.handover_creation import HandoverCreationJournal
from coire_upgrade_executor.handover_source_supervisor import reconcile_dispatched_creation


@pytest.mark.parametrize(
    "fault", [None, "absent", "unavailable", "incumbent", "configuration", "committed"]
)
async def test_creation_recovery_cannot_recreate_or_assume_absence_is_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str | None,
) -> None:
    import coire_upgrade_executor.handover_source_supervisor as module

    prep = preparation()
    prep = prep.model_copy(
        update={
            "prepared_at": prep.prepared_at - timedelta(seconds=241),
            "deadline_at": prep.deadline_at - timedelta(seconds=241),
        }
    )
    creation = HandoverCreationJournal(tmp_path / "handover/2", prep)
    initial = creation.begin(now=prep.prepared_at)
    creation.dispatch(initial, now=prep.prepared_at)
    detected = datetime.now(UTC) - timedelta(seconds=20)
    original_failure = creation.begin_creation_recovery(now=detected)
    assert creation.begin_creation_recovery() == original_failure
    settings = Settings(upgrades_enabled=True)
    monkeypatch.setattr(module, "verify_executor_namespace", lambda seed: None)
    _, physical = prepared_bootstrap(prep.incumbent.source)
    physical = physical.model_copy(update={"seed_sha256": prep.incumbent.sha256()})
    source = Mock(observe=AsyncMock(return_value=physical))
    monkeypatch.setattr(module, "InitialBootstrapObserver", Mock(return_value=source))
    observer = Mock()

    async def verify(identity: str, **kwargs: bool) -> tuple[datetime, datetime]:
        assert identity == "a" * 64
        assert kwargs == {"require_healthy": False, "require_running": False}
        if fault == "configuration":
            raise UpgradeConflict("frozen candidate configuration differs")
        at = datetime.now(UTC)
        observer.verified_reads[f"/containers/{identity}/json"] = DockerRead({"Id": identity}, at)
        return at, at

    observer.verify_container = AsyncMock(side_effect=verify)
    monkeypatch.setattr(module, "RecoveryObserver", Mock(return_value=observer))
    summary = CoreHandoverSummary(
        handover_id=uuid4(),
        project="coire",
        generation=prep.generation,
        phase="committed" if fault == "committed" else "passive",
        preparation_sha256=prep.sha256(),
        candidate_release_id=prep.candidate_release.release_id,
        incumbent_image_id=prep.incumbent.executor_image_id,
        candidate_image_id=prep.candidate_image_id,
        prepared_at=prep.prepared_at,
        deadline_at=prep.deadline_at,
    )
    api = Mock(spec=ExecutorClient)
    api.plan = None
    api.handover_status = AsyncMock(return_value=summary)
    requests: list[str] = []

    def transport(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == f"/v1.46/containers/{prep.candidate_name}/json"
        requests.append(request.url.path)
        if fault == "absent":
            return httpx.Response(404)
        if fault == "unavailable":
            return httpx.Response(503)
        return httpx.Response(
            200,
            json={"Id": prep.incumbent.executor_container_id if fault == "incumbent" else "a" * 64},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport), trust_env=False) as http:
        if fault:
            with pytest.raises((UpgradeConflict, UpgradeUnavailable)):
                await reconcile_dispatched_creation(settings, creation, api, http)
            retained = creation.current()
            assert retained is not None and retained.outcome == "dispatched"
        else:
            await reconcile_dispatched_creation(settings, creation, api, http)
            observed = creation.current()
            assert observed is not None and observed.outcome == "observed"
            assert observed.candidate_container_id == "a" * 64
    assert len(requests) == (0 if fault == "committed" else 1)
    assert (
        creation.begin_creation_recovery(now=datetime.now(UTC) + timedelta(seconds=600))
        == original_failure
    )
    assert original_failure.deadline_at == detected + timedelta(seconds=300)
    assert not creation.store.path("candidate-start.json").exists()
    assert not creation.store.path("transfer-intent.json").exists()
    assert not creation.store.path("candidate-seed.json").exists()


def test_retained_creation_recovery_cannot_predate_accepted_preparation(tmp_path: Path) -> None:
    from coire_core.models.upgrades import CoreHandoverCreationRecoveryIntent

    prep = preparation()
    creation = HandoverCreationJournal(tmp_path / "handover", prep)
    creation.store.write(
        "creation-recovery-intent.json",
        CoreHandoverCreationRecoveryIntent(
            preparation_sha256=prep.sha256(),
            generation=prep.generation,
            detected_at=prep.prepared_at - timedelta(seconds=1),
            deadline_at=prep.prepared_at + timedelta(seconds=299),
        ),
    )
    before = creation.store.path("creation-recovery-intent.json").read_bytes()
    with pytest.raises(UpgradeConflict):
        creation.begin_creation_recovery()
    assert creation.store.path("creation-recovery-intent.json").read_bytes() == before
