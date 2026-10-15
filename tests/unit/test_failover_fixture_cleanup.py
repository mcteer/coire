"""Fixture retirement refuses live/foreign resources and removes stopped upgrade debris."""

import importlib.util
import json
import subprocess
from pathlib import Path
from types import ModuleType

import pytest


@pytest.fixture
def cleanup() -> ModuleType:
    path = (
        Path(__file__).resolve().parents[2]
        / "specs/020-control-plane-failover/validation/fixture_cleanup.py"
    )
    spec = importlib.util.spec_from_file_location("failover_fixture_cleanup", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "project,terminal", [("coire", True), ("coire-020-application-aabbccddee", False)]
)
def test_never_targets_production_or_unfinished_work(cleanup, monkeypatch, project, terminal):
    def refuse(*args):
        raise AssertionError("invalid cleanup reached Docker")

    monkeypatch.setattr(cleanup, "_docker", refuse)
    with pytest.raises(ValueError):
        cleanup.retire_completed_project(project, terminal=terminal)


@pytest.mark.parametrize(
    "fault", [None, "running", "foreign_container", "foreign_volume", "name", "busy"]
)
def test_retirement_checks_ownership_and_all_container_states(cleanup, monkeypatch, fault):
    project = "coire-020-application-aabbccddee"
    volume = project + "_coire-pgdata"
    removed = {"container": False, "volume": False}
    calls = []

    def docker(*args):
        calls.append(args)
        if args[0] == "ps":
            assert "-aq" in args  # Running-only checks miss replaced Source containers.
            return "" if removed["container"] else "retired-api\n"
        if args[0] == "inspect":
            return json.dumps(
                [
                    {
                        "Id": "retired-api",
                        "Config": {
                            "Labels": {
                                "com.docker.compose.project": "coire"
                                if fault == "foreign_container"
                                else project,
                            }
                        },
                        "State": {"Running": fault == "running"},
                    }
                ]
            )
        if args[0] == "rm":
            assert args == ("rm", "retired-api")
            removed["container"] = True
            return ""
        if args[:2] == ("volume", "ls"):
            return "" if removed["volume"] else volume + "\n"
        if args[:2] == ("volume", "inspect"):
            return json.dumps(
                [
                    {
                        "Name": volume,
                        "Labels": {
                            "com.docker.compose.project": "coire"
                            if fault == "foreign_volume"
                            else project,
                        },
                    }
                ]
            )
        if args[:2] == ("volume", "rm"):
            assert args == ("volume", "rm", volume)
            if fault == "busy":
                raise subprocess.CalledProcessError(1, ["docker", *args])
            removed["volume"] = True
            return ""
        raise AssertionError(args)

    if fault == "name":
        volume = "unrelated-volume"
    monkeypatch.setattr(cleanup, "_docker", docker)
    if fault:
        expected = subprocess.CalledProcessError if fault == "busy" else AssertionError
        with pytest.raises(expected):
            cleanup.retire_completed_project(project, terminal=True)
        assert not removed["volume"]
        if fault in {"running", "foreign_container"}:
            assert not removed["container"]
    else:
        assert cleanup.retire_completed_project(project, terminal=True) == {
            "removed_containers": 1,
            "removed_volumes": 1,
        }
        assert all(removed.values())
    assert not any("--force" in call or "-f" in call for call in calls)
