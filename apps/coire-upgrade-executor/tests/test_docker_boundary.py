"""The executor refuses Docker paths outside its fixed accepted read surface."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from upgrade_fixtures import core_plan

from coire_core.errors import UpgradeForbidden
from coire_core.settings import Settings


@pytest.mark.parametrize("status", [200, 404, 503])
async def test_only_explicit_not_found_proves_candidate_absence(status: int) -> None:
    from coire_core.errors import UpgradeUnavailable
    from coire_upgrade_executor.docker import read_docker_object

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(status, json={})),
        trust_env=False,
    ) as client:
        if status == 404:
            assert not await read_docker_object(
                client,
                httpx.URL("http://docker-socket-proxy:2375"),
                "/containers/accepted/json",
                allow_absent=True,
            )
        else:
            with pytest.raises(UpgradeUnavailable):
                await read_docker_object(
                    client,
                    httpx.URL("http://docker-socket-proxy:2375"),
                    "/containers/accepted/json",
                    allow_absent=True,
                )


@pytest.mark.parametrize(
    "method,path",
    [
        ("POST", "/containers/create"),
        ("POST", "/containers/abc/exec"),
        ("POST", "/exec/abc/start"),
        ("POST", "/images/create"),
        ("POST", "/build"),
        ("POST", "/containers/prune"),
        ("GET", "/volumes"),
        ("POST", "/networks/create"),
        ("DELETE", "/containers/" + "a" * 64),
        ("GET", "/containers/../images/json"),
        ("GET", "http://foreign/containers/json"),
    ],
)
async def test_hostile_paths_never_reach_the_proxy(
    method: str, path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from coire_upgrade_executor.docker import UpgradeDocker

    http = httpx.AsyncClient(trust_env=False, follow_redirects=False)
    docker = UpgradeDocker(Settings(), core_plan(), client=http)
    send = AsyncMock()
    monkeypatch.setattr(http, "send", send)
    try:
        with pytest.raises(UpgradeForbidden):
            await docker._read(method, path)
        send.assert_not_awaited()
    finally:
        await http.aclose()


async def test_image_inspection_binds_exact_digest_and_platform() -> None:
    from coire_upgrade_executor.docker import UpgradeDocker

    value = core_plan()
    image = value.target.manifest.services[0].image_id
    calls: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"Id": image, "Os": "linux", "Architecture": "arm64"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle), trust_env=False, follow_redirects=False
    ) as http:
        docker = UpgradeDocker(Settings(), value, client=http)
        observed = await docker.inspect_image(image)
        assert observed.image_id == image
        assert str(calls[0].url) == f"http://docker-socket-proxy:2375/v1.46/images/{image}/json"
        with pytest.raises(UpgradeForbidden):
            await docker.inspect_image("sha256:" + "f" * 64)
        assert len(calls) == 1


@pytest.mark.parametrize(
    "fault", ["foreign", "platform", "redirect", "oversized", "duplicate", "nonfinite"]
)
async def test_untrusted_image_responses_cannot_qualify(fault: str) -> None:
    from coire_core.errors import UpgradeConflict, UpgradeUnavailable
    from coire_upgrade_executor.docker import UpgradeDocker

    value = core_plan()
    image = value.target.manifest.services[0].image_id

    def handle(request: httpx.Request) -> httpx.Response:
        if fault == "redirect":
            return httpx.Response(302, headers={"Location": "http://foreign/private"})
        if fault == "oversized":
            return httpx.Response(200, content=b"private-data" * 100_000)
        if fault == "duplicate":
            return httpx.Response(200, content=b'{"Id":"private","Id":"other"}')
        if fault == "nonfinite":
            return httpx.Response(200, content=b'{"Id":NaN}')
        return httpx.Response(
            200,
            json={
                "Id": "sha256:" + "f" * 64 if fault == "foreign" else image,
                "Os": "linux",
                "Architecture": "amd64" if fault == "platform" else "arm64",
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle), trust_env=False, follow_redirects=False
    ) as http:
        docker = UpgradeDocker(Settings(), value, client=http)
        with pytest.raises((UpgradeConflict, UpgradeUnavailable)) as caught:
            await docker.inspect_image(image)
        assert "private-data" not in str(caught.value)
        assert "foreign/private" not in str(caught.value)


@pytest.mark.parametrize("fault", [None, "project", "role", "image", "name", "network", "mount"])
async def test_source_container_observation_requires_exact_fixed_owner(
    fault: str | None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from coire_core.errors import UpgradeConflict
    from coire_upgrade_executor.docker import UpgradeDocker

    value = core_plan()
    service = value.source.manifest.services[0]
    name = f"coire-{service.service}-1"
    labels = {
        "com.docker.compose.project": "other" if fault == "project" else "coire",
        "com.docker.compose.service": "other" if fault == "role" else service.service,
        "com.docker.compose.oneoff": "False",
    }
    body = {
        "Id": "c" * 64,
        "Name": "/other" if fault == "name" else "/" + name,
        "Image": "sha256:" + "f" * 64 if fault == "image" else service.image_id,
        "Config": {"Labels": labels},
        "State": {"Running": True, "ExitCode": 0, "Health": {"Status": "healthy"}},
        "NetworkSettings": {
            "Networks": {
                "coire-internal": {"NetworkID": "invalid" if fault == "network" else "b" * 64}
            }
        },
        "Mounts": [{"Type": "volume", "Name": "foreign"}] if fault == "mount" else [],
    }
    calls: list[httpx.Request] = []
    started = datetime.now(UTC)
    clock = Mock(wraps=datetime)
    clock.now.return_value = started
    monkeypatch.setattr("coire_upgrade_executor.docker.datetime", clock)

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        clock.now.return_value = started + timedelta(seconds=6)
        return httpx.Response(200, json=body)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle), trust_env=False, follow_redirects=False
    ) as http:
        docker = UpgradeDocker(Settings(), value, client=http)
        if fault is None:
            observed = await docker.inspect_source(name)
            assert observed.container_id == "c" * 64
            assert observed.health == "healthy"
            assert observed.mounts == ()
            assert observed.observed_at == started
            from coire_upgrade_executor.journal import ExecutorJournal

            journal = ExecutorJournal(tmp_path / "journal", value)
            with pytest.raises(UpgradeConflict, match="stale"):
                journal.freeze_source((observed,), now=started + timedelta(seconds=6))
        else:
            with pytest.raises(UpgradeConflict):
                await docker.inspect_source(name)
        assert len(calls) == 1
        with pytest.raises(UpgradeForbidden):
            await docker.inspect_source("coire-postgres-1")
        assert len(calls) == 1


async def test_image_defaults_are_read_only_from_the_exact_present_image() -> None:
    from coire_upgrade_executor.docker import UpgradeDocker

    value = core_plan()
    image = value.source.manifest.services[0].image_id

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET" and request.url.path == f"/v1.46/images/{image}/json"
        return httpx.Response(
            200,
            json={
                "Id": image,
                "Os": "linux",
                "Architecture": "arm64",
                "Config": {
                    "Env": ["PATH=/usr/bin"],
                    "Entrypoint": None,
                    "Cmd": ["python", "-m", "coire_api"],
                    "WorkingDir": "/app",
                    "ExposedPorts": {"8000/tcp": {}},
                },
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle), trust_env=False, follow_redirects=False
    ) as http:
        docker = UpgradeDocker(Settings(), value, client=http)
        defaults = await docker.inspect_image_defaults(image)
        assert defaults.command == ("python", "-m", "coire_api")
        assert defaults.environment == ("PATH=/usr/bin",)
        assert defaults.exposed_ports == ("8000/tcp",)
