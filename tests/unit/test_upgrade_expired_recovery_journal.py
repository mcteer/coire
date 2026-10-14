"""Retained new intent is immutable; retries never recapture its budget."""

import json
from datetime import datetime, timedelta

import pytest
from test_upgrade_expired_recovery_contract import intent_payload

from coire_core.errors import UpgradeConflict
from coire_core.models.upgrade_recovery import UpgradeExpiredRecoveryIntent
from coire_core.upgrade_journal import PrivateUpgradeStore


def test_existing_only_store_never_creates_missing_source_paths(tmp_path):
    from coire_core.errors import UpgradeUnavailable

    missing = tmp_path / "missing" / "source"
    with pytest.raises(UpgradeUnavailable):
        PrivateUpgradeStore(missing, create=False)
    assert not missing.parent.exists()


def test_recovery_intent_retention_replays_exact_captured_epoch(tmp_path):
    from coire_core.upgrade_recovery import retain_expired_recovery_intent

    store = PrivateUpgradeStore(tmp_path / "intents")
    value = UpgradeExpiredRecoveryIntent.model_validate_json(json.dumps(intent_payload()))
    assert retain_expired_recovery_intent(store, value) == value
    assert retain_expired_recovery_intent(store, value) == value
    retained = store.read(value.recovery_id + ".json", UpgradeExpiredRecoveryIntent)
    assert retained.deadline_at == value.deadline_at
    assert retained.original_plan == value.original_plan


def test_lost_response_after_durable_write_replays_original_epoch(tmp_path, monkeypatch):
    from coire_core.upgrade_recovery import retain_expired_recovery_intent

    store = PrivateUpgradeStore(tmp_path / "intents")
    intent = UpgradeExpiredRecoveryIntent.model_validate_json(json.dumps(intent_payload()))
    write = store.write

    def lost_response(key, value, *, replace=True):
        write(key, value, replace=replace)
        raise RuntimeError("fixture lost response after durable write")

    with monkeypatch.context() as patch:
        patch.setattr(store, "write", lost_response)
        with pytest.raises(RuntimeError, match="fixture lost response"):
            retain_expired_recovery_intent(store, intent)
    assert retain_expired_recovery_intent(store, intent) == intent
    assert (
        store.read(intent.recovery_id + ".json", UpgradeExpiredRecoveryIntent).deadline_at
        == intent.deadline_at
    )


def test_retry_cannot_recapture_the_new_budget(tmp_path):
    from coire_core.upgrade_recovery import retain_expired_recovery_intent

    store = PrivateUpgradeStore(tmp_path / "intents")
    body = intent_payload()
    first = UpgradeExpiredRecoveryIntent.model_validate_json(json.dumps(body))
    retain_expired_recovery_intent(store, first)
    for field in ("accepted_at", "deadline_at"):
        body[field] = (datetime.fromisoformat(body[field]) + timedelta(seconds=1)).isoformat()
    renewed = UpgradeExpiredRecoveryIntent.model_validate_json(json.dumps(body))
    with pytest.raises(UpgradeConflict):
        retain_expired_recovery_intent(store, renewed)
    assert store.read(first.recovery_id + ".json", UpgradeExpiredRecoveryIntent) == first
