"""Translate the frozen supported subset, preserving existing topology and binds."""

import re
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Any, Literal, cast

from pydantic import JsonValue

from coire_core.errors import UpgradeConflict
from coire_core.models.upgrades import (
    UpgradeComposeConfiguration,
    UpgradeComposeDatabase,
    UpgradeComposeExecution,
    UpgradeComposeMigration,
    UpgradeComposeResources,
    UpgradeComposeService,
    UpgradeDockerImageDefaults,
    UpgradeMountBinding,
    UpgradeServiceSpec,
)

DURATION = re.compile(r"([0-9]+(?:\.[0-9]+)?)(ns|us|ms|s|m|h)")
DURATION_SCALE = {
    "ns": 1,
    "us": 1000,
    "ms": 1_000_000,
    "s": 1_000_000_000,
    "m": 60_000_000_000,
    "h": 3_600_000_000_000,
}


def bind_physical_network_names(payload: dict[str, Any], names: Mapping[str, str]) -> None:
    """Use sealed physical names for already attested endpoint IDs, without new access."""
    endpoints = payload["NetworkingConfig"]["EndpointsConfig"]
    if any(logical not in names for logical in endpoints):
        raise UpgradeConflict("Frozen physical network name is unavailable")
    physical = {names[logical]: options for logical, options in endpoints.items()}
    if len(physical) != len(endpoints):
        raise UpgradeConflict("Frozen physical network names are ambiguous")
    payload["NetworkingConfig"]["EndpointsConfig"] = physical


def duration_ns(value: str) -> int:
    matched = DURATION.fullmatch(value)
    if matched is None:
        raise UpgradeConflict("Unsupported frozen duration")
    result = Decimal(matched[1]) * DURATION_SCALE[matched[2]]
    if result != result.to_integral_value() or not 0 < result <= 900_000_000_000:
        raise UpgradeConflict("Frozen duration exceeds its bound")
    return int(result)


def memory_bytes(value: str | int | None) -> int:
    if isinstance(value, bool) or value is None:
        raise UpgradeConflict("A bounded container memory limit is required")
    if isinstance(value, int):
        result = value
    else:
        matched = re.fullmatch(r"([0-9]+)([bkmg]?)", value.lower())
        if matched is None:
            raise UpgradeConflict("Unsupported container memory unit")
        result = (
            int(matched[1]) * {"": 1, "b": 1, "k": 1024, "m": 1024**2, "g": 1024**3}[matched[2]]
        )
    if not 1024**2 <= result <= 64 * 1024**3:
        raise UpgradeConflict("Container memory exceeds its fixed bound")
    return result


def translate_service(
    source: UpgradeServiceSpec,
    target: UpgradeServiceSpec,
    *,
    project: str,
    network_ids: Mapping[str, str],
    mount_bindings: tuple[UpgradeMountBinding, ...] = (),
    dependency_states: Mapping[str, str] | None = None,
    enforce_dependency_readiness: bool = True,
) -> dict[str, Any]:
    """Pure validation precedes any stop/create call. Mount additions are refused."""
    try:
        previous = UpgradeComposeService.model_validate(source.configuration)
        candidate = UpgradeComposeService.model_validate(target.configuration)
    except ValueError:
        raise UpgradeConflict("Frozen service uses unsupported or unsafe configuration") from None
    if source.service != target.service or re.fullmatch(r"[a-z][a-z0-9_-]{1,62}", project) is None:
        raise UpgradeConflict("Frozen service project or role differs")
    if (source.image_id, target.image_id) != (previous.image, candidate.image):
        raise UpgradeConflict("Frozen service image differs")
    return _translate_execution(
        previous,
        candidate,
        role=target.service,
        project=project,
        network_ids=network_ids,
        mount_bindings=mount_bindings,
        dependency_states=dependency_states,
        enforce_dependency_readiness=enforce_dependency_readiness,
    )


def translate_migration(
    source: Mapping[str, JsonValue],
    target: Mapping[str, JsonValue],
    *,
    project: str,
    network_ids: Mapping[str, str],
    mount_bindings: tuple[UpgradeMountBinding, ...] = (),
    dependency_states: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Translate only the fixed one-shot role without command or topology overrides."""
    try:
        previous = UpgradeComposeMigration.model_validate(source)
        candidate = UpgradeComposeMigration.model_validate(target)
    except ValueError:
        raise UpgradeConflict("Frozen migration uses unsupported or unsafe configuration") from None
    if re.fullmatch(r"[a-z][a-z0-9_-]{1,62}", project) is None:
        raise UpgradeConflict("Frozen migration project is malformed")
    return _translate_execution(
        previous,
        candidate,
        role="coire-migrate",
        project=project,
        network_ids=network_ids,
        mount_bindings=mount_bindings,
        dependency_states=dependency_states,
    )


def _translate_execution(
    previous: UpgradeComposeExecution,
    candidate: UpgradeComposeExecution,
    *,
    role: str,
    project: str,
    network_ids: Mapping[str, str],
    mount_bindings: tuple[UpgradeMountBinding, ...],
    dependency_states: Mapping[str, str] | None,
    enforce_dependency_readiness: bool = True,
) -> dict[str, Any]:
    if (
        previous.networks != candidate.networks
        or previous.ports != candidate.ports
        or previous.volumes != candidate.volumes
        or previous.secrets != candidate.secrets
        or previous.depends_on != candidate.depends_on
    ):
        raise UpgradeConflict("Managed replacement cannot alter topology or mounts")
    if previous.entrypoint != candidate.entrypoint or previous.command != candidate.command:
        raise UpgradeConflict("Managed replacement cannot introduce another command")
    for argv in (candidate.entrypoint, candidate.command):
        if argv and any(
            part.rsplit("/", 1)[-1] in {"sh", "bash", "dash", "ash"} or part == "-c"
            for part in argv
        ):
            raise UpgradeConflict("Managed replacement cannot introduce shell execution")
    mounts = translate_mounts(candidate, mount_bindings)
    resource_limits: dict[str, Any] = {}
    if candidate.deploy is not None:
        if set(candidate.deploy) != {"resources"}:
            raise UpgradeConflict("Deployment settings are outside the supported resource subset")
        resources = candidate.deploy["resources"]
        if not isinstance(resources, dict) or set(resources) != {"limits"}:
            raise UpgradeConflict("Deployment resources require explicit limits")
        limits = resources["limits"]
        if not isinstance(limits, dict) or limits.keys() - {"cpus", "memory", "pids"}:
            raise UpgradeConflict("Deployment resource limits are unsupported")
        resource_limits = dict(limits)
        if "memory" in resource_limits and memory_bytes(resource_limits["memory"]) != memory_bytes(
            candidate.mem_limit
        ):
            raise UpgradeConflict("Deployment and service memory limits differ")
        if "pids" in resource_limits and resource_limits["pids"] != candidate.pids_limit:
            raise UpgradeConflict("Deployment and service process limits differ")
        if "cpus" in resource_limits and str(resource_limits["cpus"]) != str(candidate.cpus):
            raise UpgradeConflict("Deployment and service CPU limits differ")
    for name, condition in candidate.depends_on.items():
        if condition.keys() - {"condition", "required", "restart"}:
            raise UpgradeConflict("Dependency configuration is unsupported")
        expected = {
            "service_healthy": "healthy",
            "service_completed_successfully": "completed",
        }.get(str(condition.get("condition")))
        if expected is None or (
            enforce_dependency_readiness and (dependency_states or {}).get(name) != expected
        ):
            raise UpgradeConflict("Existing dependency has not satisfied its health gate")
        if (
            condition.get("required", True) is not True
            or condition.get("restart", False) is not False
        ):
            raise UpgradeConflict("Dependency restart or optional gates are unsupported")
    if any(
        re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", key) is None
        or (value is not None and ("\x00" in value or len(value) > 32768))
        for key, value in candidate.environment.items()
    ):
        raise UpgradeConflict("Frozen environment has malformed keys or values")
    if any(value is None for value in candidate.environment.values()):
        raise UpgradeConflict("Frozen environment contains unresolved values")
    if any(
        re.search(r"(?:TOKEN|PASSWORD|SECRET|PRIVATE_KEY)$", key) and value
        for key, value in candidate.environment.items()
    ):
        raise UpgradeConflict("Service credentials must remain immutable secret mounts")
    networks: dict[str, Any] = {}
    # PostgreSQL JSONB does not preserve object order. With priority overrides
    # excluded, Compose selects the default network by its logical name.
    for name, options in sorted(candidate.networks.items()):
        identity = network_ids.get(name)
        if identity is None or re.fullmatch(r"[a-f0-9]{64}", identity) is None:
            raise UpgradeConflict("Existing network identity is unavailable")
        options = options or {}
        if options.keys() - {"aliases"}:
            raise UpgradeConflict("Network overrides are outside the frozen subset")
        aliases = options.get("aliases", [])
        if not isinstance(aliases, list) or any(
            not isinstance(alias, str) or re.fullmatch(r"[a-z][a-z0-9_.-]{0,127}", alias) is None
            for alias in aliases
        ):
            raise UpgradeConflict("Frozen network aliases are malformed")
        # Compose adds its service DNS name even when no explicit aliases are
        # configured. Fixed-role replacements must preserve that existing name
        # so the retained executor and peer services can reach the new cohort.
        networks[name] = {"NetworkID": identity, "Aliases": list(dict.fromkeys([role, *aliases]))}
    ports: dict[str, list[dict[str, str]]] = {}
    for port in candidate.ports:
        if (
            port.keys() - {"target", "published", "host_ip", "protocol", "mode"}
            or port.get("mode", "ingress") != "ingress"
        ):
            raise UpgradeConflict("Published port settings are unsupported")
        destination, published = port.get("target"), port.get("published")
        protocol = port.get("protocol", "tcp")
        address = port.get("host_ip", "")
        if (
            type(destination) is not int
            or not 1 <= destination <= 65535
            or not isinstance(published, str)
            or not published.isdigit()
            or not 1 <= int(published) <= 65535
            or protocol not in {"tcp", "udp"}
            or not isinstance(address, str)
        ):
            raise UpgradeConflict("Published port binding is malformed")
        key = f"{destination}/{protocol}"
        if key in ports:
            raise UpgradeConflict("Published port binding is duplicated")
        ports[key] = [{"HostIp": address, "HostPort": published}]
    if (
        candidate.logging.keys() - {"driver", "options"}
        or candidate.logging.get("driver") != "json-file"
    ):
        raise UpgradeConflict("Unsupported container logging driver")
    log_options = candidate.logging.get("options")
    if (
        not isinstance(log_options, dict)
        or set(log_options) != {"max-size", "max-file"}
        or not all(isinstance(v, str) for v in log_options.values())
    ):
        raise UpgradeConflict("Bounded container logging is required")
    if not re.fullmatch(r"[1-9][0-9]?m", str(log_options["max-size"])) or not re.fullmatch(
        r"[1-9]", str(log_options["max-file"])
    ):
        raise UpgradeConflict("Container log retention exceeds its bound")
    health = candidate.healthcheck if isinstance(candidate, UpgradeComposeService) else None
    if health is not None and (
        health.test[0] != "CMD"
        or health.test[1].rsplit("/", 1)[-1]
        in {
            "sh",
            "bash",
            "dash",
            "ash",
        }
    ):
        raise UpgradeConflict("Healthchecks require exec-form commands")
    if candidate.stop_signal not in {None, "SIGTERM", "TERM", "15"}:
        raise UpgradeConflict("Service stop signal differs from graceful termination")
    tmpfs = {}
    for mount in candidate.tmpfs:
        path, _, mount_options = mount.partition(":")
        if path not in {"/tmp", "/run", "/opt/coire/failover"} or path in tmpfs:
            raise UpgradeConflict("Temporary mount is outside the supported subset")
        if mount_options and any(
            option
            not in {
                "rw",
                "nosuid",
                "nodev",
                "noexec",
                "mode=1777",
                "mode=0700",
                "uid=65532",
                "gid=65532",
            }
            for option in mount_options.split(",")
        ):
            raise UpgradeConflict("Temporary mount options are unsupported")
        tmpfs[path] = mount_options
    host: dict[str, Any] = {
        "ReadonlyRootfs": True,
        "CapDrop": ["ALL"],
        "SecurityOpt": ["no-new-privileges:true"],
        "RestartPolicy": {"Name": "" if candidate.restart == "no" else candidate.restart},
        "PidsLimit": candidate.pids_limit,
        "Memory": memory_bytes(candidate.mem_limit),
        "Mounts": mounts,
        "PortBindings": ports,
        "Tmpfs": tmpfs,
        "LogConfig": {"Type": "json-file", "Config": log_options},
        "NetworkMode": next(iter(networks.values()))["NetworkID"],
    }
    if candidate.cpus is not None:
        try:
            cpus = Decimal(str(candidate.cpus)) * 1_000_000_000
        except InvalidOperation:
            raise UpgradeConflict("Container CPU limit is malformed") from None
        if (
            not cpus.is_finite()
            or not 0 < cpus <= 64_000_000_000
            or cpus != cpus.to_integral_value()
        ):
            raise UpgradeConflict("Container CPU limit exceeds its bound")
        host["NanoCpus"] = int(cpus)
    stop_ns = duration_ns(candidate.stop_grace_period)
    if stop_ns % 1_000_000_000 or not 1_000_000_000 <= stop_ns <= 30_000_000_000:
        raise UpgradeConflict("Graceful stop requires a whole bounded interval")
    exposed = dict.fromkeys(ports)
    for item in candidate.expose:
        matched = re.fullmatch(r"([0-9]{1,5})(?:/(tcp|udp))?", item)
        if matched is None or not 1 <= int(matched[1]) <= 65535:
            raise UpgradeConflict("Exposed port is malformed")
        exposed[f"{int(matched[1])}/{matched[2] or 'tcp'}"] = None
    if candidate.working_dir is not None and (
        not candidate.working_dir.startswith("/")
        or "\x00" in candidate.working_dir
        or ".." in candidate.working_dir.split("/")
    ):
        raise UpgradeConflict("Container working directory is malformed")
    payload: dict[str, Any] = {
        "Image": candidate.image,
        "User": candidate.user,
        "HostConfig": host,
        "Env": [f"{key}={value}" for key, value in sorted(candidate.environment.items())],
        "Labels": dict(candidate.labels)
        | {"com.docker.compose.project": project, "com.docker.compose.service": role},
        "ExposedPorts": {key: {} for key in exposed},
        "NetworkingConfig": {"EndpointsConfig": networks},
        "StopTimeout": stop_ns // 1_000_000_000,
    }
    if health is not None:
        payload["Healthcheck"] = {
            "Test": list(health.test),
            "Interval": duration_ns(health.interval),
            "Timeout": duration_ns(health.timeout),
            "Retries": health.retries,
            "StartPeriod": duration_ns(health.start_period),
        }
    if candidate.stop_signal is not None:
        payload["StopSignal"] = candidate.stop_signal
    if health is not None and health.start_interval is not None:
        payload["Healthcheck"]["StartInterval"] = duration_ns(health.start_interval)
    if candidate.entrypoint is not None:
        payload["Entrypoint"] = list(candidate.entrypoint)
    if candidate.command is not None:
        payload["Cmd"] = list(candidate.command)
    if candidate.working_dir is not None:
        payload["WorkingDir"] = candidate.working_dir
    return payload


def secret_target(value: JsonValue) -> str:
    """Resolved Compose may use the exact absolute target instead of its basename."""
    if not isinstance(value, str):
        raise UpgradeConflict("Secret target is malformed")
    basename = value.removeprefix("/run/secrets/")
    if re.fullmatch(r"[a-z][a-z0-9_]{0,127}", basename) is None:
        raise UpgradeConflict("Secret target is outside its fixed private directory")
    return "/run/secrets/" + basename


def translate_mounts(
    service: UpgradeComposeConfiguration,
    bindings: tuple[UpgradeMountBinding, ...],
) -> list[dict[str, Any]]:
    """Use only positively observed source mounts and preserve access and physical names."""
    by_target = {item.target: item for item in bindings}
    if len(by_target) != len(bindings):
        raise UpgradeConflict("Existing mount targets are duplicated")
    requested: set[str] = set()
    result: list[dict[str, Any]] = []
    for item in service.volumes:
        if item.keys() - {"type", "source", "target", "read_only", "bind", "volume"}:
            raise UpgradeConflict("Frozen mount settings are unsupported")
        target = item.get("target")
        binding = by_target.get(str(target))
        if (
            not isinstance(target, str)
            or target in requested
            or binding is None
            or item.get("type") != binding.type
            or item.get("source") != binding.logical_source
            or item.get("read_only", False) is not binding.read_only
        ):
            raise UpgradeConflict("Existing physical mount identity differs")
        if binding.type == "bind":
            options = item.get("bind", {})
            if not isinstance(options, dict) or options.keys() - {
                "create_host_path",
                "propagation",
            }:
                raise UpgradeConflict("Bind options are unsupported")
            if (
                options.get("create_host_path", False) is not False
                or options.get("propagation", "rprivate") != "rprivate"
            ):
                raise UpgradeConflict("Bind mounts cannot create paths or propagate writes")
            if binding.source != binding.logical_source:
                raise UpgradeConflict("Frozen bind source differs from its physical source")
        else:
            options = item.get("volume", {})
            if not isinstance(options, dict) or options.keys() - {"nocopy"}:
                raise UpgradeConflict("Volume options are unsupported")
        requested.add(target)
        result.append(
            {
                "Type": binding.type,
                "Source": binding.source,
                "Target": target,
                "ReadOnly": binding.read_only,
            }
            | (
                {"VolumeOptions": {"NoCopy": True}}
                if binding.type == "volume"
                else {"BindOptions": {"Propagation": "rprivate", "CreateMountpoint": False}}
            )
        )
    for item in service.secrets:
        if item.keys() - {"source", "target", "uid", "gid", "mode"}:
            raise UpgradeConflict("Frozen secret mount settings are unsupported")
        name = item.get("source")
        if not isinstance(name, str):
            raise UpgradeConflict("Secret identity is malformed")
        target = secret_target(item.get("target", name))
        binding = by_target.get(target)
        if (
            target in requested
            or binding is None
            or binding.type != "bind"
            or not binding.read_only
            or binding.logical_source != name
        ):
            raise UpgradeConflict("Immutable secret generation is not bound")
        if any(key in item for key in ("uid", "gid", "mode")):
            raise UpgradeConflict(
                "Secret ownership overrides require physical ownership validation"
            )
        requested.add(target)
        result.append(
            {
                "Type": "bind",
                "Source": binding.source,
                "Target": target,
                "ReadOnly": True,
                "BindOptions": {"Propagation": "rprivate", "CreateMountpoint": False},
            }
        )
    if requested != set(by_target):
        raise UpgradeConflict("Existing mount set differs from frozen service")
    return result


def reconcile_mounts(
    service: UpgradeServiceSpec,
    physical: JsonValue,
    resources: UpgradeComposeResources,
) -> tuple[UpgradeMountBinding, ...]:
    """Bind observed mounts to hash-verified frozen names before any replacement effect."""
    try:
        configuration = UpgradeComposeService.model_validate(service.configuration)
    except ValueError:
        raise UpgradeConflict("Frozen mount configuration is unsupported") from None
    return reconcile_execution_mounts(configuration, physical, resources)


def reconcile_execution_mounts(
    configuration: UpgradeComposeConfiguration,
    physical: JsonValue,
    resources: UpgradeComposeResources,
) -> tuple[UpgradeMountBinding, ...]:
    """Reconcile exact mounts for a validated long-running or fixed one-shot role."""
    expected: dict[str, tuple[str, str, str, bool]] = {}
    for item in configuration.volumes:
        target, logical, kind = item.get("target"), item.get("source"), item.get("type")
        readonly = item.get("read_only", False)
        if (
            not isinstance(target, str)
            or not isinstance(logical, str)
            or kind not in {"bind", "volume"}
            or type(readonly) is not bool
        ):
            raise UpgradeConflict("Frozen volume identity is incomplete")
        source = logical if kind == "bind" else resources.volumes.get(logical)
        if source is None or target in expected:
            raise UpgradeConflict("Frozen physical volume name is unavailable")
        expected[target] = (str(kind), source, logical, readonly)
    for item in configuration.secrets:
        logical = item.get("source")
        if not isinstance(logical, str):
            raise UpgradeConflict("Frozen secret identity is incomplete")
        target = secret_target(item.get("target", logical))
        source = resources.secrets.get(logical)
        if source is None or target in expected:
            raise UpgradeConflict("Frozen secret generation is unavailable")
        expected[target] = ("bind", source, logical, True)
    if not isinstance(physical, list) or len(physical) != len(expected):
        raise UpgradeConflict("Observed source mount set differs")
    result: list[UpgradeMountBinding] = []
    seen: set[str] = set()
    for observed_mount in physical:
        if not isinstance(observed_mount, dict):
            raise UpgradeConflict("Observed source mount is malformed")
        target = observed_mount.get("Destination")
        binding = expected.get(str(target))
        if not isinstance(target, str) or target in seen or binding is None:
            raise UpgradeConflict("Observed source mount target differs")
        kind, source, logical, readonly = binding
        observed_source = (
            observed_mount.get("Name") if kind == "volume" else observed_mount.get("Source")
        )
        if (
            observed_mount.get("Type") != kind
            or observed_source != source
            or observed_mount.get("RW") is not (not readonly)
        ):
            raise UpgradeConflict("Observed source mount identity or access differs")
        if kind == "bind" and observed_mount.get("Propagation", "rprivate") != "rprivate":
            raise UpgradeConflict("Observed source bind propagation differs")
        try:
            result.append(
                UpgradeMountBinding(
                    type=cast(Literal["bind", "volume"], kind),
                    source=source,
                    logical_source=logical,
                    target=target,
                    read_only=readonly,
                )
            )
        except ValueError:
            raise UpgradeConflict("Observed source mount identity is malformed") from None
        seen.add(target)
    # Apply the same supported-option checks used for the replacement payload.
    translate_mounts(configuration, tuple(result))
    return tuple(sorted(result, key=lambda binding: binding.target))


def frozen_resources(document: dict[str, JsonValue], project: str) -> UpgradeComposeResources:
    """Resolve existing physical resource names from the verified frozen document."""
    if document.get("name") != project:
        raise UpgradeConflict("Frozen resource project differs")
    volumes, secrets = document.get("volumes", {}), document.get("secrets", {})
    if not isinstance(volumes, dict) or not isinstance(secrets, dict):
        raise UpgradeConflict("Frozen resources are malformed")
    volume_names: dict[str, str] = {}
    secret_files: dict[str, str] = {}
    for name, entry in volumes.items():
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
            raise UpgradeConflict("Frozen volume has no explicit physical name")
        volume_names[name] = str(entry["name"])
    for name, entry in secrets.items():
        if (
            not isinstance(entry, dict)
            or entry.keys() - {"name", "file"}
            or not isinstance(entry.get("file"), str)
        ):
            raise UpgradeConflict("Frozen secret is not an immutable file binding")
        path = str(entry["file"])
        if not path.startswith("/") or ".." in path.split("/") or "\x00" in path:
            raise UpgradeConflict("Frozen secret path is malformed")
        secret_files[name] = path
    try:
        return UpgradeComposeResources(volumes=volume_names, secrets=secret_files)
    except ValueError:
        raise UpgradeConflict("Frozen resource set exceeds its bound") from None


def verify_source_configuration(
    service: UpgradeServiceSpec,
    body: dict[str, JsonValue],
    image: UpgradeDockerImageDefaults,
    *,
    project: str,
    network_ids: Mapping[str, str],
    mounts: tuple[UpgradeMountBinding, ...],
    dependency_states: Mapping[str, str],
    enforce_dependency_readiness: bool = True,
) -> dict[str, Any]:
    """Refuse actual Source drift before using frozen configuration for a replacement."""
    if image.image_id != service.image_id or body.get("Image") != image.image_id:
        raise UpgradeConflict("Source image defaults differ from the frozen image")
    expected = translate_service(
        service,
        service,
        project=project,
        network_ids=network_ids,
        mount_bindings=mounts,
        dependency_states=dependency_states,
        enforce_dependency_readiness=enforce_dependency_readiness,
    )
    return verify_execution_configuration(body, image, expected, network_ids=network_ids)


def verify_database_configuration(
    body: dict[str, JsonValue],
    image: UpgradeDockerImageDefaults,
    configuration: UpgradeComposeDatabase,
    *,
    project: str,
    network_ids: Mapping[str, str],
    mounts: tuple[UpgradeMountBinding, ...],
) -> None:
    """Inspect existing PostgreSQL configuration without producing mutation authority."""
    # Reuse topology/resource comparison after validating the separate existing
    # PostgreSQL exception. The translated non-root role is never dispatched.
    safe = UpgradeComposeService.model_validate(
        configuration.model_dump(exclude={"cap_add", "user"})
        | {
            "user": "65532:65532",
            "tmpfs": ["/tmp"],
            "healthcheck": configuration.healthcheck.model_dump()
            | {"test": ["CMD", "pg_isready", "-U", "coire", "-d", "coire"]},
        }
    )
    expected = _translate_execution(
        safe,
        safe,
        role="postgres",
        project=project,
        network_ids=network_ids,
        mount_bindings=mounts,
        dependency_states={},
    )
    expected["User"] = configuration.user or ""
    expected["HostConfig"]["CapAdd"] = list(configuration.cap_add)
    expected["HostConfig"]["Tmpfs"] = dict.fromkeys(configuration.tmpfs, "")
    expected["Healthcheck"]["Test"] = list(configuration.healthcheck.test)
    actual_config = body.get("Config")
    if (
        configuration.stop_grace_period == "10s"
        and isinstance(actual_config, dict)
        and actual_config.get("StopTimeout") is None
    ):
        # Compose leaves Docker's existing ten-second default unset on postgres.
        body = body | {"Config": actual_config | {"StopTimeout": 10}}
    verify_execution_configuration(body, image, expected, network_ids=network_ids)


def verify_execution_configuration(
    body: dict[str, JsonValue],
    image: UpgradeDockerImageDefaults,
    expected: dict[str, Any],
    *,
    network_ids: Mapping[str, str],
) -> dict[str, Any]:
    """Compare actual immutable-image execution with an already validated fixed payload."""
    if body.get("Image") != image.image_id or expected.get("Image") != image.image_id:
        raise UpgradeConflict("Execution image differs from its accepted immutable defaults")
    config, host = body.get("Config"), body.get("HostConfig")
    if not isinstance(config, dict) or not isinstance(host, dict):
        raise UpgradeConflict("Source runtime configuration is incomplete")
    for key in ("User",):
        if config.get(key) != expected[key]:
            raise UpgradeConflict("Source execution identity differs")
    for key in (
        "ReadonlyRootfs",
        "CapDrop",
        "SecurityOpt",
        "PidsLimit",
        "Memory",
        "PortBindings",
        "Tmpfs",
        "LogConfig",
    ):
        if host.get(key) != expected["HostConfig"][key]:
            raise UpgradeConflict("Source hardened configuration differs")
    network_settings = body.get("NetworkSettings")
    attachments = network_settings.get("Networks") if isinstance(network_settings, dict) else None
    if not isinstance(attachments, dict) or len(attachments) != len(network_ids):
        raise UpgradeConflict("Source network attachment set differs")
    physical_networks: dict[str, str] = {}
    for name, attachment in attachments.items():
        if not isinstance(attachment, dict) or not isinstance(attachment.get("NetworkID"), str):
            raise UpgradeConflict("Source network attachment identity is incomplete")
        physical_networks[name] = str(attachment["NetworkID"])
    if set(physical_networks.values()) != set(network_ids.values()):
        raise UpgradeConflict("Source physical network set differs")
    expected_endpoints = {
        endpoint["NetworkID"]: endpoint
        for endpoint in expected["NetworkingConfig"]["EndpointsConfig"].values()
    }
    own_name = body.get("Name")
    for attachment in attachments.values():
        if not isinstance(attachment, dict) or not isinstance(attachment.get("NetworkID"), str):
            raise UpgradeConflict("Source network attachment identity is incomplete")
        aliases = attachment.get("Aliases")
        required_aliases = set(expected_endpoints[str(attachment["NetworkID"])]["Aliases"])
        allowed_aliases = required_aliases | (
            {own_name.removeprefix("/")} if isinstance(own_name, str) else set()
        )
        if (
            not isinstance(aliases, list)
            or any(not isinstance(alias, str) for alias in aliases)
            or not required_aliases.issubset(aliases)
            or not set(aliases).issubset(allowed_aliases)
        ):
            raise UpgradeConflict("Source service DNS aliases differ")
    primary = expected["HostConfig"]["NetworkMode"]
    if host.get("NetworkMode") not in {
        primary,
        *(name for name, identity in physical_networks.items() if identity == primary),
    }:
        raise UpgradeConflict("Source primary network differs")
    if host.get("NanoCpus", 0) != expected["HostConfig"].get("NanoCpus", 0):
        raise UpgradeConflict("Source CPU limit differs")
    actual_added = host.get("CapAdd") or []
    if (
        not isinstance(actual_added, list)
        or any(not isinstance(value, str) for value in actual_added)
        or sorted(value.removeprefix("CAP_") for value in cast(list[str], actual_added))
        != sorted(expected["HostConfig"].get("CapAdd") or [])
    ):
        raise UpgradeConflict("Source added capabilities differ from its fixed role")
    if host.get("Privileged", False) is not False or any(
        host.get(key)
        for key in (
            "Devices",
            "DeviceRequests",
            "ExtraHosts",
            "Dns",
            "DnsSearch",
            "Sysctls",
            "CgroupParent",
        )
    ):
        raise UpgradeConflict("Source container has unapproved elevated settings")
    if (
        host.get("PidMode", "")
        or host.get("UTSMode", "")
        or host.get("IpcMode", "private") not in {"private", ""}
    ):
        raise UpgradeConflict("Source process namespaces differ")
    restart = host.get("RestartPolicy")
    if (
        not isinstance(restart, dict)
        or ("" if restart.get("Name") == "no" else restart.get("Name"))
        != expected["HostConfig"]["RestartPolicy"]["Name"]
        or restart.get("MaximumRetryCount", 0) != 0
    ):
        raise UpgradeConflict("Source restart policy differs")
    env: dict[str, str] = {}
    for item in image.environment:
        key, separator, value = item.partition("=")
        if not separator or key in env:
            raise UpgradeConflict("Immutable image environment is malformed")
        env[key] = value
    for item in expected["Env"]:
        key, _, value = item.partition("=")
        env[key] = value
    actual_env = config.get("Env")
    if (
        not isinstance(actual_env, list)
        or len(actual_env) != len(env)
        or any(not isinstance(item, str) for item in actual_env)
        or {str(item) for item in actual_env} != {f"{key}={value}" for key, value in env.items()}
    ):
        raise UpgradeConflict("Source environment differs from the frozen image and service")
    for key, defaults in (("Entrypoint", image.entrypoint), ("Cmd", image.command)):
        wanted = expected.get(key, list(defaults) if defaults is not None else None)
        if config.get(key) != wanted:
            raise UpgradeConflict("Source process command differs")
    if config.get("WorkingDir", "") != expected.get("WorkingDir", image.working_dir):
        raise UpgradeConflict("Source working directory differs")
    if config.get("StopTimeout") != expected["StopTimeout"]:
        raise UpgradeConflict("Source graceful stop interval differs")
    if (config.get("StopSignal") or None) != (
        expected.get("StopSignal", image.stop_signal) or None
    ):
        raise UpgradeConflict("Source graceful stop signal differs")
    health = config.get("Healthcheck")
    if "Healthcheck" in expected:
        if not isinstance(health, dict) or any(
            health.get(key) != value for key, value in expected["Healthcheck"].items()
        ):
            raise UpgradeConflict("Source healthcheck differs")
    elif health not in (None, {}):
        raise UpgradeConflict("One-shot execution cannot introduce a healthcheck")
    exposed = config.get("ExposedPorts", {})
    if not isinstance(exposed, dict) or set(exposed) != set(expected["ExposedPorts"]) | set(
        image.exposed_ports
    ):
        raise UpgradeConflict("Source exposed port set differs")
    return expected
