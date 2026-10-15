"""Release preparation must never change active credentials on validation failure."""

from __future__ import annotations

import json
import os
import runpy
import subprocess
from pathlib import Path
from typing import Any

import pytest

from coire_core.deploy import (
    REQUIRED_SECRET_FILES,
    ReleaseInputError,
    matching_active_generation,
    read_secret_inputs,
    release_manifest,
    stage_secret_generation,
    validate_external_secret_base,
    validate_project_name,
)


@pytest.mark.parametrize(
    "fault", ["none", "architecture", "os", "identity", "missing", "duplicate", "timeout"]
)
def test_upgrade_preparation_requires_exact_local_arm64_images(fault: str) -> None:
    from upgrade_fixtures import core_plan

    script = runpy.run_path(str(Path(__file__).resolve().parents[2] / "scripts/coire-deploy.py"))
    release = core_plan().target
    expected = release.manifest.services[0].image_id
    metadata = {
        "Id": "sha256:" + "f" * 64 if fault == "identity" else expected,
        "Os": "windows" if fault == "os" else "linux",
        "Architecture": "amd64" if fault == "architecture" else "arm64",
    }
    calls: list[tuple[str, ...]] = []

    def inspect(
        *args: str, check: bool = True, timeout: float | None = None
    ) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        assert timeout == 10
        if fault == "timeout":
            raise subprocess.TimeoutExpired(args, timeout)
        # Each service is checked against its own immutable image, without pulling.
        value = {**metadata, "Id": metadata["Id"] if fault == "identity" else args[-1]}
        return subprocess.CompletedProcess(
            args,
            1 if fault == "missing" else 0,
            json.dumps([value, value] if fault == "duplicate" else [value]),
            "",
        )

    verify = script["verify_upgrade_images"]
    verify.__globals__["docker"] = inspect
    if fault == "none":
        verify(release)
    else:
        with pytest.raises(ReleaseInputError):
            verify(release)
    assert calls and all(call[:2] == ("image", "inspect") for call in calls)


@pytest.mark.parametrize("missing", [False, True])
def test_upgrade_preparation_checks_separate_migration_image(tmp_path: Path, missing: bool) -> None:
    import hashlib

    from upgrade_fixtures import core_plan

    script = runpy.run_path(str(Path(__file__).resolve().parents[2] / "scripts/coire-deploy.py"))
    original = core_plan().target
    migration_image = "sha256:" + "c" * 64
    document = {
        "services": {
            "coire-migrate": {
                "image": migration_image,
                "user": "65532:65532",
                "read_only": True,
                "cap_drop": ["ALL"],
                "security_opt": ["no-new-privileges:true"],
                "restart": "no",
                "pids_limit": 128,
                "mem_limit": "512m",
                "networks": {"coire-db": {}, "coire-telemetry": {}},
                "logging": {"driver": "json-file", "options": {"max-size": "10m", "max-file": "3"}},
            }
        }
    }
    content = json.dumps(document).encode()
    digest = hashlib.sha256(content).hexdigest()
    manifest = original.manifest.model_copy(
        update={
            "compose_sha256": digest,
            "artifact_sha256s": {"compose.json": digest},
            "staged_bytes": len(content),
        }
    )
    release = original.model_copy(
        update={"manifest": manifest, "manifest_sha256": manifest.sha256()}
    )
    artifact = tmp_path / "compose.json"
    artifact.write_bytes(content)
    inspected: list[str] = []

    def inspect(
        *args: str, check: bool = True, timeout: float | None = None
    ) -> subprocess.CompletedProcess[str]:
        assert args[:2] == ("image", "inspect") and timeout == 10
        inspected.append(args[-1])
        return subprocess.CompletedProcess(
            args,
            1 if missing and args[-1] == migration_image else 0,
            json.dumps([{"Id": args[-1], "Os": "linux", "Architecture": "arm64"}]),
            "",
        )

    verify = script["verify_upgrade_images"]
    verify.__globals__["docker"] = inspect
    if missing:
        with pytest.raises(ReleaseInputError):
            verify(release, {"compose.json": artifact})
    else:
        verify(release, {"compose.json": artifact})
    assert migration_image in inspected
    assert original.manifest.services[0].image_id in inspected


def test_upgrade_preparation_rejects_unfrozen_database_before_image_inspection(
    tmp_path: Path,
) -> None:
    import hashlib
    from unittest.mock import Mock

    from upgrade_fixtures import core_plan

    script = runpy.run_path(str(Path(__file__).resolve().parents[2] / "scripts/coire-deploy.py"))
    original = core_plan().target
    content = json.dumps(
        {"services": {"postgres": {"image": "postgres@sha256:" + "d" * 64}}}
    ).encode()
    digest = hashlib.sha256(content).hexdigest()
    manifest = original.manifest.model_copy(
        update={
            "compose_sha256": digest,
            "artifact_sha256s": {"compose.json": digest},
            "staged_bytes": len(content),
        }
    )
    release = original.model_copy(
        update={"manifest": manifest, "manifest_sha256": manifest.sha256()}
    )
    artifact = tmp_path / "compose.json"
    artifact.write_bytes(content)
    verify = script["verify_upgrade_images"]
    inspect = Mock()
    verify.__globals__["docker"] = inspect
    with pytest.raises(ReleaseInputError, match="database or migration image configuration"):
        verify(release, {"compose.json": artifact})
    inspect.assert_not_called()


def test_missing_late_secret_preserves_active_generation(tmp_path: Path) -> None:
    secret_root = tmp_path / "secrets"
    active = secret_root / "old"
    active.mkdir(parents=True)
    sentinel = active / "postgres_password"
    sentinel.write_bytes(b"current-credential")
    values = {name: f"replacement-{name}" for name in REQUIRED_SECRET_FILES}
    values.pop("bootstrap_admin_email")

    with pytest.raises(ReleaseInputError, match="bootstrap_admin_email"):
        stage_secret_generation(secret_root, values)

    assert sentinel.read_bytes() == b"current-credential"
    assert sorted(p.name for p in secret_root.iterdir()) == ["old"]


def test_generation_is_complete_private_and_distinct(tmp_path: Path) -> None:
    values = {name: f"secret-{name}" for name in REQUIRED_SECRET_FILES}
    first = stage_secret_generation(tmp_path / "secrets", values)
    second = stage_secret_generation(tmp_path / "secrets", values)
    assert first != second
    assert {p.name for p in first.iterdir()} == set(REQUIRED_SECRET_FILES) | {
        "upgrade_executor_token",
        "ops_service_token",
        "file_worker_service_token",
        "failover_peer_key",
        "failover_relay_token",
        "failover_monitor_token",
        "failover_lifecycle_token",
        "openai_api_key",
        "anthropic_api_key",
    }
    assert first.stat().st_mode & 0o777 == 0o700
    assert all(p.stat().st_mode & 0o777 == 0o644 for p in first.iterdir())


def test_ops_credential_is_required_only_when_ops_profile_is_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in REQUIRED_SECRET_FILES:
        monkeypatch.setenv("COIRE_SECRET_" + name.upper(), "test-value")
    monkeypatch.delenv("COIRE_SECRET_OPS_SERVICE_TOKEN", raising=False)
    monkeypatch.delenv("COIRE_FAILOVER_MEMBER_NAME", raising=False)
    monkeypatch.setenv("COMPOSE_PROFILES", "")
    assert read_secret_inputs(from_env=True)["ops_service_token"] == ""
    monkeypatch.setenv("COMPOSE_PROFILES", "diagnostics,ops")
    with pytest.raises(ReleaseInputError, match="ops profile requires"):
        read_secret_inputs(from_env=True)


def test_file_worker_credential_is_required_only_for_its_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in REQUIRED_SECRET_FILES:
        monkeypatch.setenv("COIRE_SECRET_" + name.upper(), "test-value")
    monkeypatch.delenv("COIRE_SECRET_FILE_WORKER_SERVICE_TOKEN", raising=False)
    monkeypatch.delenv("COIRE_FAILOVER_MEMBER_NAME", raising=False)
    monkeypatch.setenv("COMPOSE_PROFILES", "")
    assert read_secret_inputs(from_env=True)["file_worker_service_token"] == ""
    monkeypatch.setenv("COMPOSE_PROFILES", "chat-files")
    with pytest.raises(ReleaseInputError, match="chat-files profile requires"):
        read_secret_inputs(from_env=True)


@pytest.mark.parametrize("value", ["../coire", "coire/it", "", "COIRE!", ".hidden"])
def test_project_name_rejects_paths(value: str) -> None:
    with pytest.raises(ReleaseInputError):
        validate_project_name(value)


def test_credentials_cannot_be_redirected_inside_source_tree(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(source)
    with pytest.raises(ReleaseInputError, match="outside the repository"):
        validate_external_secret_base(alias / "secrets", source)


def test_release_manifest_freezes_images_and_mounts_without_mutating_source(tmp_path: Path) -> None:
    source = {
        "services": {
            "coire-api": {
                "image": "coire-api:dev",
                "build": {"context": "/mutable/checkout"},
                "volumes": [
                    {
                        "type": "bind",
                        "source": "/mutable/checkout/generated",
                        "target": "/run/coire/cluster",
                    }
                ],
            },
            "postgres": {"image": "postgres@sha256:pinned"},
        },
        "secrets": {"postgres_password": {"file": "/old/password"}},
    }
    release = release_manifest(
        source,
        secret_dir=tmp_path / "secrets",
        cluster_dir=tmp_path / "cluster",
        image_ids={"coire-api": "sha256:immutable", "postgres": "sha256:database"},
    )
    assert release["services"]["coire-api"]["image"] == "sha256:immutable"
    assert release["services"]["postgres"]["image"] == "sha256:database"
    assert "build" not in release["services"]["coire-api"]
    assert release["services"]["coire-api"]["volumes"][0]["source"] == str(tmp_path / "cluster")
    assert release["services"]["coire-api"]["volumes"][0]["bind"]["create_host_path"] is False
    assert release["secrets"]["postgres_password"]["file"] == str(
        tmp_path / "secrets/postgres_password"
    )
    assert source["services"]["coire-api"]["image"] == "coire-api:dev"


@pytest.mark.parametrize(
    "service,image", [("coire-api", "coire-api:dev"), ("postgres", "postgres@sha256:pinned")]
)
def test_release_manifest_requires_every_local_image(
    tmp_path: Path, service: str, image: str
) -> None:
    with pytest.raises(ReleaseInputError, match="missing image ID"):
        release_manifest(
            {"services": {service: {"image": image}}, "secrets": {}},
            secret_dir=tmp_path,
            cluster_dir=tmp_path,
            image_ids={},
        )


def test_unchanged_active_credentials_reuse_mounted_generation(tmp_path: Path) -> None:
    project = "coiretest"
    state = tmp_path / "state"
    base = tmp_path / "credentials"
    values = dict.fromkeys(REQUIRED_SECRET_FILES, "same")
    generation = stage_secret_generation(base / project / "secrets", values)
    release = state / project / "releases" / "release-one"
    release.mkdir(parents=True)
    manifest = {
        "secrets": {
            name: {"file": str(generation / name)}
            for name in (*REQUIRED_SECRET_FILES, "failover_peer_key", "failover_relay_token")
        }
    }
    (release / "compose.json").write_text(json.dumps(manifest))
    (state / project / "current").symlink_to(release)
    assert matching_active_generation(state, base, project, values) == generation
    changed = {**values, "postgres_password": "different"}
    assert matching_active_generation(state, base, project, changed) is None


def test_installed_release_survives_source_change_and_preflight_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = runpy.run_path(str(Path(__file__).resolve().parents[2] / "scripts/coire-deploy.py"))
    install = script["install"]
    preflight = script["preflight"]
    compose_source = tmp_path / "compose.yaml"
    compose_source.write_text("source can change after installation")
    cluster_source = tmp_path / "generated"
    cluster_source.mkdir()
    (cluster_source / "hostfile.json").write_text("{}")
    secret_dir = stage_secret_generation(
        tmp_path / "credentials", {name: f"sentinel-{name}" for name in REQUIRED_SECRET_FILES}
    )
    monkeypatch.setenv("COMPOSE_PROJECT_NAME", "coiretest")
    monkeypatch.setenv("COIRE_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.setenv("COIRE_CLUSTER_CONFIG_DIR", str(cluster_source))
    monkeypatch.delenv("COMPOSE_FILE", raising=False)
    source = {
        "name": "coiretest",
        "services": {"coire-api": {"image": "coire-api:dev", "build": {"context": "/checkout"}}},
        "secrets": {"postgres_password": {"file": "/old/password"}},
    }
    calls: list[tuple[str, ...]] = []

    def fake_docker(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        if args[:2] == ("image", "inspect"):
            return subprocess.CompletedProcess(args, 0, "sha256:fixed-image\n", "")
        if "config" in args:
            return subprocess.CompletedProcess(args, 0, json.dumps(source), "")
        if args[:2] == ("ps", "-q"):
            return subprocess.CompletedProcess(args, 0, "existing-postgres\n", "")
        return subprocess.CompletedProcess(args, 1, "", "auth failed")

    install.__globals__["docker"] = fake_docker
    release: Path = install(secret_dir)
    manifest: dict[str, Any] = json.loads((release / "compose.json").read_text())
    assert manifest["services"]["coire-api"]["image"] == "sha256:fixed-image"
    assert manifest["secrets"]["postgres_password"]["file"] == str(secret_dir / "postgres_password")
    compose_source.unlink()
    assert (release / "compose.json").exists()
    preflight.__globals__["docker"] = fake_docker
    with pytest.raises(ReleaseInputError, match="application-network"):
        preflight(release)
    assert "sentinel-postgres_password" not in repr(calls)
    assert (secret_dir / "postgres_password").read_text() == "sentinel-postgres_password"


def test_recovery_backs_up_before_role_change_without_argv_leak(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = runpy.run_path(str(Path(__file__).resolve().parents[2] / "scripts/coire-deploy.py"))
    recover = script["recover_db_role"]
    secret_dir = stage_secret_generation(
        tmp_path / "credentials",
        {
            name: ("private'password" if name == "postgres_password" else "fixture")
            for name in REQUIRED_SECRET_FILES
        },
    )
    calls: list[tuple[list[str], str | None]] = []

    def fake_docker(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        assert args[:2] == ("ps", "-q")
        return subprocess.CompletedProcess(args, 0, "postgres-container\n", "")

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[Any]:
        calls.append((argv, kwargs.get("input")))
        if "pg_dump" in argv:
            kwargs["stdout"].write(b"PGDMP-sentinel")
            return subprocess.CompletedProcess(argv, 0, b"", b"")
        return subprocess.CompletedProcess(argv, 0, "ALTER ROLE\n", "")

    recover.__globals__["docker"] = fake_docker
    monkeypatch.setattr(subprocess, "run", fake_run)
    backup: Path = recover(secret_dir, tmp_path / "backups")
    assert backup.read_bytes() == b"PGDMP-sentinel"
    assert backup.stat().st_mode & 0o777 == 0o600
    assert "pg_dump" in calls[0][0]
    assert "ALTER ROLE" in (calls[1][1] or "")
    assert "private''password" in (calls[1][1] or "")
    assert all("private'password" not in repr(argv) for argv, _ in calls)


def test_down_cleans_only_owned_credentials_and_preserves_volumes(tmp_path: Path) -> None:
    repo = Path(__file__).resolve().parents[2]
    project = "coiretest"
    state = tmp_path / "state"
    release = state / project / "releases" / "release-one"
    release.mkdir(parents=True)
    (release / "compose.json").write_text("{}")
    (state / project / "current").symlink_to(release)
    base = tmp_path / "credentials"
    owned = stage_secret_generation(
        base / project / "secrets", dict.fromkeys(REQUIRED_SECRET_FILES, "owned-sentinel")
    )
    unrelated = stage_secret_generation(
        base / "different" / "secrets", dict.fromkeys(REQUIRED_SECRET_FILES, "other-sentinel")
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "docker-args"
    docker = bin_dir / "docker"
    docker.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$COIRE_TEST_DOCKER_LOG"\n')
    docker.chmod(0o755)
    env = {
        **os.environ,
        "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
        "COMPOSE_PROJECT_NAME": project,
        "COIRE_STATE_ROOT": str(state),
        "COIRE_SECRETS_BASE": str(base),
        "COIRE_TEST_DOCKER_LOG": str(log),
    }
    result = subprocess.run(
        [str(repo / "deploy/compose/coire-down")], capture_output=True, text=True, env=env
    )
    assert result.returncode == 0, result.stderr
    assert not owned.exists()
    assert unrelated.exists()
    assert "down --remove-orphans" in log.read_text()
    assert " -v " not in log.read_text()
    assert (release / "compose.json").exists()


def test_retained_release_protects_credential_generation_from_cleanup(tmp_path: Path) -> None:
    from coire_core.deploy import cleanup_unreferenced_secrets

    state = tmp_path / "state"
    base = tmp_path / "credentials"
    root = base / "coiretest/secrets"
    kept = stage_secret_generation(root, dict.fromkeys(REQUIRED_SECRET_FILES, "retained"))
    unused = stage_secret_generation(root, dict.fromkeys(REQUIRED_SECRET_FILES, "unused"))
    release = state / "coiretest/releases/release-one"
    release.mkdir(parents=True)
    (release / "compose.json").write_text(
        json.dumps({"secrets": {"postgres_password": {"file": str(kept / "postgres_password")}}})
    )
    assert (
        cleanup_unreferenced_secrets(state, base, "coiretest", mounted_generations=frozenset()) == 1
    )
    assert kept.is_dir() and (kept / "postgres_password").read_text() == "retained"
    assert not unused.exists()


def test_unknown_retained_manifest_refuses_cleanup_without_deleting_credentials(
    tmp_path: Path,
) -> None:
    from coire_core.deploy import cleanup_unreferenced_secrets

    state = tmp_path / "state"
    base = tmp_path / "credentials"
    kept = stage_secret_generation(
        base / "coiretest/secrets", dict.fromkeys(REQUIRED_SECRET_FILES, "retained")
    )
    release = state / "coiretest/releases/release-one"
    release.mkdir(parents=True)
    (release / "compose.json").write_text("corrupt")
    with pytest.raises(ReleaseInputError):
        cleanup_unreferenced_secrets(state, base, "coiretest", mounted_generations=frozenset())
    assert (kept / "postgres_password").read_text() == "retained"


def test_release_selection_is_atomic_and_project_owned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = runpy.run_path(str(Path(__file__).resolve().parents[2] / "scripts/coire-deploy.py"))
    select = script["select_release"]
    project = "coiretest"
    state = tmp_path / "state"
    first = state / project / "releases" / "release-one"
    second = state / project / "releases" / "release-two"
    foreign = state / "different" / "releases" / "release-foreign"
    for release in (first, second, foreign):
        release.mkdir(parents=True)
        (release / "compose.json").write_text("{}")
    monkeypatch.setenv("COMPOSE_PROJECT_NAME", project)
    monkeypatch.setenv("COIRE_STATE_ROOT", str(state))
    select(first)
    assert (state / project / "current").resolve() == first
    with pytest.raises(ReleaseInputError, match="not owned"):
        select(foreign)
    assert (state / project / "current").resolve() == first
    select(second)
    assert (state / project / "current").resolve() == second


def test_actual_project_container_mount_protects_its_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from coire_core.deploy import cleanup_unreferenced_secrets, mounted_secret_generations

    base = tmp_path / "credentials"
    generation = stage_secret_generation(
        base / "coiretest/secrets", dict.fromkeys(REQUIRED_SECRET_FILES, "mounted")
    )
    container = {
        "Id": "a" * 64,
        "Config": {"Labels": {"com.docker.compose.project": "coiretest"}},
        "Mounts": [{"Type": "bind", "Source": str(generation / "postgres_password")}],
    }

    def docker(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            argv, 0, "a" * 64 + "\n" if argv[1] == "ps" else json.dumps(container)
        )

    monkeypatch.setattr(subprocess, "run", docker)
    protected = mounted_secret_generations(base, "coiretest")
    assert protected == frozenset({generation.resolve()})
    assert (
        cleanup_unreferenced_secrets(
            tmp_path / "state", base, "coiretest", mounted_generations=protected
        )
        == 0
    )
    assert (generation / "postgres_password").read_text() == "mounted"
    container["Id"] = "b" * 64
    with pytest.raises(ReleaseInputError):
        mounted_secret_generations(base, "coiretest")


def test_upgrade_preparation_cli_publishes_only_verified_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import hashlib
    import sys

    from upgrade_fixtures import plan

    from coire_core.models.upgrades import UpgradeRelease, UpgradeReleaseRegister
    from coire_core.upgrade_artifacts import verify_prepared_release
    from coire_core.upgrade_journal import PrivateUpgradeStore

    initial = plan().target
    payload = b"trusted-wheel"
    manifest = initial.manifest.model_copy(
        update={
            "artifact_sha256s": {"wheel": hashlib.sha256(payload).hexdigest()},
            "staged_bytes": len(payload),
        }
    )
    release = initial.model_copy(
        update={"manifest": manifest, "manifest_sha256": manifest.sha256()}
    )
    document = PrivateUpgradeStore(tmp_path / "operator")
    document.write("release.json", release)
    artifact = tmp_path / "wheel"
    artifact.write_bytes(payload)
    catalog = tmp_path / "catalog"
    script = runpy.run_path(str(Path(__file__).resolve().parents[2] / "scripts/coire-deploy.py"))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "coire-deploy.py",
            "prepare-upgrade",
            str(catalog),
            "--release-document",
            str(document.path("release.json")),
            "--artifact",
            f"wheel={artifact}",
        ],
    )
    assert script["main"]() == 0
    request = UpgradeReleaseRegister.model_validate_json(capsys.readouterr().out)
    assert verify_prepared_release(catalog, request) == UpgradeRelease.model_validate(release)
    assert script["main"]() == 2
    assert "already exists" in capsys.readouterr().err
    assert verify_prepared_release(catalog, request) == release


@pytest.mark.parametrize("fault", [None, "platform", "user", "project", "receipt"])
def test_container_catalog_publication_has_no_network_or_host_path_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str | None,
) -> None:
    import hashlib
    import io
    import json
    import runpy
    import subprocess

    from upgrade_fixtures import core_plan

    from coire_core.models.upgrades import UpgradeReleaseRegister, UpgradeServiceSpec
    from coire_core.upgrade_artifacts import verify_prepared_release
    from coire_core.upgrade_publication import publish_stream

    original = core_plan().target
    configuration = {"image": original.manifest.services[0].image_id}
    service_spec = UpgradeServiceSpec(
        service="coire-api",
        image_id=configuration["image"],
        configuration=configuration,
        configuration_sha256=hashlib.sha256(
            json.dumps(configuration, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
    )
    payload = json.dumps({"name": "coire", "services": {"coire-api": configuration}}).encode()
    digest = hashlib.sha256(payload).hexdigest()
    manifest = original.manifest.model_copy(
        update={
            "services": (service_spec,),
            "compose_sha256": digest,
            "artifact_sha256s": {"compose.json": digest},
            "staged_bytes": len(payload),
        }
    )
    release = original.model_copy(
        update={"manifest": manifest, "manifest_sha256": manifest.sha256()}
    )
    artifact = tmp_path / "compose.json"
    artifact.write_bytes(payload)
    image_id = "sha256:" + "f" * 64
    calls: list[list[str]] = []

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        if argv[1:3] == ["image", "inspect"]:
            return subprocess.CompletedProcess(
                argv,
                0,
                json.dumps(
                    [
                        {
                            "Id": image_id,
                            "Os": "linux",
                            "Architecture": "amd64" if fault == "platform" else "arm64",
                            "Config": {"User": "0" if fault == "user" else "65532:65532"},
                        }
                    ]
                ),
                "",
            )
        if argv[1:3] == ["volume", "inspect"]:
            return subprocess.CompletedProcess(
                argv,
                0,
                json.dumps(
                    [
                        {
                            "Name": "coire_coire-upgrade-prepared",
                            "Labels": {
                                "com.docker.compose.project": "foreign"
                                if fault == "project"
                                else "coire"
                            },
                        }
                    ]
                ),
                "",
            )
        assert argv[1] == "run"
        assert "--network=none" in argv and "--read-only" in argv and "--pull=never" in argv
        assert "--cap-drop=ALL" in argv and "--security-opt=no-new-privileges:true" in argv
        assert argv[-3:] == [image_id, "-m", "coire_core.upgrade_publication"]
        assert (
            "type=volume,source=coire_coire-upgrade-prepared,target=/opt/coire/upgrades/prepared"
            in argv
        )
        assert all(str(tmp_path) not in value for value in argv)
        request = publish_stream(io.BytesIO(kwargs["input"].encode()), tmp_path / "service-catalog")
        if fault == "receipt":
            request = request.model_copy(update={"expected_sha256": "e" * 64})
        return subprocess.CompletedProcess(argv, 0, request.model_dump_json(), "")

    monkeypatch.setattr(subprocess, "run", run)
    script = runpy.run_path(str(Path(__file__).resolve().parents[2] / "scripts/coire-deploy.py"))
    if fault is None:
        result = script["publish_container_catalog"](
            release, {"compose.json": artifact}, image_id=image_id
        )
        assert isinstance(result, UpgradeReleaseRegister)
        assert verify_prepared_release(tmp_path / "service-catalog", result) == release
    else:
        with pytest.raises(ReleaseInputError):
            script["publish_container_catalog"](
                release, {"compose.json": artifact}, image_id=image_id
            )
        if fault != "receipt":
            assert len(calls) == 2
            assert not (tmp_path / "service-catalog").exists()


@pytest.mark.parametrize("fault", [None, "active", "hold", "missing"])
def test_backup_preparation_holds_shared_exclusion_without_changing_runtime_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys, fault: str | None
) -> None:
    import sys
    from datetime import UTC, datetime
    from unittest.mock import Mock

    from upgrade_fixtures import core_plan

    from coire_api.upgrades import backup_preparation
    from coire_core.deploy import host_boot_identity
    from coire_core.models.upgrades import DeploymentOperationHold, DeploymentOwnership
    from coire_core.upgrade_journal import PrivateUpgradeStore

    value = core_plan(schema_changed=True)
    root = tmp_path / "state"
    store = PrivateUpgradeStore(root / "coire/upgrades/coordination")
    owner = DeploymentOwnership(
        project="coire",
        owner_kind="host",
        owner_identity="host-source",
        boot_identity=host_boot_identity(),
        generation=1,
        state="active" if fault == "active" else "committed",
    )
    if fault != "missing":
        store.write("ownership.json", owner)
    if fault == "hold":
        store.write(
            "operation-hold.json",
            DeploymentOperationHold(
                project="coire",
                job_id=value.operation.job_id,
                generation=value.operation.generation,
                plan_sha256=value.authority_sha256(),
                source_sha256=value.operation.source_sha256,
                created_at=datetime.now(UTC),
            ),
        )

    def prepare(path, project):
        assert path == tmp_path / "backups" and project == "coire"
        assert store.path("deployment.lease").exists()
        assert store.read("ownership.json", DeploymentOwnership) == owner
        return value.backup

    prepared = Mock(side_effect=prepare)
    monkeypatch.setattr(backup_preparation, "prepare_backup", prepared)
    monkeypatch.setenv("COIRE_STATE_ROOT", str(root))
    monkeypatch.setenv("COMPOSE_PROJECT_NAME", "coire")
    monkeypatch.setattr(
        sys, "argv", ["coire-deploy.py", "prepare-backup", str(tmp_path / "backups")]
    )
    script = runpy.run_path(str(Path(__file__).resolve().parents[2] / "scripts/coire-deploy.py"))
    assert script["main"]() == (0 if fault is None else 2)
    assert not store.path("deployment.lease").exists()
    if fault is None:
        assert json.loads(capsys.readouterr().out) == value.backup.model_dump(mode="json")
        prepared.assert_called_once()
    else:
        prepared.assert_not_called()
    assert store.read("ownership.json", DeploymentOwnership) == (
        None if fault == "missing" else owner
    )


@pytest.mark.parametrize("fault", [None, "bytes", "missing", "linked", "key"])
def test_core_preparation_binds_exact_retained_host_compose(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str | None
) -> None:
    import hashlib

    from upgrade_fixtures import core_plan

    value = core_plan()
    state_root = tmp_path / "state"
    release_dir = state_root / "coire/releases/core-source"
    release_dir.mkdir(mode=0o700, parents=True)
    payload = b'{"name":"coire","services":{}}'
    compose = release_dir / "compose.json"
    compose.write_bytes(b"changed" if fault == "bytes" else payload)
    compose.chmod(0o600)
    if fault == "missing":
        compose.unlink()
    elif fault == "linked":
        retained = release_dir / "linked.json"
        compose.rename(retained)
        compose.symlink_to(retained)
    manifest = value.source.manifest.model_copy(
        update={
            "compose_sha256": hashlib.sha256(payload).hexdigest(),
            "host_release_key": None if fault == "key" else "core-source",
        }
    )
    release = value.source.model_copy(
        update={"manifest": manifest, "manifest_sha256": manifest.sha256()}
    )
    monkeypatch.setenv("COIRE_STATE_ROOT", str(state_root))
    script = runpy.run_path(str(Path(__file__).resolve().parents[2] / "scripts/coire-deploy.py"))
    if fault is None:
        script["verify_host_release_binding"](release)
    else:
        with pytest.raises(ReleaseInputError):
            script["verify_host_release_binding"](release)


@pytest.mark.parametrize("fault", [None, "state_project", "receipt", "missing"])
def test_bootstrap_publisher_has_no_network_or_coordination_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str | None
) -> None:
    import runpy

    from upgrade_bootstrap_fixtures import prepared_bootstrap

    seed, _ = prepared_bootstrap()
    calls: list[list[str]] = []

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        if argv[1:3] == ["image", "inspect"]:
            return subprocess.CompletedProcess(
                argv,
                0,
                json.dumps(
                    [
                        {
                            "Id": seed.executor_image_id,
                            "Os": "linux",
                            "Architecture": "arm64",
                            "Config": {"User": "65532:65532"},
                        }
                    ]
                ),
                "",
            )
        if argv[1:3] == ["volume", "inspect"]:
            state = argv[3].endswith("state")
            return subprocess.CompletedProcess(
                argv,
                1 if state and fault == "missing" else 0,
                json.dumps(
                    [
                        {
                            "Name": argv[3],
                            "Labels": {
                                "com.docker.compose.project": "foreign"
                                if state and fault == "state_project"
                                else "coire"
                            },
                        }
                    ]
                ),
                "",
            )
        assert argv[1] == "run"
        assert "--network=none" in argv and "--read-only" in argv
        assert "--pull=never" in argv and "--cap-drop=ALL" in argv
        assert "--security-opt=no-new-privileges:true" in argv
        assert "UPGRADE_PROJECT=coire" in argv
        assert (
            "type=volume,source=coire_coire-upgrade-prepared,target=/opt/coire/upgrades/prepared,readonly"
            in argv
        )
        assert (
            "type=volume,source=coire_coire-upgrade-state,target=/opt/coire/upgrades/state" in argv
        )
        assert not any(
            "coordination" in item or "docker.sock" in item or str(tmp_path) in item
            for item in argv
        )
        assert argv[-3:] == [
            seed.executor_image_id,
            "-m",
            "coire_core.upgrade_bootstrap_publication",
        ]
        assert kwargs["input"] == seed.model_dump_json()
        receipt = (
            seed.model_copy(update={"generation": seed.generation + 1})
            if fault == "receipt"
            else seed
        )
        return subprocess.CompletedProcess(argv, 0, receipt.model_dump_json(), "")

    monkeypatch.setenv("COMPOSE_PROJECT_NAME", "coire")
    monkeypatch.setattr(subprocess, "run", run)
    script = runpy.run_path(str(Path(__file__).resolve().parents[2] / "scripts/coire-deploy.py"))
    if fault:
        with pytest.raises(ReleaseInputError):
            script["publish_bootstrap_intent"](seed)
        if fault != "receipt":
            assert not any(argv[1] == "run" for argv in calls)
    else:
        assert script["publish_bootstrap_intent"](seed) == seed
