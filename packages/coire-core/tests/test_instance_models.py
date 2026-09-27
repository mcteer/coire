from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from coire_core.models.instance import InstanceCreate, InstanceState, NodeDeclaration


def test_instance_create_forbids_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        InstanceCreate.model_validate(
            {
                "model_id": str(uuid.uuid4()),
                "variant_id": str(uuid.uuid4()),
                "policy": "single:auto",
                "engine_argv": ["caller-controlled"],
            }
        )


def test_instance_state_has_exact_lifecycle() -> None:
    assert [item.value for item in InstanceState] == [
        "requested",
        "reserving",
        "launching",
        "warming",
        "ready",
        "draining",
        "stopped",
        "failed",
    ]


def test_declared_node_hosts_are_dns_identities() -> None:
    declaration = NodeDeclaration(
        name="coire-edge-a",
        control_host="coire-edge-a.lab",
        data_host="coire-edge-a.fabric",
        memory_total_bytes=1,
        disk_total_bytes=1,
    )
    assert declaration.control_host.endswith(".lab")
