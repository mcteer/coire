"""Run the failover ASGI application."""

import logging

import uvicorn

from coire_core.logging import StructuredJSONFormatter
from coire_core.settings import get_settings
from coire_failover.telemetry import configure

handler = logging.StreamHandler()
handler.setFormatter(StructuredJSONFormatter())
logging.basicConfig(level=logging.INFO, handlers=[handler])
metering, tracing = configure(get_settings())
try:
    uvicorn.run("coire_failover.app:app", host="0.0.0.0", port=8004)
finally:
    metering.shutdown(timeout_millis=1000)
    tracing.shutdown()
