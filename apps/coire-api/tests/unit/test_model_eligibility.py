"""The same current entitlements govern registry listings and resolution."""

import uuid

from coire_api.auth import Principal, PrincipalKind
from coire_api.db import ModelRow
from coire_api.gateway.resolution import _visible
from coire_api.registry.service import chat_model_eligible, to_listing, visible_to
from coire_core.models.registry import EngineBackend, ModelState, Visibility


def model(**changes: object) -> ModelRow:
    values: dict[str, object] = {
        "id": uuid.uuid4(),
        "repo_id": "owner/model",
        "slug": "owner--model",
        "display_name": "Model",
        "state": ModelState.READY,
        "visibility": Visibility.PUBLISHED,
        "entitlement": ["team-a"],
        "tags": [],
        "placement_policy": "single:auto",
        "precision": "4bit",
        "weight_bytes": 1,
        "total_bytes": 1,
        "file_count": 1,
        "memory_estimate_bytes": 2,
        "capability_profile": {},
    }
    values.update(changes)
    return ModelRow(**values)


def test_entitlements_are_not_api_scopes() -> None:
    row = model()
    entitled = Principal(
        kind=PrincipalKind.USER, user_id=uuid.uuid4(), entitlements=frozenset({"team-a"})
    )
    scope_only = Principal(
        kind=PrincipalKind.API_KEY, user_id=uuid.uuid4(), scopes=frozenset({"team-a", "chat"})
    )
    assert visible_to(is_admin=False, model=row, entitlements=entitled.entitlements)
    assert _visible(row, entitled)
    assert chat_model_eligible(row, entitled)
    assert not visible_to(is_admin=False, model=row, entitlements=scope_only.entitlements)
    assert not _visible(row, scope_only)
    assert not chat_model_eligible(row, scope_only)


def test_chat_requires_published_ready_even_for_admin() -> None:
    admin = Principal(kind=PrincipalKind.ADMIN, user_id=uuid.uuid4())
    hidden = model(visibility=Visibility.ADMIN_ONLY, entitlement=[])
    assert visible_to(is_admin=True, model=hidden)
    assert _visible(hidden, admin)
    assert not chat_model_eligible(hidden, admin)
    assert not chat_model_eligible(model(state=ModelState.DOWNLOADING, entitlement=[]), admin)
    assert not chat_model_eligible(model(), admin)


def test_listing_reports_registry_backend() -> None:
    row = model(backend=EngineBackend.MLX_VLM, entitlement=[])
    assert to_listing(row, [], node_names={}).backend is EngineBackend.MLX_VLM
