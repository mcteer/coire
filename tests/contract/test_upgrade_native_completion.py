"""Native serving smoke parses the pinned engine's standard completion metadata."""

import pytest
from pydantic import ValidationError

from coire_core.models.gateway import TextCompletionResponse


def completion() -> dict[str, object]:
    return {
        "id": "cmpl-native-fixture",
        "object": "text_completion",
        "created": 1791689700,
        "model": "/isolated/models/owner--tiny",
        "choices": [{"index": 0, "text": "three, four, five", "finish_reason": "length"}],
        "usage": {
            "prompt_tokens": 14,
            "completion_tokens": 64,
            "total_tokens": 78,
            "prompt_tokens_details": {"cached_tokens": 0},
        },
        "system_fingerprint": "mlx-lm-0.31.3",
    }


def test_native_standard_metadata_is_typed_without_ignoring_unknown_fields() -> None:
    parsed = TextCompletionResponse.model_validate(completion())
    assert parsed.usage is not None and parsed.usage.prompt_tokens_details is not None
    assert parsed.usage.prompt_tokens_details.cached_tokens == 0
    assert parsed.usage.completion_tokens == 64
    assert parsed.system_fingerprint == "mlx-lm-0.31.3"


@pytest.mark.parametrize("fault", ["unknown", "nested_unknown", "negative_cache", "fingerprint"])
def test_completion_metadata_does_not_weaken_the_wire_contract(fault: str) -> None:
    payload = completion()
    if fault == "unknown":
        payload["engine_debug"] = "private"
    elif fault == "fingerprint":
        payload["system_fingerprint"] = "x" * 129
    else:
        usage = payload["usage"]
        assert isinstance(usage, dict)
        usage["prompt_tokens_details"] = (
            {"cached_tokens": 0, "engine_debug": "private"}
            if fault == "nested_unknown"
            else {"cached_tokens": -1}
        )
    with pytest.raises(ValidationError):
        TextCompletionResponse.model_validate(payload)
