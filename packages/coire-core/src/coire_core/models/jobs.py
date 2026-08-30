"""Acquisition wire shapes: repository inspection, checksum manifests, and job status.

Two jobs are modelled here, at different altitudes. `DownloadJob` is the control plane's
record of one acquisition — a cursor over a fixed sequence of stages (ADR-0005). `JobStatus`
is a node's record of one unit of work it is actually doing. The control plane advances the
first by issuing node verbs and reading the second.

The `ChecksumManifest` is what makes "verified" mean something. It is produced on the origin
node at pull time, carried to the replica, and recomputed there file by file; a mismatch on
any file fails the acquisition (spec FR-008, FR-009). Its canonical serialisation has to be
byte-stable across nodes, since its digest is compared.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator

SHA256_PATTERN = r"^[0-9a-f]{64}$"


class DownloadStage(StrEnum):
    """Ordered. The reconciler only ever advances or fails (ADR-0005).

    `verify_origin` and `verify_replica` are pass-through: the pull already verifies against
    upstream digests and the import verifies file by file, so the reconciler records them as
    transitions without issuing a node verb. They stay in the sequence so a later feature can
    make them real — a scheduled re-verify — without a contract change.
    """

    INSPECT = "inspect"
    PULL = "pull"
    VERIFY_ORIGIN = "verify_origin"
    EXPORT = "export"
    IMPORT = "import"
    VERIFY_REPLICA = "verify_replica"
    DONE = "done"
    FAILED = "failed"


STAGE_ORDER: tuple[DownloadStage, ...] = (
    DownloadStage.INSPECT,
    DownloadStage.PULL,
    DownloadStage.VERIFY_ORIGIN,
    DownloadStage.EXPORT,
    DownloadStage.IMPORT,
    DownloadStage.VERIFY_REPLICA,
    DownloadStage.DONE,
)


def next_stage(stage: DownloadStage) -> DownloadStage:
    """The stage after this one. `done` and `failed` are terminal and return themselves."""
    if stage in (DownloadStage.DONE, DownloadStage.FAILED):
        return stage
    return STAGE_ORDER[STAGE_ORDER.index(stage) + 1]


class JobKind(StrEnum):
    PULL = "pull"
    IMPORT = "import"
    VERIFY = "verify"


class JobStage(StrEnum):
    QUEUED = "queued"
    RESOLVING = "resolving"
    TRANSFERRING = "transferring"
    HASHING = "hashing"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_JOB_STAGES = frozenset({JobStage.DONE, JobStage.FAILED, JobStage.CANCELLED})


class JobErrorKind(StrEnum):
    GATED = "gated"
    NOT_FOUND = "not_found"
    NETWORK = "network"
    CHECKSUM_MISMATCH = "checksum_mismatch"
    DISK_FULL = "disk_full"
    CANCELLED = "cancelled"
    INTERNAL = "internal"


class RepoFile(BaseModel):
    """One file as Hugging Face describes it, before anything is downloaded."""

    model_config = ConfigDict(extra="forbid")

    path: str
    bytes: int = Field(ge=0)
    upstream_sha256: str | None = None
    """The LFS digest. Hugging Face publishes one for LFS files — every safetensors shard —
    and only a git blob id for small files, so this is null for `config.json` and friends."""


class Quantization(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bits: int | None = None
    group_size: int | None = None
    mode: str | None = None


class RepoInspection(BaseModel):
    """What the node learned about a repository from metadata alone (spec FR-010: this is
    computed before any bytes move)."""

    model_config = ConfigDict(extra="forbid")

    repo_id: str
    revision: str
    """The resolved commit sha, not the requested ref: a manifest must pin what was read."""
    files: list[RepoFile] = Field(default_factory=list)
    total_bytes: int = Field(ge=0)
    weight_bytes: int = Field(ge=0)
    is_mlx_format: bool
    has_gguf_only: bool = False
    gated: bool = False
    architecture: str | None = None
    quantization: Quantization | None = None
    torch_dtype: str | None = None
    max_position_embeddings: int | None = None
    chat_template_present: bool = False
    num_hidden_layers: int | None = None
    num_key_value_heads: int | None = None
    head_dim: int | None = None
    hidden_size: int | None = None
    num_attention_heads: int | None = None
    sizing_from_text_config: bool = False
    """True when the shape keys came from `config["text_config"]` — multimodal repositories
    such as Qwen3.8-27B nest the language model's shape there (research R2, R12)."""


class ManifestFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    bytes: int = Field(ge=0)
    sha256: str = Field(pattern=SHA256_PATTERN)
    upstream_sha256: str | None = None

    @field_validator("path")
    @classmethod
    def _path_is_contained(cls, value: str) -> str:
        """A manifest path is joined to a store directory on the receiving node. Anything that
        could escape it is rejected here, at the boundary, rather than at every use."""
        if not value or value.startswith("/") or value.startswith("\\"):
            raise ValueError(f"manifest path must be relative: {value!r}")
        parts = value.replace("\\", "/").split("/")
        if any(p in ("", ".", "..") for p in parts):
            raise ValueError(f"manifest path must not traverse: {value!r}")
        return value


class ChecksumManifest(BaseModel):
    """The record of exactly what a verified copy contains."""

    model_config = ConfigDict(extra="forbid")

    slug: str
    repo_id: str
    revision: str
    files: list[ManifestFile] = Field(default_factory=list)
    total_bytes: int = Field(ge=0)
    created_at: datetime

    def canonical_bytes(self) -> bytes:
        """A byte-stable serialisation for hashing.

        Files are sorted by path and `created_at` is excluded: two nodes hashing the same
        bytes must produce the same digest, and they will not have created their manifests at
        the same instant.
        """
        payload = {
            "slug": self.slug,
            "repo_id": self.repo_id,
            "revision": self.revision,
            "total_bytes": self.total_bytes,
            "files": [
                {"path": f.path, "bytes": f.bytes, "sha256": f.sha256}
                for f in sorted(self.files, key=lambda f: f.path)
            ],
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()

    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    def by_path(self) -> dict[str, ManifestFile]:
        return {f.path: f for f in self.files}


class JobStatus(BaseModel):
    """A node's view of one unit of acquisition work."""

    model_config = ConfigDict(extra="forbid")

    job_id: uuid.UUID
    kind: JobKind
    slug: str
    stage: JobStage
    bytes_done: int = 0
    bytes_total: int = 0
    files_done: int = 0
    files_total: int = 0
    error: str | None = None
    error_kind: JobErrorKind | None = None
    mismatched_paths: list[str] = Field(default_factory=list)
    manifest: ChecksumManifest | None = None
    manifest_sha256: str | None = None
    worker_pid: int | None = None
    worker_cpu_percent: float | None = None
    started_at: datetime
    updated_at: datetime
    finished_at: datetime | None = None

    @property
    def is_terminal(self) -> bool:
        return self.stage in TERMINAL_JOB_STAGES

    @property
    def percent(self) -> float:
        if self.bytes_total <= 0:
            return 100.0 if self.stage is JobStage.DONE else 0.0
        return min(100.0, self.bytes_done / self.bytes_total * 100.0)


class DownloadJob(BaseModel):
    """The control plane's record of one acquisition (ADR-0005)."""

    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    model_id: uuid.UUID
    origin_node: str
    replica_node: str
    stage: DownloadStage
    bytes_done: int = 0
    bytes_total: int = 0
    files_done: int = 0
    files_total: int = 0
    percent: float = 0.0
    failure_reason: str | None = None
    attempt: int = Field(default=1, ge=1)
    started_at: datetime
    updated_at: datetime
    finished_at: datetime | None = None
