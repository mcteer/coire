"""Actual Compose spelling and JSONB object order preserve strict execution checks."""

import copy
import hashlib
import json

import pytest

from coire_core.errors import UpgradeConflict
from coire_core.models.upgrades import UpgradeDockerImageDefaults, UpgradeServiceSpec
from coire_upgrade_executor.compose import translate_service, verify_execution_configuration


def execution() -> tuple[dict, dict, UpgradeDockerImageDefaults]:
    configuration = {
        "image": "sha256:" + "a" * 64,
        "user": "65532:65532",
        "read_only": True,
        "cap_drop": ["ALL"],
        "security_opt": ["no-new-privileges:true"],
        "restart": "no",
        "pids_limit": 64,
        "mem_limit": "128m",
        "networks": {"coire-telemetry": None, "coire-file-processing": None},
        "logging": {"driver": "json-file", "options": {"max-size": "10m", "max-file": "3"}},
        "healthcheck": {"test": ["CMD", "/app/.venv/bin/python3", "-V"]},
    }
    service = UpgradeServiceSpec(
        service="coire-file-worker",
        image_id=configuration["image"],
        configuration=configuration,
        configuration_sha256=hashlib.sha256(
            json.dumps(configuration, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
    )
    networks = {"coire-telemetry": "b" * 64, "coire-file-processing": "c" * 64}
    expected = translate_service(service, service, project="coire", network_ids=networks)
    reordered = copy.deepcopy(configuration)
    reordered["networks"] = dict(reversed(list(configuration["networks"].items())))
    other = service.model_copy(update={"configuration": reordered})
    assert translate_service(other, other, project="coire", network_ids=networks) == expected
    body = {
        "Image": configuration["image"],
        "Config": {
            key: value
            for key, value in expected.items()
            if key not in {"Image", "HostConfig", "NetworkingConfig"}
        },
        "HostConfig": copy.deepcopy(expected["HostConfig"]),
        "NetworkSettings": {
            "Networks": {
                "coire_" + name: {"NetworkID": identity, "Aliases": ["coire-file-worker"]}
                for name, identity in networks.items()
            }
        },
    }
    body["HostConfig"]["NetworkMode"] = "coire_coire-file-processing"
    return body, expected, UpgradeDockerImageDefaults(image_id=configuration["image"])


@pytest.mark.parametrize("restart", ["", "no", "always", "unless-stopped"])
def test_disabled_restart_spellings_preserve_policy_check(restart: str) -> None:
    body, expected, image = execution()
    body["HostConfig"]["RestartPolicy"] = {"Name": restart, "MaximumRetryCount": 0}
    networks = {"coire-telemetry": "b" * 64, "coire-file-processing": "c" * 64}
    if restart in {"", "no"}:
        verify_execution_configuration(body, image, expected, network_ids=networks)
    else:
        with pytest.raises(UpgradeConflict, match="restart policy"):
            verify_execution_configuration(body, image, expected, network_ids=networks)


def test_wrong_primary_network_remains_rejected() -> None:
    body, expected, image = execution()
    body["HostConfig"]["NetworkMode"] = "coire_coire-telemetry"
    with pytest.raises(UpgradeConflict, match="primary network"):
        verify_execution_configuration(
            body,
            image,
            expected,
            network_ids={"coire-telemetry": "b" * 64, "coire-file-processing": "c" * 64},
        )


def test_fixed_role_replacement_preserves_service_dns_on_each_existing_network() -> None:
    _, expected, _ = execution()
    endpoints = expected["NetworkingConfig"]["EndpointsConfig"]
    assert set(endpoints) == {"coire-telemetry", "coire-file-processing"}
    for endpoint in endpoints.values():
        assert endpoint["Aliases"] == ["coire-file-worker"]


@pytest.mark.parametrize("aliases", [[], ["foreign"], ["coire-file-worker", "coire-api"]])
def test_missing_or_foreign_service_dns_aliases_reject_actual_drift(aliases: list[str]) -> None:
    body, expected, image = execution()
    body["NetworkSettings"]["Networks"]["coire_coire-file-processing"]["Aliases"] = aliases
    with pytest.raises(UpgradeConflict, match="DNS aliases"):
        verify_execution_configuration(
            body,
            image,
            expected,
            network_ids={"coire-telemetry": "b" * 64, "coire-file-processing": "c" * 64},
        )
