from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from coire_core.models.failover import (
    FailoverAccessVerifier,
    FailoverMember,
    FailoverMembershipConfig,
    FailoverModel,
    FailoverSnapshot,
)
from coire_failover.resolution import FailoverModelUnavailable, resolve_resident_model


def _snapshot(
    *, entitlement: frozenset[str] = frozenset()
) -> tuple[FailoverSnapshot, FailoverModel]:
    now = datetime.now(UTC)
    model = FailoverModel(
        id=uuid4(),
        slug="tiny-test-model",
        display_name="Tiny test model",
        entitlement=entitlement,
        context_window=2048,
    )
    snapshot = FailoverSnapshot(
        snapshot_id=uuid4(),
        issued_at=now,
        expires_at=now + timedelta(minutes=1),
        membership=FailoverMembershipConfig(
            epoch=1,
            signing_key_id="core-1",
            members=[
                FailoverMember(name="coire-core", priority=0, public_key="core"),
                FailoverMember(name="coire-edge-a", priority=1, public_key="edge-a"),
                FailoverMember(name="coire-edge-b", priority=2, public_key="edge-b"),
            ],
        ),
        models=[model],
        access_verifier=FailoverAccessVerifier(
            issuer="https://team.cloudflareaccess.com",
            audience="aud",
            jwks_url="https://team/certs",
        ),
        signature="signature",
    )
    return snapshot, model


def test_resolution_requires_registry_authority_and_live_residency() -> None:
    snapshot, model = _snapshot()
    assert (
        resolve_resident_model(snapshot, model.id, resident_model_ids=frozenset({model.id}))
        == model
    )
    with pytest.raises(FailoverModelUnavailable):
        resolve_resident_model(snapshot, model.id, resident_model_ids=frozenset())
    with pytest.raises(FailoverModelUnavailable):
        resolve_resident_model(snapshot, uuid4(), resident_model_ids=frozenset({model.id}))


def test_resolution_fails_closed_for_models_with_user_entitlements() -> None:
    snapshot, model = _snapshot(entitlement=frozenset({"restricted"}))
    with pytest.raises(FailoverModelUnavailable):
        resolve_resident_model(snapshot, model.id, resident_model_ids=frozenset({model.id}))
