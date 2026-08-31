"""Unit tests for the shared wire shapes (T011)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from pydantic import SecretStr, ValidationError

from coire_core.models.health import (
    HealthResponse,
    HealthStatus,
    ReadyResponse,
    ServiceHealth,
)
from coire_core.models.node import (
    NetworkPath,
    Node,
    NodeEndpointSet,
    NodePath,
    NodeRegistration,
    NodeRegistrationV2,
    NodeRole,
    NodeStatus,
    NodeStatusV2,
    Reachability,
    ThermalState,
)

NOW = datetime.now(UTC)


def _registration(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "name": "coire-edge-a",
        "token": "s3cret",
        "mesh_address": "192.168.100.11",
        "egress_address": "192.168.4.11",
        "memory_total_bytes": 274877906944,
        "disk_total_bytes": 1979120929996,
        "gpu_cores": 80,
        "agent_version": "0.1.0",
    }
    base.update(overrides)
    return base


class TestExtraFieldsRejected:
    """`extra="forbid"` everywhere: a typo in a wire payload must fail loudly, not be dropped."""

    @pytest.mark.parametrize(
        ("model", "payload"),
        [
            (ReadyResponse, {"service": "coire-api", "version": "0.1.0", "ready": True}),
            (
                ServiceHealth,
                {"name": "postgres", "healthy": True, "checked_at": NOW},
            ),
            (NodeRegistration, _registration()),
        ],
    )
    def test_unknown_field_rejected(self, model: type, payload: dict[str, object]) -> None:
        model(**payload)  # sanity: the payload itself is valid
        with pytest.raises(ValidationError):
            model(**{**payload, "unexpected": "value"})


class TestNodeRegistration:
    def test_accepts_mesh_address(self) -> None:
        reg = NodeRegistration(**_registration())  # type: ignore[arg-type]
        assert str(reg.mesh_address) == "192.168.100.11"
        assert reg.token.get_secret_value() == "s3cret"

    @pytest.mark.parametrize(
        "address",
        ["192.168.4.11", "10.10.0.11", "127.0.0.1", "192.168.101.11"],
    )
    def test_rejects_off_mesh_address(self, address: str) -> None:
        with pytest.raises(ValidationError, match="mesh subnet"):
            NodeRegistration(**_registration(mesh_address=address))  # type: ignore[arg-type]

    def test_egress_address_is_not_constrained_to_mesh(self) -> None:
        reg = NodeRegistration(**_registration(egress_address="192.168.4.12"))  # type: ignore[arg-type]
        assert str(reg.egress_address) == "192.168.4.12"

    @pytest.mark.parametrize("name", ["edge-a", "Coire-Edge-A", "coire_edge_a", ""])
    def test_rejects_malformed_name(self, name: str) -> None:
        with pytest.raises(ValidationError):
            NodeRegistration(**_registration(name=name))  # type: ignore[arg-type]

    @pytest.mark.parametrize("field", ["memory_total_bytes", "disk_total_bytes"])
    def test_rejects_non_positive_sizes(self, field: str) -> None:
        with pytest.raises(ValidationError):
            NodeRegistration(**_registration(**{field: 0}))  # type: ignore[arg-type]

    def test_secret_is_not_in_repr(self) -> None:
        reg = NodeRegistration(**_registration())  # type: ignore[arg-type]
        assert "s3cret" not in repr(reg)


class TestNodeRegistrationV2:
    def test_accepts_declared_studio_endpoints(self) -> None:
        registration = NodeRegistrationV2(
            name="coire-edge-a",
            token=SecretStr("secret"),
            endpoints=NodeEndpointSet(
                control_host="coire-edge-a.lab", data_host="coire-edge-a.fabric"
            ),
            memory_total_bytes=1,
            disk_total_bytes=1,
            gpu_cores=80,
            agent_version="0.2.0",
        )
        assert registration.endpoints.contract_version == 2

    def test_accepts_bare_control_name_during_rolling_upgrade(self) -> None:
        registration = NodeRegistrationV2(
            name="coire-edge-a",
            token=SecretStr("secret"),
            endpoints=NodeEndpointSet(control_host="coire-edge-a", data_host="coire-edge-a.fabric"),
            memory_total_bytes=1,
            disk_total_bytes=1,
            agent_version="0.2.0",
        )
        assert registration.endpoints.control_host == "coire-edge-a"

    def test_rejects_mismatched_control_identity(self) -> None:
        with pytest.raises(ValidationError, match="control_host"):
            NodeRegistrationV2.model_validate(
                {
                    "name": "coire-edge-a",
                    "token": "secret",
                    "endpoints": {
                        "contract_version": 2,
                        "control_host": "coire-edge-b.lab",
                        "data_host": "coire-edge-a.fabric",
                    },
                    "memory_total_bytes": 1,
                    "disk_total_bytes": 1,
                    "agent_version": "0.2.0",
                }
            )

    def test_rejects_studio_without_data_endpoint(self) -> None:
        with pytest.raises(ValidationError, match="data_host"):
            NodeRegistrationV2.model_validate(
                {
                    "name": "coire-edge-b",
                    "token": "secret",
                    "endpoints": {
                        "contract_version": 2,
                        "control_host": "coire-edge-b",
                        "data_host": None,
                    },
                    "memory_total_bytes": 1,
                    "disk_total_bytes": 1,
                    "agent_version": "0.2.0",
                }
            )


class TestNodeStatus:
    def _status(self, **overrides: object) -> NodeStatus:
        base: dict[str, object] = {
            "name": "coire-edge-a",
            "agent_version": "0.1.0",
            "uptime_seconds": 12.5,
            "cpu_percent": 3.2,
            "gpu_percent": 41.0,
            "thermal_state": ThermalState.NOMINAL,
            "memory_total_bytes": 274877906944,
            "memory_free_bytes": 200000000000,
            "disk_total_bytes": 1979120929996,
            "disk_free_bytes": 1800000000000,
            "agent_cpu_percent": 0.4,
            "agent_rss_bytes": 48 * 1024 * 1024,
            "collection_budget_ok": True,
            "path": NodePath.MESH,
            "sampled_at": NOW,
        }
        base.update(overrides)
        return NodeStatus(**base)  # type: ignore[arg-type]

    def test_gpu_percent_may_be_absent(self) -> None:
        assert self._status(gpu_percent=None).gpu_percent is None

    @pytest.mark.parametrize(("field", "value"), [("cpu_percent", 101.0), ("gpu_percent", -1.0)])
    def test_percentages_are_bounded(self, field: str, value: float) -> None:
        with pytest.raises(ValidationError):
            self._status(**{field: value})

    def test_path_records_which_listener_answered(self) -> None:
        assert self._status(path=NodePath.FALLBACK).path is NodePath.FALLBACK

    def test_v2_path_is_control_only(self) -> None:
        payload = self._status().model_dump(exclude={"path"})
        status = NodeStatusV2(**payload)
        assert status.path is NetworkPath.CONTROL
        with pytest.raises(ValidationError):
            NodeStatusV2.model_validate({**payload, "path": "data"})


class TestEnums:
    def test_enum_values_match_the_contract(self) -> None:
        assert [r.value for r in Reachability] == [
            "healthy",
            "degraded",
            "unreachable",
            "unknown",
        ]
        assert [t.value for t in ThermalState] == [
            "nominal",
            "fair",
            "serious",
            "critical",
            "unknown",
        ]
        assert [p.value for p in NodePath] == ["mesh", "fallback"]
        assert [r.value for r in NodeRole] == ["studio", "core"]
        assert [s.value for s in HealthStatus] == ["healthy", "degraded", "unhealthy"]


class TestHealthResponse:
    def test_defaults_to_empty_collections(self) -> None:
        resp = HealthResponse(status=HealthStatus.HEALTHY, version="0.1.0", generated_at=NOW)
        assert resp.services == []
        assert resp.nodes == []

    def test_ready_is_always_true(self) -> None:
        with pytest.raises(ValidationError):
            ReadyResponse(service="coire-api", version="0.1.0", ready=False)  # type: ignore[arg-type]


class TestNode:
    def test_defaults_to_unknown_reachability(self) -> None:
        node = Node(
            id=uuid.uuid4(),
            name="coire-edge-a",
            role=NodeRole.STUDIO,
            mesh_address="192.168.100.11",  # type: ignore[arg-type]
            egress_address="192.168.4.11",  # type: ignore[arg-type]
            memory_total_bytes=1,
            disk_total_bytes=1,
            agent_version="0.1.0",
            registered_at=NOW,
            last_seen_at=NOW,
        )
        assert node.reachability is Reachability.UNKNOWN
