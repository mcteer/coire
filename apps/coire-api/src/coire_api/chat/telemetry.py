"""Content-free native Chat request telemetry."""

from __future__ import annotations

from opentelemetry import metrics, trace

tracer = trace.get_tracer("coire.api.chat")
requests_total = metrics.get_meter("coire.api.chat").create_counter(
    "coire_chat_requests_total",
    unit="1",
    description="Native Chat API requests by operation and outcome",
)
turns_total = metrics.get_meter("coire.api.chat").create_counter(
    "coire_chat_turns_total",
    unit="1",
    description="Persisted native Chat terminal outcomes by fixed mode",
)
active_turns = metrics.get_meter("coire.api.chat").create_gauge(
    "coire_chat_active_turns",
    unit="1",
    description="Persisted active native Chat turns observed by API maintenance",
)
stop_seconds = metrics.get_meter("coire.api.chat").create_histogram(
    "coire_chat_stop_seconds",
    unit="s",
    description="Time from a durable native Stop request to terminal persistence",
)
upload_rejections_total = metrics.get_meter("coire.api.chat").create_counter(
    "coire_chat_upload_rejections_total",
    unit="1",
    description="Refused native Chat uploads by safe domain error code",
)
parser_failures_total = metrics.get_meter("coire.api.chat").create_counter(
    "coire_chat_parser_failures_total",
    unit="1",
    description="Native Chat engine stream framing failures by reason",
)
purge_oldest_seconds = metrics.get_meter("coire.api.chat").create_gauge(
    "coire_chat_purge_oldest_seconds",
    unit="s",
    description="Age of the oldest deleted conversation awaiting verified content purge",
)
