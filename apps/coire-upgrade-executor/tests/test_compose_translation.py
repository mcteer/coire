"""Frozen replacement retains topology and requires all production hardening."""

import hashlib
import json

import pytest
from pydantic import JsonValue

from coire_core.errors import UpgradeConflict
from coire_core.models.upgrades import UpgradeServiceSpec


def service(**changes: JsonValue) -> UpgradeServiceSpec:
    value: dict[str, JsonValue] = {
        "image": "sha256:" + "a" * 64,
        "user": "65532:65532",
        "read_only": True,
        "cap_drop": ["ALL"],
        "security_opt": ["no-new-privileges:true"],
        "restart": "unless-stopped",
        "stop_grace_period": "5s",
        "pids_limit": 128,
        "mem_limit": "256m",
        "tmpfs": ["/tmp"],
        "networks": {"coire-internal": {"aliases": ["coire-api"]}},
        "ports": [
            {
                "target": 8000,
                "published": "8180",
                "host_ip": "127.0.0.1",
                "protocol": "tcp",
                "mode": "ingress",
            }
        ],
        "environment": {"UPGRADES_ENABLED": "false"},
        "healthcheck": {
            "test": ["CMD", "/healthcheck", "http://127.0.0.1:8000/ready"],
            "interval": "20s",
            "timeout": "2s",
            "retries": 3,
            "start_period": "10s",
        },
        "logging": {"driver": "json-file", "options": {"max-size": "10m", "max-file": "3"}},
    }
    value.update(changes)
    return UpgradeServiceSpec(
        service="coire-api",
        image_id=str(value["image"]),
        configuration=value,
        configuration_sha256=hashlib.sha256(
            json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
    )


def test_translated_payload_preserves_bound_network_ports_and_hardening() -> None:
    from coire_upgrade_executor.compose import translate_service

    value = service()
    payload = translate_service(
        value, value, project="coire", network_ids={"coire-internal": "b" * 64}
    )
    assert payload["Image"] == value.image_id
    host = payload["HostConfig"]
    assert host["ReadonlyRootfs"] is True and host["CapDrop"] == ["ALL"]
    assert host["PortBindings"] == {"8000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8180"}]}
    assert host["Memory"] == 256 * 1024**2
    assert payload["NetworkingConfig"]["EndpointsConfig"]["coire-internal"]["NetworkID"] == "b" * 64
    assert payload["Healthcheck"]["Interval"] == 20_000_000_000


def test_explicit_graceful_stop_signal_is_retained() -> None:
    from coire_upgrade_executor.compose import translate_service

    value = service(stop_signal="SIGTERM")
    payload = translate_service(
        value, value, project="coire", network_ids={"coire-internal": "b" * 64}
    )
    assert payload["StopSignal"] == "SIGTERM"


def test_retained_recovery_configuration_can_start_before_its_peer_is_restored() -> None:
    from coire_upgrade_executor.compose import translate_service

    value = service(depends_on={"postgres": {"condition": "service_healthy"}})
    network_ids = {"coire-internal": "b" * 64}
    with pytest.raises(UpgradeConflict, match="health gate"):
        translate_service(
            value, value, project="coire", network_ids=network_ids, dependency_states={}
        )
    retained = translate_service(
        value,
        value,
        project="coire",
        network_ids=network_ids,
        dependency_states={},
        enforce_dependency_readiness=False,
    )
    assert retained["Image"] == value.image_id
    changed = service(depends_on={})
    with pytest.raises(UpgradeConflict, match="topology"):
        translate_service(
            value,
            changed,
            project="coire",
            network_ids=network_ids,
            enforce_dependency_readiness=False,
        )


@pytest.mark.parametrize("target", ["postgres_password", "/run/secrets/postgres_password"])
def test_resolved_secret_target_preserves_the_same_exact_private_binding(target: str) -> None:
    from coire_core.models.upgrades import (
        UpgradeComposeResources,
        UpgradeComposeService,
        UpgradeMountBinding,
    )
    from coire_upgrade_executor.compose import reconcile_execution_mounts, translate_service

    value = service(secrets=[{"source": "postgres_password", "target": target}])
    binding = UpgradeMountBinding(
        type="bind",
        logical_source="postgres_password",
        source="/private/credentials/postgres_password",
        target="/run/secrets/postgres_password",
        read_only=True,
    )
    observed = reconcile_execution_mounts(
        UpgradeComposeService.model_validate(value.configuration),
        [{"Type": "bind", "Source": binding.source, "Destination": binding.target, "RW": False}],
        UpgradeComposeResources(secrets={"postgres_password": binding.source}),
    )
    assert observed == (binding,)
    payload = translate_service(
        value,
        value,
        project="coire",
        network_ids={"coire-internal": "b" * 64},
        mount_bindings=observed,
    )
    assert payload["HostConfig"]["Mounts"][0]["Target"] == binding.target
    assert payload["HostConfig"]["Mounts"][0]["BindOptions"]["CreateMountpoint"] is False


@pytest.mark.parametrize(
    "target",
    ["/etc/postgres_password", "/run/secrets/../postgres_password", "postgres_password/nested"],
)
def test_secret_target_cannot_escape_its_existing_private_directory(target: str) -> None:
    from coire_upgrade_executor.compose import secret_target

    with pytest.raises(UpgradeConflict):
        secret_target(target)


def test_one_shot_migration_has_fixed_role_and_exit_semantics() -> None:
    from coire_upgrade_executor.compose import translate_migration

    source = service(
        restart="no", ports=[], networks={"coire-db": {}, "coire-telemetry": {}}
    ).configuration.copy()
    source.pop("healthcheck")
    target = source | {"image": "sha256:" + "b" * 64}
    payload = translate_migration(
        source,
        target,
        project="coire",
        network_ids={"coire-db": "c" * 64, "coire-telemetry": "d" * 64},
    )
    assert payload["Image"] == target["image"]
    assert payload["Labels"]["com.docker.compose.service"] == "coire-migrate"
    assert payload["HostConfig"]["RestartPolicy"] == {"Name": ""}
    assert payload["HostConfig"]["PortBindings"] == {}
    assert "Healthcheck" not in payload and "Cmd" not in payload and "Entrypoint" not in payload
    changes: tuple[dict[str, JsonValue], ...] = (
        {"command": ["sh", "-c", "bad"]},
        {"restart": "always"},
        {"networks": {"coire-edge": {}}},
        {"secrets": [{"source": "another-secret"}]},
    )
    for change in changes:
        with pytest.raises(UpgradeConflict):
            translate_migration(
                source,
                target | change,
                project="coire",
                network_ids={"coire-db": "c" * 64, "coire-telemetry": "d" * 64},
            )


@pytest.mark.parametrize(
    "change",
    [
        {"privileged": True},
        {"network_mode": "host"},
        {"pid": "host"},
        {"cap_add": ["SYS_ADMIN"]},
        {"read_only": False},
        {"user": "root"},
        {"entrypoint": ["sh", "-c", "danger"]},
        {"healthcheck": {"test": ["CMD-SHELL", "danger"]}},
        {"networks": {"coire-edge": {}}},
        {"volumes": [{"type": "volume", "source": "new-data", "target": "/data"}]},
    ],
)
def test_unsupported_or_widened_configuration_is_refused_before_replacement(
    change: dict[str, JsonValue],
) -> None:
    from coire_upgrade_executor.compose import translate_service

    with pytest.raises(UpgradeConflict):
        translate_service(
            service(), service(**change), project="coire", network_ids={"coire-internal": "b" * 64}
        )


def test_existing_mounts_preserve_volume_and_secret_generation() -> None:
    from coire_core.models.upgrades import UpgradeMountBinding
    from coire_upgrade_executor.compose import translate_service

    value = service(
        volumes=[{"type": "volume", "source": "coire-blobs", "target": "/opt/coire/blobs"}],
        secrets=[{"source": "node_tokens"}],
        depends_on={"postgres": {"condition": "service_healthy", "required": True}},
    )
    bindings = (
        UpgradeMountBinding(
            type="volume",
            source="coire_coire-blobs",
            logical_source="coire-blobs",
            target="/opt/coire/blobs",
            read_only=False,
        ),
        UpgradeMountBinding(
            type="bind",
            source="/private/coire/secrets/source/node_tokens",
            logical_source="node_tokens",
            target="/run/secrets/node_tokens",
            read_only=True,
        ),
    )
    payload = translate_service(
        value,
        value,
        project="coire",
        network_ids={"coire-internal": "b" * 64},
        mount_bindings=bindings,
        dependency_states={"postgres": "healthy"},
    )
    mounts = payload["HostConfig"]["Mounts"]
    assert mounts[0]["Source"] == "coire_coire-blobs"
    assert mounts[0]["VolumeOptions"] == {"NoCopy": True}
    assert mounts[1]["ReadOnly"] is True
    assert mounts[1]["Source"] == bindings[1].source
    assert mounts[1]["BindOptions"]["CreateMountpoint"] is False
    with pytest.raises(UpgradeConflict):
        translate_service(
            value,
            value,
            project="coire",
            network_ids={"coire-internal": "b" * 64},
            mount_bindings=bindings,
            dependency_states={"postgres": "unhealthy"},
        )


@pytest.mark.parametrize("fault", ["missing", "readonly", "extra", "logical"])
def test_unbound_or_changed_mounts_are_refused(fault: str) -> None:
    from coire_core.models.upgrades import UpgradeMountBinding
    from coire_upgrade_executor.compose import translate_service

    value = service(volumes=[{"type": "volume", "source": "coire-blobs", "target": "/data"}])
    binding = UpgradeMountBinding(
        type="volume",
        source="coire_coire-blobs",
        logical_source="coire-blobs",
        target="/data",
        read_only=False,
    )
    bindings: tuple[UpgradeMountBinding, ...] = (binding,)
    if fault == "missing":
        bindings = ()
    elif fault == "readonly":
        bindings = (binding.model_copy(update={"read_only": True}),)
    elif fault == "logical":
        bindings = (binding.model_copy(update={"logical_source": "other"}),)
    else:
        bindings += (binding.model_copy(update={"target": "/extra"}),)
    with pytest.raises(UpgradeConflict):
        translate_service(
            value,
            value,
            project="coire",
            network_ids={"coire-internal": "b" * 64},
            mount_bindings=bindings,
        )


@pytest.mark.parametrize("fault", [None, "generation", "access", "missing", "extra", "duplicate"])
def test_observed_mounts_bind_frozen_physical_generation(fault: str | None) -> None:
    from coire_core.models.upgrades import UpgradeComposeResources
    from coire_upgrade_executor.compose import reconcile_mounts

    value = service(
        volumes=[{"type": "volume", "source": "coire-blobs", "target": "/data"}],
        secrets=[{"source": "node_tokens"}],
    )
    resources = UpgradeComposeResources(
        volumes={"coire-blobs": "coire_coire-blobs"},
        secrets={"node_tokens": "/private/coire/secrets/source/node_tokens"},
    )
    physical: list[JsonValue] = [
        {"Type": "volume", "Name": "coire_coire-blobs", "Destination": "/data", "RW": True},
        {
            "Type": "bind",
            "Source": "/private/coire/secrets/other/node_tokens"
            if fault == "generation"
            else resources.secrets["node_tokens"],
            "Destination": "/run/secrets/node_tokens",
            "RW": fault == "access",
            "Propagation": "rprivate",
        },
    ]
    if fault == "missing":
        physical.pop()
    elif fault == "extra":
        physical.append(
            {"Type": "bind", "Source": "/private/foreign", "Destination": "/foreign", "RW": False}
        )
    elif fault == "duplicate":
        physical[1] = physical[0]
    if fault is None:
        result = reconcile_mounts(value, physical, resources)
        assert result[0].source == "coire_coire-blobs"
        assert result[1].source == resources.secrets["node_tokens"]
        assert result[1].read_only is True
        assert reconcile_mounts(value, list(reversed(physical)), resources) == result
    else:
        with pytest.raises(UpgradeConflict):
            reconcile_mounts(value, physical, resources)


def test_created_endpoints_use_sealed_physical_names_and_original_ids() -> None:
    from coire_upgrade_executor.compose import bind_physical_network_names, translate_service

    payload = translate_service(
        service(), service(), project="coire", network_ids={"coire-internal": "b" * 64}
    )
    original = payload["NetworkingConfig"]["EndpointsConfig"]["coire-internal"].copy()
    bind_physical_network_names(payload, {"coire-internal": "isolated_coire-internal"})
    assert payload["NetworkingConfig"]["EndpointsConfig"] == {"isolated_coire-internal": original}
    assert payload["HostConfig"]["NetworkMode"] == "b" * 64
    with pytest.raises(UpgradeConflict):
        bind_physical_network_names(payload, {})


def test_resolved_deployment_limits_and_exposed_ports_are_translated() -> None:
    from coire_upgrade_executor.compose import translate_service

    value = service(
        cpus="0.5",
        deploy={"resources": {"limits": {"cpus": "0.5", "memory": "256m", "pids": 128}}},
        expose=["8001/tcp"],
    )
    payload = translate_service(
        value, value, project="coire", network_ids={"coire-internal": "b" * 64}
    )
    assert payload["HostConfig"]["NanoCpus"] == 500_000_000
    assert "8001/tcp" in payload["ExposedPorts"]
    assert "8001/tcp" not in payload["HostConfig"]["PortBindings"]


@pytest.mark.parametrize(
    "fault",
    [None, "environment", "command", "privileged", "memory", "network", "health", "namespace"],
)
def test_source_actual_configuration_must_match_frozen_image_and_service(fault: str | None) -> None:
    from typing import cast

    from coire_core.models.upgrades import UpgradeDockerImageDefaults
    from coire_upgrade_executor.compose import translate_service, verify_source_configuration

    value = service()
    expected = translate_service(
        value, value, project="coire", network_ids={"coire-internal": "b" * 64}
    )
    body = {
        "Image": value.image_id,
        "Config": {
            "User": expected["User"],
            "Env": expected["Env"] + ["PATH=/usr/bin"],
            "Cmd": ["python", "-m", "coire_api"],
            "Entrypoint": None,
            "WorkingDir": "/app",
            "StopTimeout": expected["StopTimeout"],
            "Healthcheck": expected["Healthcheck"],
            "ExposedPorts": expected["ExposedPorts"],
        },
        "HostConfig": expected["HostConfig"]
        | {"Privileged": False, "IpcMode": "private", "NetworkMode": "coire_coire-internal"},
        "NetworkSettings": {
            "Networks": {
                "coire_coire-internal": {"NetworkID": "b" * 64, "Aliases": [value.service]}
            }
        },
    }
    if fault == "environment":
        body["Config"]["Env"] += ["FOREIGN=1"]
    elif fault == "command":
        body["Config"]["Cmd"] = ["foreign"]
    elif fault == "privileged":
        body["HostConfig"]["Privileged"] = True
    elif fault == "memory":
        body["HostConfig"]["Memory"] = 0
    elif fault == "network":
        body["NetworkSettings"]["Networks"]["coire_coire-internal"]["NetworkID"] = "f" * 64
    elif fault == "health":
        body["Config"]["Healthcheck"] = {"Test": ["CMD-SHELL", "danger"]}
    elif fault == "namespace":
        body["HostConfig"]["PidMode"] = "host"
    image = UpgradeDockerImageDefaults(
        image_id=value.image_id,
        environment=("PATH=/usr/bin",),
        command=("python", "-m", "coire_api"),
        working_dir="/app",
    )

    def verify() -> None:
        verify_source_configuration(
            value,
            cast(dict[str, JsonValue], body),
            image,
            project="coire",
            network_ids={"coire-internal": "b" * 64},
            mounts=(),
            dependency_states={},
        )

    if fault is None:
        verify()
    else:
        with pytest.raises(UpgradeConflict):
            verify()
