import uuid

import pytest
from pydantic import ValidationError

from coire_api.auth import ADMIN, Principal, PrincipalKind
from coire_api.db import ModelRow
from coire_api.gateway.resolution import _visible
from coire_core.models.gateway import ChatCompletionRequest
from coire_core.models.registry import ModelState, Visibility


def model(**overrides: object) -> ModelRow:
    values: dict[str, object] = {
        "id": uuid.uuid4(),
        "repo_id": "owner/repo",
        "slug": "owner--repo",
        "display_name": "Repo",
        "state": ModelState.READY,
        "visibility": Visibility.PUBLISHED,
        "entitlement": [],
        "tags": [],
        "placement_policy": "single:auto",
        "precision": "4bit",
        "weight_bytes": 1,
        "total_bytes": 1,
        "file_count": 1,
        "memory_estimate_bytes": 2,
        "capability_profile": {},
    }
    values.update(overrides)
    return ModelRow(**values)


def test_model_identifier_must_be_registry_uuid() -> None:
    with pytest.raises(ValidationError):
        ChatCompletionRequest.model_validate(
            {"model": "../../caller/path", "messages": [{"role": "user", "content": "hi"}]}
        )


def test_model_identifier_remains_uuid() -> None:
    model_id = uuid.uuid4()
    request = ChatCompletionRequest.model_validate(
        {"model": str(model_id), "messages": [{"role": "user", "content": "hi"}]}
    )
    assert request.model == model_id


def test_unpublished_and_unentitled_are_indistinguishable_to_user() -> None:
    user = Principal(kind=PrincipalKind.USER, subject="user", scopes=frozenset({"general"}))
    assert not _visible(model(visibility=Visibility.ADMIN_ONLY), user)
    assert not _visible(model(entitlement=["explicit"]), user)
    assert _visible(model(visibility=Visibility.ADMIN_ONLY), ADMIN)
