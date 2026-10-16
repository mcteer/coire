"""Content-free instrumentation around universal image text policy."""

import logging

from opentelemetry import metrics, trace

from coire_core.errors import ImageContentBlocked
from coire_core.image_safety import POLICY_VERSION
from coire_core.image_safety import ensure_image_content_allowed as check

logger = logging.getLogger(__name__)
tracer = trace.get_tracer("coire.api.image.safety")
refusals = metrics.get_meter("coire.api.image").create_counter(
    "coire_image_content_policy_refusals_total",
    unit="1",
    description="Image text-policy refusals; no prompt content or identity labels",
)


def ensure_image_content_allowed(prompt: str | None, *, explicit: bool = False) -> None:
    with tracer.start_as_current_span("coire.api.images.content_policy") as span:
        span.set_attribute("policy_version", POLICY_VERSION)
        try:
            check(prompt, explicit=explicit)
        except ImageContentBlocked:
            span.set_attribute("outcome", "refused")
            refusals.add(1, {"policy_version": POLICY_VERSION})
            logger.warning("image content policy refused", extra={"policy_version": POLICY_VERSION})
            raise
