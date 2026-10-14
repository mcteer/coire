"""Existing PostgreSQL exception is inspected, never granted mutation authority."""

from copy import deepcopy
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from test_compose_translation import service
from upgrade_fixtures import core_plan

from coire_core.errors import UpgradeConflict, UpgradeForbidden, UpgradeUnavailable
from coire_core.models.upgrades import UpgradeComposeDatabase, UpgradeComposeService
from coire_core.settings import Settings
from coire_upgrade_executor.compose import _translate_execution
from coire_upgrade_executor.docker import UpgradeDocker


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "project",
        "image",
        "busy",
        "caps",
        "readonly",
        "command",
        "platform",
        "docker_defaults",
        "signal",
    ],
)
async def test_database_dependency_requires_exact_frozen_configuration(
    tmp_path: Path, fault: str | None
) -> None:
    role = UpgradeComposeDatabase.model_validate(
        service().configuration
        | {
            "user": None,
            "stop_grace_period": "10s" if fault == "docker_defaults" else "5s",
            "tmpfs": ["/tmp", "/run/postgresql", "/var/run/postgresql"],
            "ports": [],
            "networks": {"coire-db": {}},
            "cap_add": ["CHOWN", "DAC_OVERRIDE", "FOWNER", "SETGID", "SETUID"],
            "healthcheck": {"test": ["CMD-SHELL", "pg_isready -U coire -d coire"]},
        }
    )
    safe = UpgradeComposeService.model_validate(
        role.model_dump(exclude={"cap_add", "user"})
        | {"user": "65532:65532", "tmpfs": ["/tmp"], "healthcheck": {"test": ["CMD", "pg_isready"]}}
    )
    payload = _translate_execution(
        safe,
        safe,
        role="postgres",
        project="coire",
        network_ids={"coire-db": "b" * 64},
        mount_bindings=(),
        dependency_states={},
    )
    payload["Healthcheck"]["Test"] = list(role.healthcheck.test)
    body: dict[str, Any] = {
        "Id": "c" * 64,
        "Name": "/coire-postgres-1",
        "Image": role.image,
        "Config": {
            "User": "",
            "Env": payload["Env"],
            "Entrypoint": None,
            "Cmd": ["postgres"],
            "WorkingDir": "",
            "StopTimeout": 5,
            "Healthcheck": payload["Healthcheck"],
            "ExposedPorts": {"5432/tcp": {}},
            "Labels": payload["Labels"],
        },
        "HostConfig": payload["HostConfig"]
        | {"CapAdd": list(role.cap_add), "Tmpfs": dict.fromkeys(role.tmpfs, "")},
        "NetworkSettings": {
            "Networks": {
                "coire_coire-db": {
                    "NetworkID": "b" * 64,
                    "Aliases": [payload["Labels"]["com.docker.compose.service"]],
                }
            }
        },
        "Mounts": [],
        "State": {"Running": True, "Health": {"Status": "healthy"}},
    }
    body = deepcopy(body)
    if fault == "project":
        body["Config"]["Labels"]["com.docker.compose.project"] = "foreign"
    elif fault == "image":
        body["Image"] = "sha256:" + "f" * 64
    elif fault == "busy":
        body["State"]["Health"]["Status"] = "unhealthy"
    elif fault == "caps":
        body["HostConfig"]["CapAdd"].append("SYS_ADMIN")
    elif fault == "readonly":
        body["HostConfig"]["ReadonlyRootfs"] = False
    elif fault == "command":
        body["Config"]["Cmd"] = ["sh"]
    elif fault == "docker_defaults":
        body["Config"]["StopTimeout"] = None
        body["HostConfig"]["CapAdd"] = ["CAP_" + value for value in role.cap_add]
    elif fault == "signal":
        body["Config"]["StopSignal"] = "SIGKILL"
    calls: list[str] = []

    def transport(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        calls.append(request.url.path)
        if "/containers/" in request.url.path:
            assert request.url.path.endswith("/coire-postgres-1/json")
            return httpx.Response(200, json=body)
        return httpx.Response(
            200,
            json={
                "Id": role.image,
                "Os": "linux",
                "Architecture": "amd64" if fault == "platform" else "arm64",
                "Config": {"Env": [], "Cmd": ["postgres"], "ExposedPorts": {"5432/tcp": {}}},
            },
        )

    document = {"name": "coire", "networks": {"coire-db": {"name": "coire_coire-db"}}}
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport), trust_env=False) as http:
        docker = UpgradeDocker(Settings(), core_plan(), client=http)
        docker.database_role = role
        docker.images = docker.images | {role.image}
        with patch("coire_upgrade_executor.docker.read_prepared_compose", return_value=document):
            if fault and fault != "docker_defaults":
                with pytest.raises((UpgradeConflict, UpgradeUnavailable)):
                    await docker.inspect_database_dependency(tmp_path)
            else:
                observed = await docker.inspect_database_dependency(tmp_path)
                assert observed.role == "postgres" and observed.state == "healthy"
                assert observed.container_id == "c" * 64
        with pytest.raises(UpgradeForbidden):
            await docker._read("POST", "/containers/coire-postgres-1/stop")
    assert calls
