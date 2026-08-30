"""Platform settings.

Secrets are read from files under `/run/secrets/` — never from environment variables — because
the constitution requires file-mounted secrets and `coire-up` passes them to compose as
environment-sourced secrets that Docker materialises as files (research R4).
"""

from __future__ import annotations

import json
from functools import lru_cache

from pydantic import Field, SecretStr
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
        # The node agent fills node_token and hf_token from the System keychain after
        # construction (coire_node.keychain), because a keychain is not a settings source.
        validate_assignment=True,
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

    admin_token: SecretStr = SecretStr("")
    """Interim static admin bearer (ADR-0004). Empty means *nobody* is an admin, which is the
    safe default: an unset secret must never make every caller privileged. Feature 007 replaces
    this with edge identity and API keys."""

    hf_token: SecretStr = SecretStr("")
    """Hugging Face credential. Exists ONLY on a node agent, read from that Studio's System
    keychain (spec FR-005). It is never mounted into a control-plane container and never
    appears in this process on core."""

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
    registry_reconcile_interval_s: float = 5.0

    # --- model store and engines (node side) ----------------------------
    node_store_dir: str = "/opt/coire/models"
    node_state_dir: str = "/opt/coire/state"
    node_hf_cache_dir: str = "/opt/coire/hf-cache"
    node_engine_port_range: str = "9500-9599"
    node_memory_budget_fraction: float = Field(default=0.90, gt=0.0, le=1.0)
    """Share of physical memory the platform may commit to engines. 0.90 of 256 GB is the
    230 GB budget ARCHITECTURE.md section 4 assumes; macOS keeps the rest."""
    node_engine_health_interval_s: float = 5.0
    """Also the detection bound for an externally-killed engine (spec SC-009)."""
    node_engine_start_timeout_s: float = 600.0
    """A large model takes minutes to page in from SSD; this is not a liveness timeout."""

    # --- acquisition ----------------------------------------------------
    disk_reserve_bytes: int = 50 * 1024**3
    """Kept free on every Studio when deciding whether a model fits (spec FR-010)."""
    kv_headroom_tokens: int = 32_768
    """Context tokens the memory estimate reserves KV cache for (research R6)."""
    memory_overhead_by_precision: dict[str, float] = Field(
        default_factory=lambda: {
            "4bit": 1.10,
            "5bit": 1.10,
            "6bit": 1.10,
            "8bit": 1.08,
            "bf16": 1.05,
            "fp16": 1.05,
            "other": 1.15,
        }
    )
    """Multipliers applied to weight bytes. Deliberately a setting, not a constant: feature 004
    corrects them from the resident-vs-estimate deltas this feature records (research R6)."""

    # --- node agent only ------------------------------------------------
    node_name: str = ""
    node_token: SecretStr = SecretStr("")
    node_listen_port: int = 9400
    core_mesh_host: str = "coire-core"
    core_api_port: int = 8080
    """Port the node reaches the control plane on over the mesh.

    8080 is nginx, the sole ingress. Without this the agent posted to the default HTTP port,
    where nothing on core listens — registration could never have succeeded on the real
    cluster, and was not caught because feature 000's T063 install was never run."""

    @property
    def database_url(self) -> str:
        """Async SQLAlchemy URL. The password is only materialised here."""
        pw = self.postgres_password.get_secret_value()
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{pw}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def engine_port_range(self) -> tuple[int, int]:
        """Parsed `node_engine_port_range`. Raises on a malformed value rather than guessing:
        a bad range would otherwise surface as a confusing bind failure at load time."""
        raw = self.node_engine_port_range.strip()
        low, _, high = raw.partition("-")
        try:
            start, end = int(low), int(high)
        except ValueError as exc:
            raise ValueError(f"node_engine_port_range must be 'LOW-HIGH', got {raw!r}") from exc
        if not (0 < start <= end < 65536):
            raise ValueError(f"node_engine_port_range out of range: {raw!r}")
        return start, end

    def overhead_for(self, precision: str) -> float:
        """Overhead multiplier for a precision label, falling back to `other`."""
        table = self.memory_overhead_by_precision
        if precision in table:
            return table[precision]
        # `4bit-g64` and friends carry a group-size suffix; match on the bit-width prefix.
        head = precision.split("-", 1)[0]
        return table.get(head, table.get("other", 1.15))

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
