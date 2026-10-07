"""Pinned mlx-lm exports used by Coire's bare engine integration."""

from typing import Any

def load(
    path_or_hf_repo: str,
    tokenizer_config: dict[str, Any] | None = ...,
    model_config: dict[str, Any] | None = ...,
    adapter_path: str | None = ...,
    lazy: bool = ...,
    return_config: bool = ...,
    revision: str | None = ...,
) -> tuple[Any, Any] | tuple[Any, Any, dict[str, Any]]: ...
