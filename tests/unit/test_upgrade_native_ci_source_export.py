"""Reviewed source transfer excludes acquisition assets and checkout credentials."""

import subprocess
import tarfile
from pathlib import Path

import pytest
from integration.bootstrap_upgrade_ci_vm import export_source, source_files


def _repo(root: Path) -> None:
    subprocess.run(["git", "init", "--quiet", "--initial-branch=fixture", str(root)], check=True)
    files = {
        ".gitignore": ".env*\n/models/\n/data/\n/blobs/\n",
        "packages/coire-core/src/coire_core/models/contract.py": "contract = 'required schema source'\n",
        "apps/service.py": "service = 'reviewed source'\n",
        ".env.local": "test_only_secret=never_export\n",
        "models/weights.bin": "test-only acquisition asset\n",
        "data/feedback.jsonl": "test-only private contribution\n",
    }
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@isolated.test",
            "commit",
            "--quiet",
            "-m",
            "reviewed fixture",
        ],
        cwd=root,
        check=True,
    )
    subprocess.run(
        [
            "git",
            "config",
            "http.https://github.com/.extraheader",
            "AUTHORIZATION: bearer test-only-checkout-credential",
        ],
        cwd=root,
        check=True,
    )


def test_bundle_and_worktree_export_preserve_schema_without_credentials(tmp_path: Path) -> None:
    repo, destination = tmp_path / "repo", tmp_path / "private"
    _repo(repo)
    destination.mkdir(mode=0o700)
    changed = repo / "apps/service.py"
    changed.write_text("service = 'current reviewed worktree bytes'\n")
    bundle, archive = export_source(repo, destination)
    with tarfile.open(archive) as contents:
        names = contents.getnames()
        assert "packages/coire-core/src/coire_core/models/contract.py" in names
        member = contents.extractfile("apps/service.py")
        assert member is not None and member.read() == changed.read_bytes()
        assert not any(name.startswith((".git/", "models/", "data/", ".env")) for name in names)
    for path in (bundle, archive):
        assert path.stat().st_mode & 0o077 == 0
        assert b"test-only-checkout-credential" not in path.read_bytes()
        assert b"test_only_secret" not in path.read_bytes()
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "--quiet", str(bundle), str(clone)], check=True)
    assert "test-only-checkout-credential" not in (clone / ".git/config").read_text()


def test_source_export_rejects_linked_foreign_source(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _repo(repo)
    foreign = tmp_path / "operator.txt"
    foreign.write_text("foreign fixture bytes")
    (repo / "apps/foreign.py").symlink_to(foreign)
    with pytest.raises(RuntimeError, match="linked or foreign"):
        source_files(repo)
