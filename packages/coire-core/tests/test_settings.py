"""Unit tests for settings and secret sourcing (T011)."""

from __future__ import annotations

from pathlib import Path

from coire_core.settings import Settings


def _settings(tmp_path: Path, **files: str) -> Settings:
    for name, value in files.items():
        (tmp_path / name).write_text(value)
    return Settings(_secrets_dir=str(tmp_path))  # type: ignore[call-arg]


def test_secrets_are_read_from_files_not_environment(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """A secret in the environment must not win over the mounted file."""
    monkeypatch.setenv("POSTGRES_PASSWORD", "from-env")
    settings = _settings(tmp_path, postgres_password="from-file")
    assert settings.postgres_password.get_secret_value() == "from-file"


def test_database_url_is_assembled_from_parts(tmp_path: Path) -> None:
    settings = _settings(tmp_path, postgres_password="pw")
    assert settings.database_url == "postgresql+asyncpg://coire:pw@postgres:5432/coire"


def test_secret_absent_yields_empty_not_error(tmp_path: Path) -> None:
    """Services must import cleanly without secrets; bring-up is what enforces their presence."""
    assert Settings(_secrets_dir=str(tmp_path)).postgres_password.get_secret_value() == ""  # type: ignore[call-arg]


def test_node_token_map_parses_json(tmp_path: Path) -> None:
    settings = _settings(tmp_path, node_tokens='{"coire-edge-a": "aaa", "coire-edge-b": "bbb"}')
    assert settings.node_token_map == {"coire-edge-a": "aaa", "coire-edge-b": "bbb"}


def test_node_token_map_tolerates_malformed_json(tmp_path: Path) -> None:
    """A malformed token file must not crash the API at import; registration simply refuses."""
    assert _settings(tmp_path, node_tokens="not json").node_token_map == {}
    assert _settings(tmp_path, node_tokens="[1,2]").node_token_map == {}


def test_budget_defaults_match_the_spec(tmp_path: Path) -> None:
    settings = Settings(_secrets_dir=str(tmp_path))  # type: ignore[call-arg]
    assert settings.node_collection_budget_cpu_pct == 2.0
    assert settings.node_collection_budget_rss_bytes == 150 * 1024 * 1024
    assert settings.node_probe_interval_s == 10.0


def test_password_is_not_in_repr(tmp_path: Path) -> None:
    assert "pw" not in repr(_settings(tmp_path, postgres_password="pw"))
