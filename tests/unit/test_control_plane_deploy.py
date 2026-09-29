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
        "ops_service_token",
        "file_worker_service_token",
        "failover_peer_key",
        "failover_relay_token",
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
        image_ids={"coire-api": "sha256:immutable"},
    )
    assert release["services"]["coire-api"]["image"] == "sha256:immutable"
    assert "build" not in release["services"]["coire-api"]
    assert release["services"]["coire-api"]["volumes"][0]["source"] == str(tmp_path / "cluster")
    assert release["services"]["coire-api"]["volumes"][0]["bind"]["create_host_path"] is False
    assert release["secrets"]["postgres_password"]["file"] == str(
        tmp_path / "secrets/postgres_password"
    )
    assert source["services"]["coire-api"]["image"] == "coire-api:dev"


def test_release_manifest_requires_every_local_image(tmp_path: Path) -> None:
    with pytest.raises(ReleaseInputError, match="missing image ID"):
        release_manifest(
            {"services": {"coire-api": {"image": "coire-api:dev"}}, "secrets": {}},
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
