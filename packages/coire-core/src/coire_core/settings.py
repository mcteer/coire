"""Platform settings.

Secrets are read from files under `/run/secrets/` — never from environment variables — because
the constitution requires file-mounted secrets and `coire-up` passes them to compose as
environment-sourced secrets that Docker materialises as files (research R4).
"""

from __future__ import annotations

import json
from functools import lru_cache
from urllib.parse import quote, urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
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
        # The node agent fills its control, registration, and HF tokens from the System keychain after
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

    bootstrap_admin_email: SecretStr = SecretStr("")
    openai_api_key: SecretStr = SecretStr("")
    anthropic_api_key: SecretStr = SecretStr("")
    """Configured first local administrator identity. It is sourced from Keychain like other
    bootstrap material and never grants access without a separately verified Access assertion."""
    cloudflare_access_issuer: str = ""
    cloudflare_access_audience: str = ""
    cloudflare_jwks_ttl_s: float = Field(default=300.0, gt=0.0)
    cloudflare_jwt_leeway_s: float = Field(default=60.0, ge=0.0, le=300.0)
    credential_stream_recheck_s: float = Field(default=1.0, gt=0.0, le=10.0)
    identity_legacy_admin_enabled: bool = False
    """Test/rollback-only bridge for pre-007 suites. Production compose never enables it."""

    hf_token: SecretStr = SecretStr("")
    """Hugging Face credential. Exists ONLY on a node agent, read from that Studio's System
    keychain (spec FR-005). It is never mounted into a control-plane container and never
    appears in this process on core."""

    # --- telemetry ------------------------------------------------------
    otlp_endpoint: str = "http://otel-collector:4317"
    service_version: str = "0.1.0"
    mcp_enabled: bool = False
    ops_enabled: bool = False
    diagnostics_enabled: bool = False

    # --- node probing ---------------------------------------------------
    mesh_hosts_file: str = "/etc/hosts"
    control_host_suffix: str = ""
    data_host_suffix: str = ".fabric"
    legacy_network_mode: bool = False
    node_probe_interval_s: float = 10.0
    node_probe_failures_before_unreachable: int = 3
    node_collection_budget_cpu_pct: float = 2.0
    node_collection_budget_rss_bytes: int = 150 * 1024 * 1024
    node_inventory_file: str = "/app/nodes.yaml"
    registry_reconcile_interval_s: float = 5.0
    acquisition_poll_interval_s: float = Field(default=2.0, gt=0.0)
    acquisition_stuck_seconds: int = Field(default=1800, ge=60)
    acquisition_perplexity_tolerance: float = Field(default=0.10, ge=0.0, le=1.0)
    acquisition_conversion_memory_overhead: float = Field(default=1.20, ge=1.0)
    acquisition_disk_safety_fraction: float = Field(default=0.10, ge=0.0, le=1.0)
    acquisition_validation_fixture_version: str = "v1"

    # --- placement scheduler -------------------------------------------
    placement_default_budget_bytes: int = Field(default=230 * 1024**3, gt=0)
    placement_sandbox_bytes: int = Field(default=16 * 1024**3, ge=0)
    placement_health_freshness_s: float = Field(default=30.0, gt=0.0)
    placement_cpu_saturation_percent: float = Field(default=90.0, ge=0.0, le=100.0)
    placement_busy_drain_timeout_s: float = Field(default=10.0, ge=0.0)
    placement_poll_interval_s: float = Field(default=1.0, gt=0.0)
    placement_ttl_interval_s: float = Field(default=30.0, gt=0.0)
    placement_lease_ttl_s: float = Field(default=60.0, gt=0.0)
    instance_drain_timeout_s: float = Field(default=30.0, gt=0.0)
    instance_event_poll_interval_s: float = Field(default=0.5, gt=0.0)
    console_snapshot_interval_s: float = Field(default=2.0, ge=0.25, le=30.0)
    scheduler_idle_scan_max_s: float = Field(default=5.0, ge=1.0, le=60.0)
    scheduler_failure_backoff_max_s: float = Field(default=30.0, ge=1.0, le=300.0)
    scheduler_shutdown_timeout_s: float = Field(default=10.0, ge=1.0, le=60.0)
    run_kill_poll_interval_s: float = Field(default=0.25, gt=0.0, le=0.5)

    # --- Studio data-link and sharding ---------------------------------
    link_probe_interval_s: float = Field(default=30.0, gt=0.0)
    link_probe_freshness_s: float = Field(default=120.0, gt=0.0)
    link_failures_before_down: int = Field(default=2, ge=1)
    link_successes_before_up: int = Field(default=3, ge=1)
    sharding_allow_ring_fallback: bool = True
    sharding_start_timeout_s: float = Field(default=600.0, gt=0.0)
    sharding_port_range: str = "9600-9699"
    sharding_jaccl_hostfile: str = "/opt/coire/state/jaccl-hostfile.json"
    sharding_ring_hostfile: str = "/opt/coire/state/ring-hostfile.json"

    # --- compatible inference gateway ----------------------------------
    gateway_wait_ceiling_s: float = Field(default=600.0, gt=0.0)
    gateway_keepalive_interval_s: float = Field(default=10.0, gt=0.0)
    gateway_max_inflight_per_engine: int = Field(default=1, ge=1)
    gateway_retry_after_s: int = Field(default=30, ge=1)
    gateway_engine_request_timeout_s: float = Field(default=900.0, gt=0.0)
    gateway_inline_visual_enabled: bool = False
    provider_chat_enabled: bool = False

    # --- private native chat and CPU file worker -----------------------
    chat_enabled: bool = False
    chat_output_tokens: int = Field(default=1024, ge=1, le=4096)
    chat_upload_max_bytes: int = Field(default=10 * 1024**2, ge=1, le=10 * 1024**2)
    chat_conversation_quota_bytes: int = Field(default=50 * 1024**2, ge=1, le=50 * 1024**2)
    chat_owner_quota_bytes: int = Field(default=500 * 1024**2, ge=1, le=500 * 1024**2)
    chat_derived_job_max_bytes: int = Field(default=32 * 1024**2, ge=1, le=32 * 1024**2)
    chat_extracted_text_max_bytes: int = Field(default=1024**2, ge=1, le=1024**2)
    chat_pdf_max_pages: int = Field(default=50, ge=1, le=50)
    chat_upload_max_pixels: int = Field(default=20_000_000, ge=1, le=20_000_000)
    chat_normalized_max_pixels: int = Field(default=4_000_000, ge=1, le=4_000_000)
    chat_normalized_max_side: int = Field(default=2048, ge=1, le=2048)
    chat_event_retention_hours: int = Field(default=24, ge=1, le=24)
    chat_purge_deadline_hours: int = Field(default=24, ge=1, le=24)
    chat_browser_origin: str = ""

    @field_validator("chat_browser_origin")
    @classmethod
    def chat_origin_is_exact(cls, value: str) -> str:
        if not value:
            return value
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or parsed.query
            or parsed.fragment
            or parsed.netloc != value.split("://", 1)[-1]
            or (parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1"})
        ):
            raise ValueError(
                "chat browser origin must be an exact HTTPS origin or local HTTP origin"
            )
        return value

    chat_original_root: str = "/opt/coire/chat/originals"
    chat_derived_root: str = "/opt/coire/chat/derived"
    file_worker_input_root: str = "/opt/coire/chat/originals"
    file_worker_output_root: str = "/opt/coire/chat/derived"
    file_worker_url: str = "http://coire-file-worker:8010"
    file_worker_service_token: SecretStr = SecretStr("")
    file_worker_process_timeout_s: int = Field(default=30, ge=1, le=30)
    file_worker_max_active: int = Field(default=1, ge=1, le=1)

    # --- agent harness -------------------------------------------------
    harness_retry_limit: int = Field(default=2, ge=0, le=5)
    harness_tool_output_byte_cap: int = Field(default=16_384, ge=1024, le=1_048_576)
    harness_summary_threshold: float = Field(default=0.8, gt=0.0, le=1.0)
    harness_evaluation_pass_score: float = Field(default=0.8, ge=0.0, le=1.0)

    # --- core-only ops harness -----------------------------------------
    ops_service_token: SecretStr = SecretStr("")
    """Dedicated read/propose credential mounted only into coire-ops and coire-api."""

    ops_service_url: str = "http://coire-ops:8003"
    ops_api_url: str = "http://coire-api:8000"
    ops_gateway_url: str = "http://coire-api:8000/v1"
    ops_model_id: str = ""
    ops_confirmation_ttl_s: int = Field(default=300, ge=30, le=300)
    ops_session_heartbeat_s: float = Field(default=10.0, gt=0.0, le=60.0)
    ops_session_stale_s: float = Field(default=30.0, gt=0.0, le=300.0)
    ops_request_timeout_s: float = Field(default=120.0, gt=0.0, le=900.0)
    ops_service_instance: str = Field(default="coire-ops", min_length=1, max_length=128)

    # --- stateless control-plane failover ------------------------------
    failover_snapshot_path: str = "/opt/coire/failover/snapshot.json"
    failover_proof_path: str = "/opt/coire/failover/proof.json"
    failover_snapshot_max_age_s: float = Field(default=120.0, gt=0.0, le=3600.0)
    failover_reservation_bytes: int = Field(default=256 * 1024 * 1024, ge=0)
    failover_lease_ttl_s: float = Field(default=15.0, gt=0.0, le=300.0)
    failover_election_interval_s: float = Field(default=2.0, gt=0.0, le=30.0)
    failover_promotion_threshold_s: float = Field(default=15.0, gt=0.0, le=300.0)
    failover_demotion_threshold_s: float = Field(default=45.0, gt=0.0, le=900.0)
    failover_drain_timeout_s: float = Field(default=30.0, gt=0.0, le=300.0)
    failover_heartbeat_latency_budget_ms: float = Field(default=50.0, gt=0.0, le=1000.0)
    """A beat slower than this is degraded, not unreachable. Control-path RTT on the lab is ~1 ms."""
    failover_member_name: str = ""
    failover_core_public_key: str = ""
    failover_edge_a_public_key: str = ""
    failover_edge_b_public_key: str = ""
    failover_membership_epoch: int = Field(default=1, ge=1)
    failover_signing_key_id: str = "core-1"
    failover_local_relay_url: str = ""
    failover_peer_relay_url: str = ""
    failover_peer_key: SecretStr = SecretStr("")
    """Per-host Keychain-sourced signing material; never a node registration token."""
    failover_relay_token: SecretStr = SecretStr("")
    """Studio peer inference relay credential, scoped only to the failover proxy route."""
    failover_frontend_image: str = ""

    @field_validator("failover_frontend_image")
    @classmethod
    def failover_image_is_digest_pinned(cls, value: str) -> str:
        import re

        if value and not re.fullmatch(r"[A-Za-z0-9._:/-]+@sha256:[a-f0-9]{64}", value):
            raise ValueError("failover frontend image must be digest-pinned")
        return value

    @model_validator(mode="after")
    def failover_demotion_outlasts_promotion(self) -> Settings:
        if self.failover_demotion_threshold_s <= self.failover_promotion_threshold_s:
            raise ValueError("failover demotion threshold must exceed the promotion threshold")
        return self

    # --- Studio container runs ----------------------------------------
    run_concurrency_cap: int = Field(default=3, ge=1, le=32)
    run_default_memory_bytes: int = Field(default=4 * 1024**3, ge=128 * 1024**2)
    run_max_memory_bytes: int = Field(default=16 * 1024**3, ge=128 * 1024**2)
    run_default_nano_cpus: int = Field(default=2_000_000_000, ge=100_000_000)
    run_default_pids_limit: int = Field(default=256, ge=16, le=4096)
    run_default_timeout_s: int = Field(default=900, ge=10, le=86_400)
    run_max_log_bytes: int = Field(default=8 * 1024**2, ge=1024)
    run_max_result_bytes: int = Field(default=4 * 1024**2, ge=1024)
    run_token_ttl_s: int = Field(default=1200, ge=60, le=86_400)
    run_stuck_seconds: int = Field(default=1800, ge=60, le=86_400)
    run_workspace_root: str = "/opt/coire/workspaces"
    mcp_source_hosts: str = "github.com"
    """Comma-separated HTTPS repository hosts allowed for Studio workspace preparation."""
    mcp_workspace_max_bytes: int = Field(default=512 * 1024**2, ge=1024, le=8 * 1024**3)
    mcp_workspace_prepare_timeout_s: int = Field(default=120, ge=1, le=900)
    mcp_artifact_retention_hours: int = Field(default=168, ge=1, le=720)
    mcp_run_timeout_seconds: int = Field(default=900, ge=10, le=900)
    run_agent_image: str = ""
    run_relay_image: str = ""
    run_relay_request_bytes: int = Field(default=2 * 1024**2, ge=1024, le=16 * 1024**2)
    run_relay_start_timeout_s: float = Field(default=15.0, gt=0.0, le=60.0)
    run_gateway_url: str = "http://coire-core.lab:8180/v1"
    run_docker_socket: str = "/var/run/docker.sock"

    @field_validator("run_agent_image", "run_relay_image")
    @classmethod
    def run_images_are_digest_pinned(cls, value: str) -> str:
        import re

        if value and not re.fullmatch(r"[A-Za-z0-9._:/-]+@sha256:[a-f0-9]{64}", value):
            raise ValueError("run image must be digest-pinned")
        return value

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
    node_registration_token: SecretStr = SecretStr("")
    node_listen_port: int = 9400
    node_data_listen_port: int = 9401
    node_control_host: str = ""
    node_data_host: str = ""
    core_mesh_host: str = "coire-core"
    core_control_host: str = "coire-core.lab"
    core_api_port: int = 8180
    """Port the node reaches the control plane on over the mesh.

    8180 is the host-facing nginx ingress. Without this the agent posted to the default HTTP port,
    where nothing on core listens — registration could never have succeeded on the real
    cluster, and was not caught because feature 000's T063 install was never run."""

    @property
    def database_url(self) -> str:
        """Async SQLAlchemy URL. The password is only materialised here."""
        user = quote(self.postgres_user, safe="")
        pw = quote(self.postgres_password.get_secret_value(), safe="")
        database = quote(self.postgres_db, safe="")
        return (
            f"postgresql+asyncpg://{user}:{pw}@{self.postgres_host}:{self.postgres_port}/{database}"
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
