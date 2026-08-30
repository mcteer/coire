"""Platform settings.

Secrets are read from files under `/run/secrets/` — never from environment variables — because
the constitution requires file-mounted secrets and `coire-up` passes them to compose as
environment-sourced secrets that Docker materialises as files (research R4).
"""

from __future__ import annotations

import json
from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

DEFAULT_SECRETS_DIR = "/run/secrets"


class Settings(BaseSettings):
    """Configuration for every first-party service.

    Field names match the compose secret names exactly, so `postgres_password` is read from
    `/run/secrets/postgres_password`.
    """

    model_config = SettingsConfigDict(
        secrets_dir=DEFAULT_SECRETS_DIR,
        extra="ignore",
        case_sensitive=False,
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """File-mounted secrets outrank environment variables.

        pydantic-settings ranks env above `secrets_dir` by default. The constitution requires
        secrets to arrive as mounted files, so an environment variable must never be able to
        substitute for one — otherwise a leaked or inherited env var silently wins over the
        Keychain-sourced value.
        """
        return (init_settings, file_secret_settings, env_settings, dotenv_settings)

    # --- database -------------------------------------------------------
    postgres_host: str = "postgres"
    postgres_port: int = 5432
    postgres_user: str = "coire"
    postgres_db: str = "coire"
    postgres_password: SecretStr = SecretStr("")

    # --- credentials (declared now; used from feature 007) --------------
    key_signing_secret: SecretStr = SecretStr("")
    node_tokens: SecretStr = SecretStr("{}")
    """JSON object mapping node name to its static token. Replaced by issued tokens in 005."""

    # --- telemetry ------------------------------------------------------
    otlp_endpoint: str = "http://otel-collector:4317"
    service_version: str = "0.1.0"

    # --- node probing ---------------------------------------------------
    mesh_hosts_file: str = "/etc/hosts"
    node_probe_interval_s: float = 10.0
    node_probe_failures_before_unreachable: int = 3
    node_collection_budget_cpu_pct: float = 2.0
    node_collection_budget_rss_bytes: int = 150 * 1024 * 1024
    node_inventory_file: str = "/app/nodes.yaml"

    # --- node agent only ------------------------------------------------
    node_name: str = ""
    node_token: SecretStr = SecretStr("")
    node_listen_port: int = 9400
    core_mesh_host: str = "coire-core"

    @property
    def database_url(self) -> str:
        """Async SQLAlchemy URL. The password is only materialised here."""
        pw = self.postgres_password.get_secret_value()
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{pw}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def node_token_map(self) -> dict[str, str]:
        """Parsed `node_tokens`. Returns an empty map rather than raising on malformed JSON."""
        raw = self.node_tokens.get_secret_value().strip() or "{}"
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        if not isinstance(parsed, dict):
            return {}
        return {str(k): str(v) for k, v in parsed.items()}


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings. Cached so secret files are read once."""
    return Settings()
