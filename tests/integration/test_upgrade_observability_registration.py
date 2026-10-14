"""Real composed API metrics, Prometheus rules and diagnostic dashboard registration."""

import json
import os
import secrets
import socket
import subprocess
import time
from pathlib import Path
from uuid import uuid4

import pytest

pytestmark = pytest.mark.integration
REPO = Path(__file__).resolve().parents[2]

READ = """
import json,sys
from pathlib import Path
import httpx
body=json.load(sys.stdin)
with httpx.Client(trust_env=False,timeout=5) as client:
 rules=client.get('http://prometheus:9090/api/v1/rules');rules.raise_for_status()
 metrics=client.get('http://otel-collector:8889/metrics');metrics.raise_for_status()
 result={'rules':rules.json(),'metrics':metrics.text}
 if body['diagnostics']:
  secret=Path('/run/secrets/admin_token').read_text().strip()
  dashboard=client.get('http://grafana:3000/api/dashboards/uid/coire-upgrades',auth=('admin',secret));dashboard.raise_for_status()
  result['dashboard']=dashboard.json()['dashboard']
 print(json.dumps(result))
"""


@pytest.mark.parametrize("diagnostics", [False, True], ids=["lean", "diagnostics"])
def test_actual_upgrade_metrics_rules_and_dashboard_registration(
    tmp_path: Path,
    diagnostics: bool,
) -> None:
    project = "coire-019-observability-" + uuid4().hex[:12]
    tag = os.environ.get("COIRE_UPGRADE_COHORT_TEST_TAG", "ci")
    tmp_path.chmod(0o700)
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    overlay = tmp_path / "overlay.yaml"
    overlay.write_text(
        "services:\n  otel-collector:\n    ports: !reset []\n  coire-api:\n    environment:\n      NODE_INVENTORY_FILE: /tmp/no-studio-inventory\n"
    )
    env = {
        **os.environ,
        "COMPOSE_FILE": str(REPO / "deploy/compose/compose.yaml") + os.pathsep + str(overlay),
        "COMPOSE_PROJECT_NAME": project,
        "COMPOSE_PROFILES": "upgrades,diagnostics" if diagnostics else "upgrades",
        "COIRE_TAG": tag,
        "COIRE_REGISTRY": "",
        "COIRE_STATE_ROOT": str(tmp_path / "state"),
        "COIRE_SECRETS_BASE": str(tmp_path / "secrets"),
        "COIRE_SECRETS_DIR": str(tmp_path / "secrets"),
        "COIRE_CLUSTER_CONFIG_DIR": str(REPO / "tests/integration/testdata"),
        "COIRE_CONTROL_BIND_ADDRESS": "127.0.0.1",
        "COIRE_CONTROL_PORT": str(port),
        "COIRE_UPGRADES_ENABLED": "true",
        "COIRE_SECRET_POSTGRES_PASSWORD": secrets.token_urlsafe(32),
        "COIRE_SECRET_KEY_SIGNING_SECRET": secrets.token_urlsafe(32),
        "COIRE_SECRET_ADMIN_TOKEN": secrets.token_urlsafe(32),
        "COIRE_SECRET_BOOTSTRAP_ADMIN_EMAIL": "observability@isolated.test",
        "COIRE_SECRET_NODE_TOKENS": "{}",
        "COIRE_SECRET_OPS_SERVICE_TOKEN": "coire_ops_" + secrets.token_hex(32),
        "COIRE_SECRET_FILE_WORKER_SERVICE_TOKEN": "coire_file_worker_" + secrets.token_hex(32),
        "COIRE_SECRET_UPGRADE_EXECUTOR_TOKEN": "coire_upgrade_" + secrets.token_hex(32),
    }
    frozen = tmp_path / "state" / project / "current/compose.json"
    try:
        installed = subprocess.run(
            [str(REPO / "deploy/compose/coire-up"), "--secrets-from-env", "--no-build"],
            env=env,
            cwd=REPO,
            capture_output=True,
            text=True,
            timeout=240,
        )
        log = tmp_path / "installation.log"
        log.write_text(installed.stdout + installed.stderr)
        log.chmod(0o600)
        assert installed.returncode == 0, "Isolated installation failed; inspect private log"
        compose = ["docker", "compose", "-p", project, "-f", str(frozen)]
        expected = {
            "coire_upgrades_enabled",
            "coire_upgrade_operations",
            "coire_upgrade_snapshot_timestamp_seconds",
            "coire_upgrade_executor_ready",
            "coire_upgrade_maintenance_age_seconds",
            "coire_upgrade_recovery_age_seconds",
        }
        deadline = time.monotonic() + 45
        while True:
            result = subprocess.run(
                [*compose, "exec", "-T", "coire-api", "/app/.venv/bin/python3", "-c", READ],
                input=json.dumps({"diagnostics": diagnostics}),
                env=env,
                capture_output=True,
                text=True,
                timeout=20,
            )
            if result.returncode == 0:
                observation = json.loads(result.stdout)
                names = {
                    line.split("{", 1)[0].split(" ", 1)[0]
                    for line in observation["metrics"].splitlines()
                    if line and not line.startswith("#")
                }
                if expected <= names:
                    break
            assert time.monotonic() < deadline, (
                "Actual composed upgrade observations never became available"
            )
            time.sleep(0.5)
        groups = observation["rules"]["data"]["groups"]
        rules = next(group for group in groups if group["name"] == "coire-upgrades")["rules"]
        assert {rule["name"] for rule in rules} == {
            "CoireUpgradeColdContinuationAccepted",
            "CoireUpgradeNativeFailureReconciled",
            "CoireUpgradeUnstartedRecoveryClosed",
            "CoireUpgradeRecoverySupportLaunchRefused",
            "CoireUpgradeSourceSupervisorMissing",
            "CoireUpgradeColdRecoveryRefused",
            "CoireUpgradeRecoveryCancelled",
            "CoireUpgradeHandoverDeadlineExceeded",
            "CoireUpgradeExecutorDeadlineExceeded",
            "CoireUpgradeBaselineUnavailable",
            "CoireUpgradeExecutorUnavailable",
            "CoireUpgradeRecoveryRequired",
            "CoireUpgradeProgressStalled",
            "CoireUpgradeCollectiveCleanupRefused",
            "CoireUpgradeRetirementReconciliationRefused",
            "CoireUpgradeSuccessorControllerRefused",
            "CoireUpgradeTerminalHostHold",
            "CoireUpgradeNativeTerminalRefused",
            "CoireUpgradeStagingQuarantineRetained",
            "CoireUpgradeUnclaimedCancellationPending",
        }
        assert all(rule["health"] == "ok" for rule in rules)
        assert not any(name.startswith("coire_upgrade") and "_ratio" in name for name in names)
        if diagnostics:
            shipped = json.loads(
                (REPO / "deploy/observability/grafana/dashboards/upgrades.json").read_bytes()
            )
            assert observation["dashboard"]["uid"] == shipped["uid"]
            assert observation["dashboard"]["panels"] == shipped["panels"]
        services = json.loads(frozen.read_bytes())["services"]
        assert ("grafana" in services) is diagnostics
        assert ("tempo" in services) is diagnostics
        assert ("loki" in services) is diagnostics
        evidence = tmp_path / "actual-observability.json"
        evidence.write_text(
            json.dumps(
                {
                    "project": project,
                    "diagnostics": diagnostics,
                    "metric_names": sorted(
                        name for name in names if name.startswith("coire_upgrade")
                    ),
                    "rule_names": sorted(rule["name"] for rule in rules),
                },
                sort_keys=True,
            )
        )
        evidence.chmod(0o600)
    finally:
        if frozen.exists():
            cleanup = subprocess.run(
                [
                    "docker",
                    "compose",
                    "-p",
                    project,
                    "-f",
                    str(frozen),
                    "down",
                    "--volumes",
                    "--remove-orphans",
                ],
                env=env,
                capture_output=True,
                timeout=120,
            )
            assert cleanup.returncode == 0, "Owned observability fixture cleanup failed"
