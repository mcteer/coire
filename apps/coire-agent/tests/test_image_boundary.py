from __future__ import annotations

from pathlib import Path


def test_user_distribution_excludes_admin_client() -> None:
    package = Path(__file__).parents[1] / "src" / "coire_agent"
    assert not (package / "admin_client.py").exists()
    dockerfile = (Path(__file__).parents[1] / "Dockerfile").read_text()
    assert "coire_ops" not in dockerfile


def test_ops_distribution_contains_admin_client_and_distinct_entrypoint() -> None:
    root = Path(__file__).parents[1]
    assert (root / "ops" / "coire_ops" / "admin_client.py").is_file()
    assert 'CMD ["-m", "coire_ops"]' in (root / "ops.Dockerfile").read_text()


def test_ops_image_removes_user_harness_and_has_no_debug_entrypoint() -> None:
    root = Path(__file__).parents[1]
    dockerfile = (root / "ops.Dockerfile").read_text()
    assert "site-packages/coire_agent" in dockerfile
    assert "rm -rf /app/.venv/lib/python3.13/site-packages/coire_agent" in dockerfile
    final_stage = dockerfile.split("FROM gcr.io/distroless", maxsplit=1)[1]
    for forbidden in ("apt-get", "curl", "git", "bash", "/bin/sh", "docker"):
        assert forbidden not in final_stage
    assert "USER 65532:65532" in final_stage


def test_ops_package_contains_only_the_reviewed_runtime_modules() -> None:
    package = Path(__file__).parents[1] / "ops" / "coire_ops"
    assert {path.name for path in package.glob("*.py")} == {
        "__init__.py",
        "__main__.py",
        "admin_client.py",
        "app.py",
        "model.py",
        "service.py",
    }
