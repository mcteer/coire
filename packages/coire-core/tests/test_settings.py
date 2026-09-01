"""Unit tests for settings and secret sourcing (T011)."""

from __future__ import annotations

from pathlib import Path

import pytest

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


def test_admin_token_defaults_to_empty(tmp_path: Path) -> None:
    """An unset admin secret must make nobody an admin, never everybody (ADR-0004)."""
    assert Settings(_secrets_dir=str(tmp_path)).admin_token.get_secret_value() == ""  # type: ignore[call-arg]


def test_admin_token_is_read_from_the_mounted_file(tmp_path: Path) -> None:
    assert _settings(tmp_path, admin_token="sekrit").admin_token.get_secret_value() == "sekrit"


def test_admin_token_is_not_in_repr(tmp_path: Path) -> None:
    assert "sekrit" not in repr(_settings(tmp_path, admin_token="sekrit"))


def test_identity_defaults_are_fail_closed(tmp_path: Path) -> None:
    settings = Settings(_secrets_dir=str(tmp_path))  # type: ignore[call-arg]
    assert settings.cloudflare_access_issuer == ""
    assert settings.cloudflare_access_audience == ""
    assert settings.bootstrap_admin_email.get_secret_value() == ""
    assert settings.cloudflare_jwks_ttl_s == 300.0
    assert settings.cloudflare_jwt_leeway_s == 60.0


def test_bootstrap_admin_email_is_file_sourced_and_redacted(tmp_path: Path) -> None:
    settings = _settings(tmp_path, bootstrap_admin_email="admin@example.test")
    assert settings.bootstrap_admin_email.get_secret_value() == "admin@example.test"
    assert "admin@example.test" not in repr(settings)


def test_engine_port_range_parses(tmp_path: Path) -> None:
    assert Settings(_secrets_dir=str(tmp_path)).engine_port_range == (9500, 9599)  # type: ignore[call-arg]


def test_engine_port_range_rejects_malformed_values(tmp_path: Path) -> None:
    """A bad range must fail loudly here, not as a puzzling bind error at load time."""
    for bad in ("9500", "abc-def", "9600-9500", "0-10", "1-70000"):
        settings = Settings(_secrets_dir=str(tmp_path), node_engine_port_range=bad)  # type: ignore[call-arg]
        with pytest.raises(ValueError):
            _ = settings.engine_port_range


def test_overhead_falls_back_by_bit_width_then_other(tmp_path: Path) -> None:
    settings = Settings(_secrets_dir=str(tmp_path))  # type: ignore[call-arg]
    assert settings.overhead_for("8bit") == 1.08
    assert settings.overhead_for("4bit-g64") == 1.10  # group-size suffix stripped
    assert settings.overhead_for("something-odd") == 1.15


def test_registry_defaults_match_the_plan(tmp_path: Path) -> None:
    settings = Settings(_secrets_dir=str(tmp_path))  # type: ignore[call-arg]
    assert settings.node_memory_budget_fraction == 0.90
    assert settings.node_engine_health_interval_s == 5.0
    assert settings.kv_headroom_tokens == 32_768
    assert settings.disk_reserve_bytes == 50 * 1024**3
