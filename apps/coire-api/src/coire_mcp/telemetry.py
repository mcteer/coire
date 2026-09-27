"""MCP call telemetry shared by the three public coding tools."""

from opentelemetry import metrics, trace

tracer = trace.get_tracer("coire.mcp")
meter = metrics.get_meter("coire.mcp")
calls_total = meter.create_counter("coire_mcp_calls_total", unit="1")
call_duration = meter.create_histogram("coire_mcp_call_duration_seconds", unit="s")
