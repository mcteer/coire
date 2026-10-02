"""Metadata-only acquisition inspection and placement estimates."""

from __future__ import annotations

import re

from coire_api.registry.placement import NodeView
from coire_core.image_assets import has_seedvr2_3b_layout, include_image_asset_path
from coire_core.memory import runtime_reservation_bytes
from coire_core.models.acquisition import FitDecision, InspectionResult, Precision, VariantRecipe
from coire_core.models.jobs import RepoInspection
from coire_core.models.registry import AUXILIARY_IMAGE_KINDS, EngineBackend, ModelKind
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
SUPPORTED_VISUAL_ARCHITECTURE_FAMILIES = frozenset({"idefics3"})
VISUAL_PROCESSOR_FILES = frozenset(
    {
        "config.json",
        "processor_config.json",
        "preprocessor_config.json",
        "tokenizer_config.json",
        "tokenizer.json",
    }
)
_IMAGE_SAFE_SUFFIXES = frozenset(
    {".safetensors", ".json", ".txt", ".model", ".tiktoken", ".md", ".yaml", ".yml"}
)
_IMAGE_SAFE_NAMES = frozenset({".gitattributes", "LICENSE", "LICENSE.txt"})


def classify_image_inspection(repo: RepoInspection, kind: ModelKind) -> InspectionResult:
    """Refuse unsafe image sources before an admin can start any weight transfer."""
    if kind is not ModelKind.IMAGE_MODEL and kind not in AUXILIARY_IMAGE_KINDS:
        raise ValueError("image asset kind required")
    backend = EngineBackend.MFLUX if kind is ModelKind.IMAGE_MODEL else EngineBackend.AUXILIARY
    rejection: str | None = None
    selected: set[str] = set()
    seen: set[str] = set()
    if repo.gated:
        rejection = "gated"
    elif not re.fullmatch(r"[0-9a-f]{40}", repo.revision) or repo.revision == "0" * 40:
        rejection = "unresolved_revision"
    elif not repo.license_id or repo.license_id.lower() in {"other", "unknown"}:
        rejection = "licence_unreviewed"
    elif not repo.files or len(repo.files) > 4096:
        rejection = "invalid_file_list"
    else:
        for item in repo.files:
            path = item.path
            parts = path.split("/")
            if (
                not path
                or path.startswith("/")
                or "\\" in path
                or any(part in {"", ".", ".."} for part in parts)
                or any(char in path for char in "*?[]")
                or any(ord(char) < 32 or ord(char) == 127 for char in path)
                or path in seen
            ):
                rejection = "unsafe_file_path"
                break
            seen.add(path)
            if not include_image_asset_path(repo.repo_id, kind, path):
                continue
            if path.split("/")[-1] in _IMAGE_SAFE_NAMES or any(
                path.endswith(suffix) for suffix in _IMAGE_SAFE_SUFFIXES
            ):
                selected.add(path)
                if path.endswith(".safetensors") and (
                    item.bytes <= 0
                    or item.upstream_sha256 is None
                    or not re.fullmatch(r"[0-9a-f]{64}", item.upstream_sha256)
                ):
                    rejection = "unverified_weight"
                    break
        if rejection is None and not any(path.endswith(".safetensors") for path in selected):
            rejection = "missing_safetensors"
        if (
            rejection is None
            and kind in {ModelKind.IMAGE_MODEL, ModelKind.CONTROL_MODEL, ModelKind.UPSCALE_MODEL}
            and "config.json" not in selected
            and not (
                kind is ModelKind.UPSCALE_MODEL and has_seedvr2_3b_layout(repo.repo_id, selected)
            )
        ):
            rejection = "missing_local_config"
    return InspectionResult(
        revision=repo.revision,
        kind=kind,
        architecture=repo.architecture,
        source_format="safetensors",
        backend=backend,
        gated=repo.gated,
        license_id=repo.license_id,
        metadata_bytes=max(0, repo.total_bytes - repo.weight_bytes),
        weight_bytes=repo.weight_bytes,
        total_bytes=repo.total_bytes,
        supported=rejection is None,
        rejection_code=rejection,
        rejection_detail=("image asset cannot be acquired" if rejection else None),
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


def visual_recipe_rejection(repo: RepoInspection, recipe: VariantRecipe) -> str | None:
    """An already-converted visual source cannot silently change precision on pull."""
    if recipe.precision is Precision.MIXED or recipe.mode is not None:
        return "visual conversion recipes are unsupported"
    quantization = repo.quantization
    if quantization is not None and quantization.bits in (4, 6, 8):
        expected = {4: Precision.BIT4, 6: Precision.BIT6, 8: Precision.BIT8}[quantization.bits]
        if recipe.precision is not expected:
            return "requested visual precision differs from the preconverted source"
        if recipe.bits is not None and recipe.bits != quantization.bits:
            return "requested visual bits differ from the preconverted source"
        if (
            recipe.group_size is not None
            and quantization.group_size is not None
            and recipe.group_size != quantization.group_size
        ):
            return "requested visual group size differs from the preconverted source"
        return None
    if repo.torch_dtype in ("float16", "bfloat16"):
        expected = Precision.FP16 if repo.torch_dtype == "float16" else Precision.BF16
        if recipe.precision is expected:
            return None
    return "visual source precision cannot be verified from metadata"


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


def estimate_variant_memory_bytes(
    metadata: RepoInspection,
    precision: Precision,
    backend: EngineBackend,
    settings: Settings,
) -> int:
    """Reserve actual preconverted weights once, plus the measured MLX runtime floor."""
    estimated = (
        metadata.weight_bytes
        if metadata.is_mlx_format or backend is EngineBackend.MLX_VLM
        else estimate_weight_bytes(metadata.weight_bytes, precision)
    )
    return runtime_reservation_bytes(
        int(estimated * settings.overhead_for(precision.value)),
        metadata.total_bytes if metadata.is_mlx_format else estimated,
    )


def classify_inspection(
    repo: RepoInspection,
    nodes: list[NodeView],
    settings: Settings,
) -> InspectionResult:
    """Turn node metadata into an actionable pre-transfer decision."""
    source_format = "gguf" if repo.has_gguf_only else "mlx" if repo.is_mlx_format else "safetensors"
    family = _family(repo.architecture)
    visual = family in SUPPORTED_VISUAL_ARCHITECTURE_FAMILIES
    visual_architecture = bool(
        repo.architecture
        and (
            repo.architecture.endswith("ForConditionalGeneration")
            or repo.architecture.endswith("ForVision2Seq")
        )
    )
    backend = EngineBackend.MLX_VLM if visual else EngineBackend.MLX_LM
    metadata_bytes = max(0, repo.total_bytes - repo.weight_bytes)
    candidates = list(Precision)
    fit: list[FitDecision] = []
    for precision in candidates:
        required = estimate_variant_memory_bytes(repo, precision, backend, settings)
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
    elif visual_architecture and not visual:
        rejection_code = "unsupported_visual_architecture"
        rejection_detail = "the pinned visual engine does not support this architecture"
    elif visual and not repo.is_mlx_format:
        rejection_code = "vision_requires_preconverted_mlx"
        rejection_detail = "visual acquisition requires an already-converted MLX repository"
    elif visual and not VISUAL_PROCESSOR_FILES.issubset({item.path for item in repo.files}):
        rejection_code = "incomplete_visual_processor"
        rejection_detail = "visual repository is missing local processor or tokenizer files"
    elif visual and not any(item.path.endswith(".safetensors") for item in repo.files):
        rejection_code = "missing_visual_weights"
        rejection_detail = "visual repository has no safetensors weights"
    elif not architecture_supported(repo.architecture):
        if not visual:
            rejection_code = "unsupported_architecture"
            rejection_detail = (
                f"no pinned bare engine supports architecture {repo.architecture or 'unknown'}"
            )
    elif not any(decision.fits for decision in fit):
        rejection_code = "no_fit_memory"
        rejection_detail = "no candidate precision fits a supported node memory budget"
        guidance = "use a smaller or pre-quantized MLX repository"

    return InspectionResult(
        revision=repo.revision,
        architecture=repo.architecture,
        source_format=source_format,
        backend=backend,
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
