"""Remove a completed private fixture, including stopped upgrade containers and volumes."""

from __future__ import annotations

import json
import re
import subprocess
from typing import Any, cast


def _docker(*args: str) -> str:
    return subprocess.check_output(["docker", *args], text=True, timeout=90)


def retire_completed_project(project: str, *, terminal: bool) -> dict[str, int]:
    """Call only after terminal operations and native/coordinator shutdown.

    Failed or unresolved fixtures must use their recovery workflow first. Retained
    redacted results and host journals are outside Docker; successful test volumes
    are disposable. Never force-remove a running container or an in-use volume.
    """
    if (
        not terminal
        or re.fullmatch(r"coire-020-(?:application|cohort)-[a-f0-9]{10}", project) is None
    ):
        raise ValueError("cleanup requires a completed private 020 fixture")
    query = ("ps", "-aq", "--no-trunc", "--filter", "label=com.docker.compose.project=" + project)
    identities = _docker(*query).split()
    if identities:
        records = cast(list[dict[str, Any]], json.loads(_docker("inspect", *identities)))
        assert {item["Id"] for item in records} == set(identities)
        assert all(
            item["Config"]["Labels"].get("com.docker.compose.project") == project
            and not item["State"]["Running"]
            for item in records
        ), "fixture still has a live or foreign container"
        _docker("rm", *identities)
    assert not _docker(*query).split(), "fixture containers remain"
    volumes = _docker(
        "volume", "ls", "-q", "--filter", "label=com.docker.compose.project=" + project
    ).split()
    if volumes:
        records = json.loads(_docker("volume", "inspect", *volumes))
        assert {item["Name"] for item in records} == set(volumes)
        assert all(
            item["Labels"].get("com.docker.compose.project") == project
            and item["Name"].startswith(project + "_")
            for item in records
        ), "fixture volume ownership differs"
        _docker("volume", "rm", *volumes)
    assert not _docker(
        "volume", "ls", "-q", "--filter", "label=com.docker.compose.project=" + project
    ).split(), "fixture volumes remain"
    return {"removed_containers": len(identities), "removed_volumes": len(volumes)}
