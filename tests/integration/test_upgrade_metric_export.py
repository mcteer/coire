"""Actual OTLP-to-Prometheus names must match the shipped alert queries."""

import json
import os
import subprocess
import sys
import time
from uuid import uuid4

import httpx
import pytest

pytestmark = pytest.mark.integration

PRODUCER = """
import json, sys
from opentelemetry import metrics
from coire_core.settings import Settings
from coire_api.telemetry import configure_telemetry
from coire_api.upgrades import telemetry, handover_telemetry, cold_continuation_telemetry
from coire_upgrade_executor import telemetry as executor, passive, worker
from coire_node import swap
body = json.load(sys.stdin)
settings = Settings(diagnostics_enabled=body['diagnostics'], otlp_endpoint=body['endpoint'])
configure_telemetry('coire-upgrade-metric-export-test', '019', settings.otlp_endpoint)
telemetry.enabled.set(1)
telemetry.active.set(1, {'kind': 'control_plane'})
telemetry.recovery_required.set(1, {'kind': 'control_plane'})
telemetry.executor_ready.set(0)
telemetry.maintenance_age.set(123, {'kind': 'control_plane'})
telemetry.recovery_age.set(45, {'kind': 'control_plane'})
telemetry.failure_observations.add(1, {'kind': 'control_plane', 'phase': 'restoring', 'outcome': 'failed'})
telemetry.operation_duration.record(123, {'kind': 'control_plane', 'phase': 'rolled_back'})
handover_telemetry.pending.set(1)
telemetry.receipt_attempts.add(1, {'kind': 'control_plane', 'phase': 'restoring', 'outcome': 'intent', 'replay': 'false'})
executor.replays.add(1)
executor.source_configuration.add(1, {'service': 'coire-api', 'outcome': 'drift'})
passive.observations.add(1)
worker.failures.add(1)
cold_continuation_telemetry.accepted.add(1, {'node': 'coire-edge-a', 'outcome': 'accepted'})
cold_continuation_telemetry.prepared.add(1, {'outcome': 'prepared'})
swap.failures.add(1, {'platform': 'darwin'})
assert metrics.get_meter_provider().force_flush(timeout_millis=10000)
metrics.get_meter_provider().shutdown(timeout_millis=10000)
"""


@pytest.mark.parametrize("diagnostics", [False, True], ids=["diagnostics_off", "diagnostics_on"])
def test_real_collector_exports_exact_upgrade_rule_and_counter_names(diagnostics: bool) -> None:
    # Synthetic instrument values exercise export translation only. They do
    # not represent an accepted upgrade, durable restoration or native smoke.
    name = "coire-019-metric-export-" + uuid4().hex[:12]
    image = os.environ.get("COIRE_UPGRADE_OTEL_TEST_IMAGE", "coire-otel:ci")
    created = subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--name",
            name,
            "--label",
            "com.coire.test=019-metric-export",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--user",
            "10001:10001",
            "--memory",
            "384m",
            "--cpus",
            "1",
            "--pids-limit",
            "64",
            "--tmpfs",
            "/tmp:rw,nosuid,nodev,size=32m",
            "--publish",
            "127.0.0.1::4317",
            "--publish",
            "127.0.0.1::8889",
            image,
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert created.returncode == 0, "Isolated policy-checked collector image is required"
    container = created.stdout.strip()
    try:
        inspected = subprocess.run(
            ["docker", "inspect", container], capture_output=True, text=True, check=True, timeout=10
        )
        metadata = json.loads(inspected.stdout)[0]
        assert metadata["Name"] == "/" + name
        assert metadata["Config"]["Labels"]["com.coire.test"] == "019-metric-export"
        ports = metadata["NetworkSettings"]["Ports"]
        assert all(ports[key][0]["HostIp"] == "127.0.0.1" for key in ("4317/tcp", "8889/tcp"))
        endpoint = "http://127.0.0.1:" + ports["4317/tcp"][0]["HostPort"]
        scrape = "http://127.0.0.1:" + ports["8889/tcp"][0]["HostPort"] + "/metrics"
        deadline = time.monotonic() + 30
        with httpx.Client(trust_env=False, timeout=2) as client:
            while True:
                try:
                    response = client.get(scrape)
                    if response.status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                assert time.monotonic() < deadline, "Private collector did not become ready"
                time.sleep(0.1)
            produced = subprocess.run(
                [sys.executable, "-c", PRODUCER],
                input=json.dumps({"endpoint": endpoint, "diagnostics": diagnostics}),
                capture_output=True,
                text=True,
                env={**os.environ, "OTEL_SDK_DISABLED": "false"},
                timeout=30,
            )
            assert produced.returncode == 0, "Actual metric export failed"
        expected = {
            "coire_upgrades_enabled",
            "coire_upgrade_operations",
            "coire_upgrade_recovery_required",
            "coire_upgrade_executor_ready",
            "coire_upgrade_maintenance_age_seconds",
            "coire_upgrade_recovery_age_seconds",
            "coire_upgrade_failure_observations_total",
            "coire_upgrade_operation_duration_seconds_count",
            "coire_upgrade_handover_pending",
            "coire_upgrade_receipt_ingest_attempts_total",
            "coire_upgrade_executor_receipt_replay_total",
            "coire_upgrade_source_configuration_observations_total",
            "coire_upgrade_executor_passive_observations_total",
            "coire_upgrade_executor_step_failures_total",
            "coire_upgrade_cold_continuations_total",
            "coire_upgrade_cold_continuation_schema_preparations_total",
            "coire_training_swap_sample_failures_total",
        }
        # The production collector batches OTLP for five seconds. A successful
        # SDK flush acknowledges receipt, not completion of that batch.
        deadline = time.monotonic() + 15
        with httpx.Client(trust_env=False, timeout=2) as client:
            while True:
                document = client.get(scrape)
                document.raise_for_status()
                names = {
                    line.split("{", 1)[0].split(" ", 1)[0]
                    for line in document.text.splitlines()
                    if line and not line.startswith("#")
                }
                if expected <= names or time.monotonic() >= deadline:
                    break
                time.sleep(0.1)
        assert expected <= names, "Exported upgrade series differ from alert/dashboard contracts"
        assert not any(name.startswith("coire_upgrade") and "_ratio" in name for name in names)
    finally:
        subprocess.run(
            ["docker", "rm", "-f", container], capture_output=True, check=True, timeout=15
        )
