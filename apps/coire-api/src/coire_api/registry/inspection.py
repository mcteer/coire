"""Metadata-only acquisition inspection and placement estimates."""

from __future__ import annotations

import re

from coire_api.registry.placement import NodeView
from coire_core.models.acquisition import FitDecision, InspectionResult, Precision
from coire_core.models.jobs import RepoInspection
from coire_core.settings import Settings

# These are architecture families supported by the pinned mlx-lm release. Matching is
# deliberately normalized and exact-by-family, never inferred from a repository name.
SUPPORTED_ARCHITECTURE_FAMILIES = frozenset(
    {
        "deepseekv2",
        "gemma2",
        "gemma3",
        "llama",
        "mistral",
        "mixtral",
        "phi3",
        "qwen2",
        "qwen2moe",
        "qwen3",
        "qwen3moe",
        "qwen35",
    }
)


def _family(architecture: str | None) -> str:
    if not architecture:
        return ""
    value = architecture.removesuffix("ForCausalLM").removesuffix("ForConditionalGeneration")
    return re.sub(r"[^a-z0-9]", "", value.lower())


def architecture_supported(architecture: str | None) -> bool:
    family = _family(architecture)
    return any(
        family == item or family.startswith(item) for item in SUPPORTED_ARCHITECTURE_FAMILIES
    )


def estimate_weight_bytes(source_bytes: int, precision: Precision) -> int:
    """Conservative serialized weight estimate from an fp16/bf16 source."""
    ratios = {
        Precision.BF16: 1.0,
        Precision.FP16: 1.0,
        Precision.BIT8: 0.55,
        Precision.BIT6: 0.43,
        Precision.BIT4: 0.32,
        Precision.MIXED: 0.45,
    }
    return int(source_bytes * ratios[precision])


def classify_inspection(
    repo: RepoInspection,
    nodes: list[NodeView],
    settings: Settings,
) -> InspectionResult:
    """Turn node metadata into an actionable pre-transfer decision."""
    source_format = "gguf" if repo.has_gguf_only else "mlx" if repo.is_mlx_format else "safetensors"
    metadata_bytes = max(0, repo.total_bytes - repo.weight_bytes)
    candidates = list(Precision)
    fit: list[FitDecision] = []
    for precision in candidates:
        weight = estimate_weight_bytes(repo.weight_bytes, precision)
        required = int(weight * settings.overhead_for(precision.value))
        fit.extend(
            FitDecision(
                node=node.name,
                precision=precision,
                required_bytes=required,
                available_bytes=node.memory_budget_bytes,
                fits=required <= node.memory_budget_bytes,
            )
            for node in nodes
        )

    rejection_code: str | None = None
    rejection_detail: str | None = None
    guidance: str | None = None
    if repo.gated:
        rejection_code = "gated"
        rejection_detail = "accept the repository licence with the node credential, then retry"
    elif repo.has_gguf_only:
        rejection_code = "gguf_only"
        rejection_detail = "GGUF is not an MLX source format"
        guidance = "use the original safetensors repository or a pre-quantized MLX repository"
    elif not architecture_supported(repo.architecture):
        rejection_code = "unsupported_architecture"
        rejection_detail = f"mlx-lm does not support architecture {repo.architecture or 'unknown'}"
    elif not any(decision.fits for decision in fit):
        rejection_code = "no_fit_memory"
        rejection_detail = "no candidate precision fits a supported node memory budget"
        guidance = "use a smaller or pre-quantized MLX repository"

    return InspectionResult(
        revision=repo.revision,
        architecture=repo.architecture,
        source_format=source_format,
        gated=repo.gated,
        chat_template_present=repo.chat_template_present,
        metadata_bytes=metadata_bytes,
        weight_bytes=repo.weight_bytes,
        total_bytes=repo.total_bytes,
        supported=rejection_code is None,
        rejection_code=rejection_code,
        rejection_detail=rejection_detail,
        source_repo_guidance=guidance,
        fit=fit,
    )
