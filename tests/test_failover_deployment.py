"""Failover state and credentials cannot inherit ephemeral or cross-scope wiring."""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_core_failover_guards_are_durable_and_nonroot_owned() -> None:
    compose = yaml.safe_load((ROOT / "deploy/compose/compose.yaml").read_text())
    api = compose["services"]["coire-api"]
    assert all("/opt/coire/failover" not in mount for mount in api["tmpfs"])
    assert "coire-failover-state:/opt/coire/failover" in api["volumes"]
    assert "coire-failover-state" in compose["volumes"]
    image = (ROOT / "apps/coire-api/docker/api.Dockerfile").read_text()
    assert "--chown=65532:65532 --chmod=0700 /volume/failover-state /opt/coire/failover" in image


def test_monitor_and_local_lifecycle_credentials_have_separate_mounts() -> None:
    compose = yaml.safe_load((ROOT / "deploy/compose/compose.yaml").read_text())
    core = compose["services"]["coire-api"]
    frontend = compose["services"]["coire-failover"]
    assert "failover_monitor_token" in core["secrets"]
    assert "failover_lifecycle_token" not in core["secrets"]
    assert set(frontend["secrets"]) == {
        "failover_relay_token",
        "failover_monitor_token",
        "failover_lifecycle_token",
    }
    assert "FAILOVER_MEMBER_NAME" in frontend["environment"]
    assert "FAILOVER_MEMBER_NAME" in core["environment"]
    assert frontend["environment"]["OTLP_ENDPOINT"] == "http://coire-core.lab:4317"
    assert frontend["environment"]["DIAGNOSTICS_ENABLED"] == "${COIRE_DIAGNOSTICS_ENABLED:-false}"
    for secret in ("failover_monitor_token", "failover_lifecycle_token"):
        assert compose["secrets"][secret]["file"] == "${COIRE_SECRETS_DIR}/" + secret


def test_studio_precreation_checks_host_and_materializes_all_scoped_credentials() -> None:
    script = (ROOT / "deploy/compose/coire-studio-failover-create").read_text()
    assert "hostname" in script
    assert "COIRE_FAILOVER_MEMBER_NAME" in script
    for secret in ("relay", "monitor", "lifecycle"):
        assert "coire-failover-" + secret + "-token" in script
    assert "create --no-deps coire-failover" in script
    assert "start coire-failover" not in script
