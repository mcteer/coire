"""Disposable composed control-plane acceptance; never connect to real Studios."""

import json
import os
import secrets
import shutil
import socket
import subprocess
import time
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from upgrade_composed_fixtures import (
    candidate_image,
    prepare_executor_candidate,
    qualify_source,
    record_measurement,
)

pytestmark = pytest.mark.integration
REPO = Path(__file__).resolve().parents[2]


def test_upgrade_inventory_requires_quorum_and_drained_core_requests() -> None:
    """Actual signed election members supply upgrade admission's handback inventory."""
    from datetime import UTC, datetime, timedelta

    from pydantic import SecretStr
    from test_control_plane_failover import _members, _settle

    from coire_api.failover.participant import CoreParticipant
    from coire_api.routes import failover
    from coire_api.upgrades.inventory import failover_inventory
    from coire_core.failover_election import ElectionTimings
    from coire_core.settings import Settings

    private, members, membership = _members()
    core = CoreParticipant(
        name="coire-core",
        membership=membership,
        private_key_b64=private["coire-core"],
        timings=ElectionTimings(
            promotion=timedelta(seconds=10),
            demotion=timedelta(seconds=30),
            lease=timedelta(minutes=5),
            drain=timedelta(seconds=30),
        ),
    )
    members["coire-core"] = core
    settings = Settings(
        failover_member_name="coire-core", failover_peer_key=SecretStr(private["coire-core"])
    )
    failover.set_participant(core)
    try:
        _settle(members, datetime.now(UTC))
        assert core.frontend_ready()
        inventory = failover_inventory(settings)
        assert inventory.status == "complete" and inventory.entries[0].state == "standing"
        core.in_flight = 1
        assert failover_inventory(settings).entries[0].state == "active"
        core.in_flight = 0
        assert failover_inventory(settings).entries[0].state == "standing"
    finally:
        failover.set_participant(None)


@pytest.mark.parametrize(
    "handover,crash_candidate,restart_candidate,restart_incumbent,restart_committed,restart_service",
    [
        (False, False, False, False, False, None),
        (True, False, False, False, False, None),
        (True, True, False, False, False, None),
        (True, True, True, False, False, None),
        (True, True, False, True, False, None),
        (True, False, False, False, True, None),
        (True, False, False, False, False, "coire-api"),
        (True, False, False, False, False, "coire-scheduler"),
    ],
    ids=[
        "bootstrap",
        "healthy_handover",
        "candidate_failure",
        "candidate_restart",
        "incumbent_restart_then_candidate_failure",
        "committed_candidate_restart",
        "api_restart_during_handover",
        "scheduler_restart_during_handover",
    ],
)
def test_first_install_remains_unready_across_executor_restart(
    tmp_path: Path,
    handover: bool,
    crash_candidate: bool,
    restart_candidate: bool,
    restart_incumbent: bool,
    restart_committed: bool,
    restart_service: str | None,
) -> None:
    """Real host install, migrations, cohort and executor; no accepted Source invented."""
    project = "coire-019-bootstrap-" + uuid4().hex[:12]
    tag = os.environ.get("COIRE_UPGRADE_COHORT_TEST_TAG", "ci")
    executor = os.environ.get("COIRE_UPGRADE_EXECUTOR_TEST_IMAGE", "coire-upgrade-executor:ci")
    token = "coire_upgrade_" + secrets.token_hex(32)
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        control_port = reservation.getsockname()[1]
    overlay = tmp_path / "bootstrap-overlay.yaml"
    overlay.write_text(
        "services:\n"
        "  otel-collector:\n    ports: !reset []\n"
        "  coire-upgrade-executor:\n    image: " + executor + "\n"
        "  coire-api:\n    environment:\n"
        "      IDENTITY_LEGACY_ADMIN_ENABLED: 'true'\n"
        "      NODE_INVENTORY_FILE: /tmp/no-studio-inventory\n"
    )
    env = {
        **os.environ,
        "COMPOSE_FILE": str(REPO / "deploy/compose/compose.yaml") + os.pathsep + str(overlay),
        "COMPOSE_PROJECT_NAME": project,
        "COMPOSE_PROFILES": "upgrades",
        "COIRE_TAG": tag,
        "COIRE_REGISTRY": "",
        "COIRE_STATE_ROOT": str(tmp_path / "state"),
        "COIRE_SECRETS_BASE": str(tmp_path / "secrets"),
        "COIRE_SECRETS_DIR": str(tmp_path / "secrets"),
        "COIRE_CLUSTER_CONFIG_DIR": str(REPO / "tests/integration/testdata"),
        "COIRE_CONTROL_BIND_ADDRESS": "127.0.0.1",
        "COIRE_CONTROL_PORT": str(control_port),
        "COIRE_UPGRADES_ENABLED": "true",
        "COIRE_SECRET_POSTGRES_PASSWORD": secrets.token_urlsafe(32),
        "COIRE_SECRET_KEY_SIGNING_SECRET": secrets.token_urlsafe(32),
        "COIRE_SECRET_ADMIN_TOKEN": "bootstrap-" + secrets.token_urlsafe(32),
        "COIRE_SECRET_BOOTSTRAP_ADMIN_EMAIL": "bootstrap@isolated.test",
        "COIRE_SECRET_NODE_TOKENS": "{}",
        "COIRE_SECRET_OPS_SERVICE_TOKEN": "coire_ops_" + secrets.token_hex(32),
        "COIRE_SECRET_FILE_WORKER_SERVICE_TOKEN": "coire_file_worker_" + secrets.token_hex(32),
        "COIRE_SECRET_UPGRADE_EXECUTOR_TOKEN": token,
    }
    assert not subprocess.run(
        ["docker", "ps", "-aq", "--filter", "label=com.docker.compose.project=" + project],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    ).stdout.strip()
    frozen = tmp_path / "state" / project / "current/compose.json"
    owned_candidate_tag: str | None = None
    completed = False
    try:
        installed = subprocess.run(
            [str(REPO / "deploy/compose/coire-up"), "--secrets-from-env", "--no-build"],
            cwd=REPO,
            env=env,
            capture_output=True,
            text=True,
            timeout=240,
        )
        (tmp_path / "installation-output.txt").write_text(installed.stdout + installed.stderr)
        assert installed.returncode == 0, (
            "isolated bring-up failed; see private installation-output.txt"
        )
        document = json.loads(frozen.read_text())
        assert document["name"] == project
        compose = ["docker", "compose", "-p", project, "-f", str(frozen)]
        port = subprocess.run(
            [*compose, "port", "coire-web", "8080"],
            env=env,
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        ).stdout.strip()
        assert port.startswith("127.0.0.1:")
        with httpx.Client(base_url="http://" + port, trust_env=False, timeout=5) as client:
            headers = {"Authorization": "Bearer " + env["COIRE_SECRET_ADMIN_TOKEN"]}
            assert client.get("/api/v1/admin/upgrades/targets", headers=headers).status_code == 403
            user = client.post(
                "/api/v1/admin/users",
                headers=headers,
                json={
                    "email": "upgrade-admin@isolated.test",
                    "display_name": "Upgrade fixture",
                    "role": "admin",
                },
            )
            assert user.status_code == 201
            issued = client.post(
                f"/api/v1/admin/users/{user.json()['id']}/keys",
                headers=headers,
                json={
                    "name": "isolated-upgrade",
                    "scopes": ["admin"],
                    "requests_per_minute": 1000,
                    "monthly_budget_tokens": 10000,
                },
            )
            assert issued.status_code == 201
            headers = {"Authorization": "Bearer " + issued.json()["secret"]}
            for attempt in range(2):
                assert client.get("/ready").status_code == 200
                result = client.get("/api/v1/admin/upgrades/targets", headers=headers)
                assert result.status_code == 200
                core = next(
                    item for item in result.json()["items"] if item["kind"] == "control_plane"
                )
                assert core["readiness"] == "not_ready" and not core["bootstrap_ready"]
                assert "independent_recovery_bootstrap_unready" in core["blocking_reasons"]
                if attempt == 0:
                    subprocess.run(
                        [*compose, "restart", "coire-upgrade-executor"],
                        env=env,
                        capture_output=True,
                        check=True,
                        timeout=20,
                    )
            seed = qualify_source(
                REPO, tmp_path, env, frozen, client, headers, UUID(user.json()["id"])
            )
            generations = []
            for attempt in range(2):
                deadline = time.monotonic() + 30
                while True:
                    qualified = client.get("/api/v1/admin/upgrades/targets", headers=headers)
                    assert qualified.status_code == 200
                    core = next(
                        item
                        for item in qualified.json()["items"]
                        if item["kind"] == "control_plane"
                    )
                    record_measurement(
                        tmp_path,
                        project,
                        "bootstrap-last-poll",
                        {
                            "attempt": attempt,
                            "target": core,
                        },
                    )
                    if core["bootstrap_ready"]:
                        assert core["readiness"] == "ready"
                        assert core["observed_sha256"] == seed.source.manifest_sha256
                        break
                    assert time.monotonic() < deadline, (
                        "actual Source bootstrap failed its bounded qualification"
                    )
                    time.sleep(0.2)
                active = json.loads(
                    (
                        tmp_path / "state" / project / "upgrades/coordination/ownership.json"
                    ).read_text()
                )
                generations.append(active["generation"])
                if attempt == 0:
                    subprocess.run(
                        [*compose, "restart", "coire-upgrade-executor"],
                        env=env,
                        capture_output=True,
                        check=True,
                        timeout=20,
                    )
            assert generations[1] > generations[0]
            record_measurement(
                tmp_path,
                project,
                "measured-bootstrap",
                {
                    "project": project,
                    "source_sha256": seed.source.manifest_sha256,
                    "executor_image_id": seed.executor_image_id,
                    "writer_generations": generations,
                    "service_image_ids": core["service_image_ids"],
                    "observed_at": core["observed_at"],
                    "scope": "actual Docker/SQL bootstrap and qualified restart; no application upgrade or model smoke",
                },
            )
            if handover:
                image, owned_candidate_tag = candidate_image(seed.executor_image_id)
                target = prepare_executor_candidate(
                    REPO, tmp_path, env, frozen, client, headers, seed, image
                )
                request = {
                    "release_id": str(target.release_id),
                    "expected_incumbent_seed_sha256": seed.sha256(),
                    "idempotency_key": str(uuid4()),
                }
                initiated = client.post(
                    "/api/v1/admin/upgrades/executor-handover", headers=headers, json=request
                )
                assert initiated.status_code == 202
                accepted = initiated.json()
                repeated = client.post(
                    "/api/v1/admin/upgrades/executor-handover", headers=headers, json=request
                )
                assert repeated.status_code == 200
                assert repeated.json()["handover_id"] == accepted["handover_id"]
                deadline = time.monotonic() + (420 if crash_candidate else 270)
                started_wait = time.monotonic()
                phases = []
                candidate_stopped = False
                service_restarted = False
                service_outage_s: float | None = None
                while True:
                    status = client.get(
                        "/api/v1/admin/upgrades/executor-handover/" + accepted["handover_id"],
                        headers=headers,
                    )
                    assert status.status_code == 200
                    current = status.json()
                    if not phases or phases[-1] != current["phase"]:
                        phases.append(current["phase"])
                    actual_owner = json.loads(
                        (
                            tmp_path / "state" / project / "upgrades/coordination/ownership.json"
                        ).read_text()
                    )
                    if (
                        restart_service is not None
                        and not service_restarted
                        and actual_owner["state"] == "active"
                        and actual_owner["generation"] == accepted["generation"]
                        and actual_owner["executor_container_id"] != seed.executor_container_id
                    ):
                        service_id = (
                            subprocess.check_output(
                                [*compose, "ps", "-q", restart_service], env=env, timeout=10
                            )
                            .decode()
                            .strip()
                        )
                        before = json.loads(
                            subprocess.check_output(["docker", "inspect", service_id], timeout=10)
                        )[0]
                        assert before["State"]["Running"] is True
                        assert before["Config"]["Labels"]["com.docker.compose.project"] == project
                        outage_started = time.monotonic()
                        subprocess.run(
                            ["docker", "restart", "--time=5", service_id],
                            capture_output=True,
                            check=True,
                            timeout=20,
                        )
                        service_deadline = time.monotonic() + 30
                        while True:
                            after = json.loads(
                                subprocess.check_output(
                                    ["docker", "inspect", service_id], timeout=10
                                )
                            )[0]
                            assert after["Image"] == before["Image"]
                            assert after["Config"] == before["Config"]
                            assert after["HostConfig"] == before["HostConfig"]
                            assert sorted(
                                after["Mounts"], key=lambda mount: mount["Destination"]
                            ) == sorted(before["Mounts"], key=lambda mount: mount["Destination"])
                            if after["State"].get("Health", {}).get("Status") == "healthy":
                                break
                            assert time.monotonic() < service_deadline, (
                                "restarted service failed health"
                            )
                            time.sleep(0.2)
                        service_outage_s = time.monotonic() - outage_started
                        assert service_outage_s < 30
                        service_restarted = True
                    if (
                        crash_candidate
                        and not candidate_stopped
                        and current["phase"] in {"transfer_consumed", "claimed"}
                        and actual_owner["state"] == "active"
                        and actual_owner["generation"] == accepted["generation"]
                        and actual_owner["executor_container_id"] != seed.executor_container_id
                    ):
                        candidate = json.loads(
                            subprocess.check_output(
                                [
                                    "docker",
                                    "inspect",
                                    project
                                    + "-coire-upgrade-executor-"
                                    + str(accepted["generation"]),
                                ],
                                timeout=10,
                            )
                        )[0]
                        assert candidate["Image"] == image
                        assert candidate["Id"] == actual_owner["executor_container_id"]
                        assert (
                            candidate["Config"]["Labels"]["com.docker.compose.project"] == project
                        )
                        assert candidate["State"]["Running"] is True
                        if restart_incumbent:
                            predecessor = json.loads(
                                subprocess.check_output(
                                    ["docker", "inspect", seed.executor_container_id], timeout=10
                                )
                            )[0]
                            assert predecessor["Image"] == seed.executor_image_id
                            assert predecessor["State"]["Running"] is True
                            subprocess.run(
                                ["docker", "restart", "--time=5", seed.executor_container_id],
                                capture_output=True,
                                check=True,
                                timeout=20,
                            )
                            restarted = json.loads(
                                subprocess.check_output(
                                    ["docker", "inspect", seed.executor_container_id], timeout=10
                                )
                            )[0]
                            assert restarted["State"]["Running"] is True
                            assert restarted["Image"] == predecessor["Image"]
                            assert restarted["Config"] == predecessor["Config"]
                            assert restarted["HostConfig"] == predecessor["HostConfig"]
                            assert sorted(
                                restarted["Mounts"], key=lambda mount: mount["Destination"]
                            ) == sorted(
                                predecessor["Mounts"], key=lambda mount: mount["Destination"]
                            )
                        subprocess.run(
                            ["docker", "stop", "--time=5", candidate["Id"]],
                            capture_output=True,
                            check=True,
                            timeout=15,
                        )
                        candidate_stopped = True
                        if restart_candidate:
                            subprocess.run(
                                ["docker", "start", candidate["Id"]],
                                capture_output=True,
                                check=True,
                                timeout=15,
                            )
                    terminal = "restored" if crash_candidate else "committed"
                    if current["phase"] == terminal:
                        break
                    if current["phase"] in {"committed", "restored", "failed_unchanged"}:
                        failures: dict[str, list[str]] = {}
                        for role, identity in {
                            "incumbent": seed.executor_container_id,
                            "candidate": actual_owner["executor_container_id"],
                        }.items():
                            output = subprocess.run(
                                ["docker", "logs", "--tail=100", identity],
                                capture_output=True,
                                text=True,
                                timeout=10,
                            )
                            messages = []
                            for line in (output.stdout + output.stderr).splitlines():
                                try:
                                    entry = json.loads(line)
                                except ValueError:
                                    continue
                                if (
                                    isinstance(entry, dict)
                                    and entry.get("logger") == "coire_upgrade_executor.supervisor"
                                    and entry.get("level") == "WARNING"
                                    and isinstance(entry.get("msg"), str)
                                ):
                                    messages.append(entry["msg"][:1024])
                            failures[role] = messages[-8:]
                        record_measurement(
                            tmp_path,
                            project,
                            "unexpected-terminal",
                            {
                                "status": current,
                                "phases": phases,
                                "restarted_service": restart_service,
                                "service_outage_seconds": service_outage_s,
                                "executor_failures": failures,
                            },
                        )
                    assert current["phase"] not in {"committed", "restored", "failed_unchanged"}, (
                        "executor handover reached an unexpected terminal phase"
                    )
                    assert time.monotonic() < deadline, (
                        "handover failed its bounded composed acceptance"
                    )
                    time.sleep(0.5)
                elapsed = time.monotonic() - started_wait
                if crash_candidate:
                    assert candidate_stopped
                else:
                    assert elapsed >= 120, "healthy handover must honor the real stability interval"
                if restart_service is not None:
                    assert service_restarted
                retirement_deadline = time.monotonic() + 30
                while True:
                    retained_source = json.loads(
                        subprocess.check_output(
                            ["docker", "inspect", seed.executor_container_id], timeout=10
                        )
                    )[0]
                    assert retained_source["Image"] == seed.executor_image_id
                    if retained_source["State"]["Running"] is crash_candidate:
                        break
                    assert time.monotonic() < retirement_deadline, (
                        "committed candidate did not retire its retained predecessor"
                    )
                    time.sleep(0.5)
                if crash_candidate:
                    readiness_deadline = time.monotonic() + 30
                    while True:
                        targets = client.get("/api/v1/admin/upgrades/targets", headers=headers)
                        assert targets.status_code == 200
                        restored_core = next(
                            item
                            for item in targets.json()["items"]
                            if item["kind"] == "control_plane"
                        )
                        if restored_core["readiness"] == "ready":
                            assert restored_core["observed_sha256"] == seed.source.manifest_sha256
                            break
                        assert time.monotonic() < readiness_deadline, (
                            "SQL-restored Source did not reacquire qualified idle readiness"
                        )
                        time.sleep(0.5)
                if restart_committed:
                    committed_owner = json.loads(
                        (
                            tmp_path / "state" / project / "upgrades/coordination/ownership.json"
                        ).read_text()
                    )
                    selected_executor = committed_owner["executor_container_id"]
                    assert selected_executor != seed.executor_container_id
                    assert committed_owner["state"] == "active"
                    subprocess.run(
                        ["docker", "restart", "--time=5", selected_executor],
                        capture_output=True,
                        check=True,
                        timeout=20,
                    )
                    restart_deadline = time.monotonic() + 30
                    while True:
                        targets = client.get("/api/v1/admin/upgrades/targets", headers=headers)
                        assert targets.status_code == 200
                        selected_core = next(
                            item
                            for item in targets.json()["items"]
                            if item["kind"] == "control_plane"
                        )
                        resumed_owner = json.loads(
                            (
                                tmp_path
                                / "state"
                                / project
                                / "upgrades/coordination/ownership.json"
                            ).read_text()
                        )
                        if (
                            selected_core["readiness"] == "ready"
                            and resumed_owner["state"] == "active"
                            and resumed_owner["generation"] > committed_owner["generation"]
                        ):
                            assert resumed_owner["executor_container_id"] == selected_executor
                            assert selected_core["observed_sha256"] == seed.source.manifest_sha256
                            break
                        assert time.monotonic() < restart_deadline, (
                            "committed executor did not reacquire fresh qualified writer ownership"
                        )
                        time.sleep(0.5)
                record_measurement(
                    tmp_path,
                    project,
                    "measured-handover",
                    {
                        "handover_id": accepted["handover_id"],
                        "generation": accepted["generation"],
                        "source_image_id": seed.executor_image_id,
                        "candidate_image_id": image,
                        "phases": phases,
                        "prepared_at": accepted["prepared_at"],
                        "deadline_at": accepted["deadline_at"],
                        "fixture_only": True,
                        "elapsed_seconds": elapsed,
                        "retained_source_running": retained_source["State"]["Running"],
                        "candidate_failure": crash_candidate,
                        "candidate_restart": restart_candidate,
                        "incumbent_restart": restart_incumbent,
                        "committed_candidate_restart": restart_committed,
                        "restarted_service": restart_service,
                        "service_outage_seconds": service_outage_s,
                    },
                )
        coordination = tmp_path / "state" / project / "upgrades/coordination"
        assert (coordination / "deployment.lease").exists()
        owner = json.loads((coordination / "ownership.json").read_text())
        assert owner["owner_kind"] == "executor" and owner["state"] == "active"
        assert owner["job_id"] is None
        if not handover or crash_candidate:
            assert owner["executor_container_id"] == seed.executor_container_id
        if handover:
            hold = json.loads((coordination / "handover-hold.json").read_text())
            assert hold["released"] is True
        completed = True
    finally:
        executor_ids = (
            subprocess.check_output(
                [
                    "docker",
                    "ps",
                    "-aq",
                    "--filter",
                    "label=com.docker.compose.project=" + project,
                    "--filter",
                    "label=com.docker.compose.service=coire-upgrade-executor",
                ],
                timeout=10,
            )
            .decode()
            .splitlines()
        )
        health_observations = []
        for identity in executor_ids:
            observed = json.loads(
                subprocess.check_output(["docker", "inspect", identity], timeout=10)
            )[0]
            assert observed["Config"]["Labels"]["com.docker.compose.project"] == project
            health_observations.append(
                {
                    "container_id": observed["Id"],
                    "image_id": observed["Image"],
                    "health": observed["State"].get("Health"),
                    "status": observed["State"]["Status"],
                }
            )
            namespace = subprocess.run(
                [
                    "docker",
                    "exec",
                    identity,
                    "/app/.venv/bin/python3",
                    "-c",
                    "import json;from pathlib import Path;"
                    "r=Path('/opt/coire/upgrades/state/bootstrap');"
                    "print(json.dumps([{'name':p.name,'uid':p.stat().st_uid,"
                    "'mode':p.stat().st_mode & 511,'bytes':p.stat().st_size} "
                    "for p in r.iterdir()]))",
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=15,
            )
            health_observations[-1]["bootstrap_namespace"] = (
                json.loads(namespace.stdout) if namespace.returncode == 0 else None
            )
            journal_snapshot = subprocess.run(
                [
                    "docker",
                    "exec",
                    identity,
                    "/app/.venv/bin/python3",
                    "-c",
                    "import json;from pathlib import Path;"
                    "r=Path('/opt/coire/upgrades/state/bootstrap');"
                    "paths=sorted(r.rglob('*.json'));"
                    "assert len(paths)<=256;"
                    "assert sum(p.stat().st_size for p in paths)<=4194304;"
                    "print(json.dumps({str(p.relative_to(r)):json.loads(p.read_bytes()) "
                    "for p in paths}))",
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=15,
            )
            if journal_snapshot.returncode == 0:
                private_journal = tmp_path / ("executor-journal-" + identity[:12] + ".json")
                private_journal.write_text(journal_snapshot.stdout)
                private_journal.chmod(0o600)
        record_measurement(
            tmp_path, project, "executor-health", {"observations": health_observations}
        )
        source = frozen if frozen.exists() else REPO / "deploy/compose/compose.yaml"
        logs = subprocess.run(
            [
                "docker",
                "compose",
                "-p",
                project,
                "-f",
                str(source),
                "logs",
                "--no-color",
                "--tail=3000",
                "coire-upgrade-executor",
            ],
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        private_log = tmp_path / "executor-output.txt"
        private_log.write_text(logs.stdout + logs.stderr)
        private_log.chmod(0o600)
        api_logs = subprocess.run(
            [
                "docker",
                "compose",
                "-p",
                project,
                "-f",
                str(source),
                "logs",
                "--no-color",
                "--tail=100",
                "coire-api",
            ],
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        private_api_log = tmp_path / "api-output.txt"
        private_api_log.write_text(api_logs.stdout + api_logs.stderr)
        private_api_log.chmod(0o600)
        subprocess.run(
            [
                "docker",
                "compose",
                "-p",
                project,
                "-f",
                str(source),
                "down",
                "-v",
                "--remove-orphans",
            ],
            env=env,
            capture_output=True,
            check=True,
            timeout=60,
        )
        remaining = subprocess.run(
            ["docker", "ps", "-aq", "--filter", "label=com.docker.compose.project=" + project],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        for identity in remaining.stdout.splitlines():
            inspected = json.loads(
                subprocess.check_output(["docker", "inspect", identity], timeout=10)
            )[0]
            assert inspected["Config"]["Labels"]["com.docker.compose.project"] == project
            assert inspected["Config"]["Labels"]["com.docker.compose.service"] == (
                "coire-upgrade-executor"
            )
            assert "coire.bootstrap.preparation_sha256" in inspected["Config"]["Labels"]
            full_id = inspected["Id"]
            if inspected["State"]["Running"]:
                subprocess.run(
                    ["docker", "stop", "--time=5", full_id],
                    capture_output=True,
                    check=True,
                    timeout=15,
                )
            stopped = json.loads(
                subprocess.check_output(["docker", "inspect", full_id], timeout=10)
            )[0]
            assert stopped["State"]["Running"] is False
            subprocess.run(["docker", "rm", full_id], capture_output=True, check=True, timeout=10)
        assert not subprocess.check_output(
            ["docker", "ps", "-aq", "--filter", "label=com.docker.compose.project=" + project],
            timeout=10,
        ).strip()
        networks = (
            subprocess.check_output(
                [
                    "docker",
                    "network",
                    "ls",
                    "-q",
                    "--filter",
                    "label=com.docker.compose.project=" + project,
                ],
                timeout=10,
            )
            .decode()
            .splitlines()
        )
        for identity in networks:
            network = json.loads(
                subprocess.check_output(["docker", "network", "inspect", identity], timeout=10)
            )[0]
            assert network["Labels"]["com.docker.compose.project"] == project
            assert not network["Containers"]
            subprocess.run(
                ["docker", "network", "rm", network["Id"]],
                capture_output=True,
                check=True,
                timeout=10,
            )
        for role in ("coire-upgrade-prepared", "coire-upgrade-state"):
            volume = project + "_" + role
            if (
                subprocess.run(
                    ["docker", "volume", "inspect", volume], capture_output=True, timeout=10
                ).returncode
                == 0
            ):
                subprocess.run(
                    ["docker", "volume", "rm", volume],
                    capture_output=True,
                    check=True,
                    timeout=10,
                )
        shutil.rmtree(tmp_path / "secrets", ignore_errors=False)
        if owned_candidate_tag is not None:
            subprocess.run(
                ["docker", "image", "rm", owned_candidate_tag],
                capture_output=True,
                check=True,
                timeout=15,
            )
        if completed:
            assert not (
                tmp_path / "state" / project / "upgrades/coordination/deployment.lease"
            ).exists()
