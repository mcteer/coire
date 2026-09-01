"""pin-images.sh behaviour (T043)."""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts/pin-images.sh"


def run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([str(SCRIPT), *args], capture_output=True, text=True, cwd=REPO)


def test_repository_is_fully_pinned() -> None:
    """The real tree passes with digest-pinned images and Docker's reserved scratch base."""
    proc = run("--check")
    assert proc.returncode == 0, proc.stderr


def test_check_rejects_an_unpinned_from(tmp_path: Path) -> None:
    """A Dockerfile with a bare tag must fail rule 7."""
    bad = REPO / "apps/coire-api/docker/.tmp-unpinned.Dockerfile"
    bad.write_text('FROM alpine:3.20\nENTRYPOINT ["/bin/true"]\n')
    try:
        proc = run("--check")
        assert proc.returncode != 0
        assert "unpinned FROM" in proc.stderr
    finally:
        bad.unlink()


def test_fixtures_are_exempt_from_the_lock() -> None:
    """Test fixtures are pinned but never deployed, so they need no images.lock entry."""
    assert (REPO / "tests/fixtures/policy/bad.Dockerfile").read_text().count("@sha256:") == 2
    assert run("--check").returncode == 0


def test_copy_from_digest_is_not_required_in_deployment_lock() -> None:
    """A pinned build-stage source is not a deployed base image and needs no lock entry."""
    dockerfile = REPO / "apps/coire-api/docker/.tmp-copy-source.Dockerfile"
    dockerfile.write_text(
        "FROM gcr.io/distroless/base-debian12:nonroot@sha256:"
        "7f0c72cd138b442ae0deeb69c08b1acf5525439ba251a49ad93c320a061567e5\n"
        "COPY --from=docker.io/library/busybox:1.36-musl@sha256:"
        "3c6ae8008e2c2eedd141725c30b20d9c36b026eb796688f88205845ef17aa213 "
        "/bin/sh /bin/sh\n"
        'ENTRYPOINT ["/bin/true"]\n'
    )
    try:
        proc = run("--check")
        assert proc.returncode == 0, proc.stderr
    finally:
        dockerfile.unlink()
