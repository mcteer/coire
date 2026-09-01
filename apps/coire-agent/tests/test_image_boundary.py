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
