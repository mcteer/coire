import uuid

import pytest
from pydantic import ValidationError

from coire_core.models.gateway import ChatCompletionRequest


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
