"""Shared wire models."""

from coire_core.models.gateway import (
    AnthropicMessage,
    AnthropicMessagesRequest,
    ChatCompletionRequest,
    ChatMessage,
    GatewayModel,
    GatewayModelList,
    GatewayProtocol,
    ProblemDetails,
    UsageOutcome,
    UsageRecord,
)
from coire_core.models.link import (
    ControlPathStatus,
    LinkState,
    RdmaState,
    StudioDataLinkStatus,
)

__all__ = [
    "AnthropicMessage",
    "AnthropicMessagesRequest",
    "ChatCompletionRequest",
    "ChatMessage",
    "ControlPathStatus",
    "GatewayModel",
    "GatewayModelList",
    "GatewayProtocol",
    "LinkState",
    "ProblemDetails",
    "RdmaState",
    "StudioDataLinkStatus",
    "UsageOutcome",
    "UsageRecord",
]
