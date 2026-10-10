"""Native lifecycle logs preserve identity without exporting contribution bodies."""

import json
import logging
import uuid

from coire_core.logging import StructuredJSONFormatter


def test_control_log_is_valid_json_with_identity_and_no_transport_content() -> None:
    record = logging.LogRecord(
        "coire_node.training",
        logging.INFO,
        __file__,
        1,
        'control "%s"\naccepted',
        ("prepare",),
        None,
    )
    identity = uuid.uuid4()
    for key, value in {
        "job_id": "01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "instance_id": identity,
        "operation": "prepare",
        "grant_secret": "PRIVATE-GRANT",
        "prompt": "PRIVATE-PROMPT",
        "candidate": "PRIVATE-CANDIDATE",
    }.items():
        setattr(record, key, value)
    encoded = StructuredJSONFormatter().format(record)
    parsed = json.loads(encoded)
    assert parsed["job_id"] == "01ARZ3NDEKTSV4RRFFQ69G5FAV"
    assert parsed["instance_id"] == str(identity)
    assert parsed["msg"] == 'control "prepare"\naccepted'
    assert parsed["operation"] == "prepare"
    assert "PRIVATE" not in encoded
