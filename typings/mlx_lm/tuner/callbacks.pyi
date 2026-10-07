"""Pinned mlx-lm callback interface, available to type checks on non-MLX hosts."""

from typing import Any

class TrainingCallback:
    def on_train_loss_report(self, train_info: dict[str, Any]) -> None: ...
    def on_val_loss_report(self, val_info: dict[str, Any]) -> None: ...
