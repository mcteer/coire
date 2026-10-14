"""Private input/evidence boundaries for real native workload acceptance."""

import json
from pathlib import Path

import httpx
import pytest
from integration.test_upgrade_native_workloads import _request_bytes, _save, _work_settled


def test_private_bounded_request_is_read_without_mutation(tmp_path: Path) -> None:
    path = tmp_path / "request.json"
    path.write_bytes(b'{"model_id":"prepared-registry-id"}')
    path.chmod(0o600)
    assert _request_bytes(path) == path.read_bytes()
    assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("kind", ["public", "linked", "hardlinked", "oversized", "directory"])
def test_untrusted_request_fails_before_workload_submission(tmp_path: Path, kind: str) -> None:
    path = tmp_path / "request.json"
    if kind == "directory":
        path.mkdir(mode=0o700)
    else:
        path.write_bytes(b"{}")
        path.chmod(0o600)
        if kind == "public":
            path.chmod(0o644)
        elif kind == "linked":
            target = tmp_path / "target.json"
            path.rename(target)
            path.symlink_to(target)
        elif kind == "hardlinked":
            (tmp_path / "alias.json").hardlink_to(path)
        elif kind == "oversized":
            path.write_bytes(b"x" * (64 * 1024 + 1))
    with pytest.raises(RuntimeError, match="bounded, private and owned"):
        _request_bytes(path)


def test_evidence_is_private_and_cannot_overwrite_existing_authority(tmp_path: Path) -> None:
    body = {"normal_completion": True, "positive_censuses": 19}
    _save(tmp_path, "result.json", body)
    path = tmp_path / "result.json"
    assert path.stat().st_mode & 0o777 == 0o600
    assert json.loads(path.read_bytes()) == body
    with pytest.raises(FileExistsError):
        _save(tmp_path, "result.json", {"normal_completion": False})
    assert json.loads(path.read_bytes()) == body


@pytest.mark.parametrize(
    ("state", "settled"),
    [
        ("succeeded", True),
        ("failed", True),
        ("cancelled", True),
        ("killed", True),
        ("timed_out", True),
        ("running", False),
        ("queued", False),
    ],
)
def test_cleanup_requires_terminal_work_under_original_initiating_key(
    state: str,
    settled: bool,
) -> None:
    def fetch(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/images/work"
        assert request.headers["Authorization"] == "Bearer initiating-key"
        return httpx.Response(200, json={"state": state})

    with httpx.Client(
        base_url="http://fixture.test",
        headers={"Authorization": "Bearer admin-key"},
        transport=httpx.MockTransport(fetch),
    ) as client:
        assert _work_settled(client, "/api/v1/images", "work", "initiating-key") is settled


@pytest.mark.parametrize("failure", ["timeout", "unavailable", "forbidden"])
def test_unknown_cleanup_observation_keeps_initiating_authority(failure: str) -> None:
    def fetch(request: httpx.Request) -> httpx.Response:
        if failure == "timeout":
            raise httpx.ReadTimeout("observation lost", request=request)
        return httpx.Response(503 if failure == "unavailable" else 403, json={})

    with httpx.Client(
        base_url="http://fixture.test", transport=httpx.MockTransport(fetch)
    ) as client:
        assert not _work_settled(client, "/api/v1/images", "work", "initiating-key")


def test_evidence_never_follows_a_link(tmp_path: Path) -> None:
    target = tmp_path / "authority.json"
    target.write_bytes(b"preserve")
    (tmp_path / "result.json").symlink_to(target)
    with pytest.raises(FileExistsError):
        _save(tmp_path, "result.json", {})
    assert target.read_bytes() == b"preserve"
