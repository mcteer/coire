"""Initialize the existing SDK in credential-free native worker processes."""

import os

from coire_node import __version__
from coire_node.otel import configure_node_telemetry


def initialize_training_telemetry() -> None:
    endpoint = os.environ.get("OTLP_ENDPOINT")
    if endpoint:
        configure_node_telemetry(__version__, endpoint)
