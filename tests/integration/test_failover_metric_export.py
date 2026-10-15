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
from coire_core.settings import Settings
from coire_failover.telemetry import configure
from coire_core import failover_telemetry as telemetry
body = json.load(sys.stdin)
metering, tracing = configure(Settings(diagnostics_enabled=body['diagnostics'], otlp_endpoint=body['endpoint']))
telemetry.readiness.set(1, {'host': 'coire-edge-a'})
telemetry.snapshot_fresh.set(1, {'host': 'coire-edge-a'})
telemetry.participant_last_seen.set(123, {'host': 'coire-edge-a'})
telemetry.audit_available.set(1, {'host': 'coire-edge-a'})
telemetry.audit_utilization.set(.1, {'host': 'coire-edge-a'})
telemetry.drain_remaining.set(10, {'host': 'coire-edge-a'})
telemetry.ingress_refusals_total.add(1, {'reason': 'not_elected'})
telemetry.elections_total.add(1, {'host': 'coire-edge-a', 'kind': 'promoted'})
telemetry.handbacks_total.add(1, {'host': 'coire-edge-a'})
with telemetry.tracer.start_as_current_span('coire.failover.qualification'):
    pass
assert metering.force_flush(timeout_millis=10000)
metering.shutdown(timeout_millis=1000)
tracing.shutdown()
"""


@pytest.mark.parametrize("diagnostics", [False, True], ids=["diagnostics_off", "diagnostics_on"])
def test_real_collector_exports_exact_failover_rule_and_counter_names(diagnostics: bool) -> None:
    # Synthetic instrument values exercise export translation only. They do
    # not represent an native failover, resource measurements or public ingress.
    name = "coire-020-metric-export-" + uuid4().hex[:12]
    image = os.environ.get("COIRE_FAILOVER_OTEL_TEST_IMAGE", "coire-otel:ci")
    created = subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--name",
            name,
            "--label",
            "com.coire.test=020-metric-export",
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
        assert metadata["Config"]["Labels"]["com.coire.test"] == "020-metric-export"
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
            "coire_failover_audit_utilization",
            "coire_failover_drain_remaining_seconds",
            "coire_failover_frontend_rss_bytes",
            "coire_failover_participant_last_seen_seconds",
            "coire_failover_snapshot_fresh",
            "coire_failover_elections_total",
            "coire_failover_readiness",
            "coire_failover_handbacks_total",
            "coire_failover_audit_available",
            "coire_failover_ingress_refusals_total",
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
        assert expected <= names, "Exported failover series differ from alert/dashboard contracts"
    finally:
        subprocess.run(
            ["docker", "rm", "-f", container], capture_output=True, check=True, timeout=15
        )


@pytest.mark.parametrize("diagnostics", [False, True])
def test_unreachable_collector_has_bounded_flush_and_does_not_block_admission(
    diagnostics: bool,
) -> None:
    # A closed loopback port models total collector loss without changing any host network.
    producer = PRODUCER.replace(
        "assert metering.force_flush(timeout_millis=10000)",
        """
import time
from fastapi.testclient import TestClient
from coire_failover.app import FailoverRuntime, create_app
runtime = FailoverRuntime(settings=Settings(), load_snapshot=lambda: None, load_lease=lambda: None)
started = time.monotonic()
with TestClient(create_app(runtime)) as client:
    for _ in range(20):
        assert client.get('/v1/models').status_code == 401
assert time.monotonic() - started < 3
started = time.monotonic()
metering.force_flush(timeout_millis=2000)
assert time.monotonic() - started < 3
""",
    )
    result = subprocess.run(
        [sys.executable, "-c", producer],
        input=json.dumps({"endpoint": "http://127.0.0.1:1", "diagnostics": diagnostics}),
        capture_output=True,
        text=True,
        env={**os.environ, "OTEL_SDK_DISABLED": "false"},
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
