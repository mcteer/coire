"""Conservative text estimates cover normalization and malformed Unicode without tokenizers."""

from uuid import uuid4

import pytest

from coire_core.failover_limits import FailoverLimitError, bounded_text_request
from coire_core.models.failover import FailoverLimits, FailoverModel
from coire_core.models.gateway import ChatCompletionRequest, ChatMessage


@pytest.mark.parametrize(
    "content,reason",
    [("\ufdfa" * 100, "request_limit_exceeded"), ("\ud800", "unsupported_capability")],
)
def test_normalization_expansion_and_surrogates_fail_closed(content: str, reason: str) -> None:
    model = FailoverModel(id=uuid4(), slug="fixture", display_name="Fixture", context_window=2048)
    request = ChatCompletionRequest(
        model=model.id, messages=[ChatMessage(role="user", content=content)]
    )
    with pytest.raises(FailoverLimitError, match=reason):
        bounded_text_request(request, model, FailoverLimits())


def test_signed_model_policy_can_only_tighten_subject_limits() -> None:
    model = FailoverModel(
        id=uuid4(),
        slug="fixture",
        display_name="Fixture",
        context_window=2048,
        limits=FailoverLimits(prompt_tokens=512, output_tokens=16),
    )
    request = ChatCompletionRequest(
        model=model.id, messages=[ChatMessage(role="user", content="hello")]
    )
    assert bounded_text_request(request, model, FailoverLimits()).max_tokens == 16
    with pytest.raises(FailoverLimitError, match="request_limit_exceeded"):
        bounded_text_request(request.model_copy(update={"max_tokens": 17}), model, FailoverLimits())
    with pytest.raises(FailoverLimitError, match="request_limit_exceeded"):
        bounded_text_request(
            request.model_copy(update={"messages": [ChatMessage(role="user", content="a" * 257)]}),
            model,
            FailoverLimits(),
        )


def test_resident_text_request_never_retains_a_wait_for_model_instruction() -> None:
    model = FailoverModel(id=uuid4(), slug="fixture", display_name="Fixture", context_window=2048)
    request = ChatCompletionRequest(
        model=model.id, messages=[ChatMessage(role="user", content="hello")]
    )
    bounded = bounded_text_request(request, model, FailoverLimits())
    assert bounded.max_tokens == 1024
    assert bounded.coire_wait_for_model is False


def test_model_rate_and_concurrency_limits_tighten_subject_counters() -> None:
    from coire_core.failover_limits import IdentityCounters, effective_limits

    model = FailoverModel(
        id=uuid4(),
        slug="fixture",
        display_name="Fixture",
        context_window=2048,
        limits=FailoverLimits(requests_per_minute=2, concurrency=1),
    )
    policy = effective_limits(FailoverLimits(requests_per_minute=10, concurrency=1), model)
    counters = IdentityCounters()
    counters.acquire("user", policy)
    with pytest.raises(FailoverLimitError):
        counters.acquire("user", policy)
    counters.release("user")
    counters.acquire("user", policy)
    counters.release("user")
    with pytest.raises(FailoverLimitError):
        counters.acquire("user", policy)
