"""JSON lifecycle logs with a closed set of content-free correlation fields."""

import json
import logging
import math
import uuid
from datetime import UTC, datetime
from enum import Enum


class StructuredJSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        values: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for name in (
            "run_id",
            "job_id",
            "instance_id",
            "measurement_id",
            "phase",
            "model_id",
            "user_id",
            "attempt_id",
            "command_id",
            "command_type",
            "node",
            "operation",
            "error_type",
            "reason",
            "safe_reason",
            "outcome",
            "pair_id",
            "export_id",
            "capture_generation",
        ):
            value = getattr(record, name, None)
            if isinstance(value, (uuid.UUID, Enum)):
                value = str(value)
            if isinstance(value, (str, int, bool)) or (
                isinstance(value, float) and math.isfinite(value)
            ):
                values[name] = value
        if record.exc_info and record.exc_info[0]:
            values["error_type"] = record.exc_info[0].__name__
        return json.dumps(values, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
