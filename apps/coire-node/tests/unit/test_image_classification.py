"""Offline classifier policy and hard stage supervision."""

from __future__ import annotations

import asyncio
import sys
from contextlib import nullcontext
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from coire_core.models.images import ImageClassificationResult, ImageContentTag
from coire_node.image_runtime import classification

REVISION = "96cb0d0342c7afb80cab76ecc58b265fa44da256"


def _result(tag: ImageContentTag, *, score: Decimal | None) -> ImageClassificationResult:
    return ImageClassificationResult(
        tag=tag,
        score=score,
        threshold=Decimal("0.5"),
        classifier_revision=REVISION,
        processor_sha256="a" * 64,
        safe_error=None if score is not None else "classifier_failed",
        tagged_at=datetime.now(UTC),
    )


def test_policy_explicit_overrides_classifier_without_changing_prompt_or_score() -> None:
    normal = _result(ImageContentTag.NORMAL, score=Decimal("0.01"))
    assert classification.apply_explicit_policy(normal, explicit=False) == normal
    overridden = classification.apply_explicit_policy(normal, explicit=True)
    assert overridden.tag is ImageContentTag.EXPLICIT
    assert overridden.score == normal.score
    assert overridden.classifier_revision == normal.classifier_revision
    unknown = _result(ImageContentTag.UNKNOWN, score=None)
    assert (
        classification.apply_explicit_policy(unknown, explicit=True).tag is ImageContentTag.EXPLICIT
    )


class FakeProcess:
    def __init__(self, *, output: bytes, completes: bool = True, returncode: int = 0) -> None:
        self.pid = 123456789
        self.output = output
        self.completes = completes
        self.returncode = returncode
        self.killed = False

    async def wait(self) -> int:
        if not self.completes and not self.killed:
            await asyncio.Event().wait()
        return self.returncode

    async def communicate(self) -> tuple[bytes, None]:
        return self.output, None

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


@pytest.mark.parametrize(
    ("process", "rss", "stage_timeout_s", "error"),
    [
        (FakeProcess(output=b"bad-json"), 1, 0.1, "classifier_invalid_result"),
        (FakeProcess(output=b"{}", returncode=1), 1, 0.1, "classifier_failed"),
        (FakeProcess(output=b"{}", completes=False), 1, 0.01, "classifier_timeout"),
        (FakeProcess(output=b"{}", completes=False), 1_000_000, 0.1, "classifier_memory"),
    ],
)
async def test_supervisor_returns_unknown_and_kills_failed_stage(
    monkeypatch: pytest.MonkeyPatch,
    process: FakeProcess,
    rss: int,
    stage_timeout_s: float,
    error: str,
) -> None:
    async def fake_spawn(*args: object, **kwargs: object) -> FakeProcess:
        assert kwargs["stderr"] == asyncio.subprocess.DEVNULL
        assert "HF_TOKEN" not in kwargs["env"]  # type: ignore[operator]
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_spawn)
    monkeypatch.setattr(classification, "process_rss_bytes", lambda pid: rss)
    result = await classification.classify_image(
        Path("/local/model"),
        Path("/local/output.png"),
        reservation_bytes=1000,
        timeout_s=stage_timeout_s,
    )
    assert result.tag is ImageContentTag.UNKNOWN
    assert result.safe_error == error
    if error in {"classifier_timeout", "classifier_memory"}:
        assert process.killed


async def test_supervisor_uses_strict_child_result_and_policy_explicit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _result(ImageContentTag.NORMAL, score=Decimal("0.1")).model_dump_json().encode()
    process = FakeProcess(output=payload)

    async def fake_spawn(*args: object, **kwargs: object) -> FakeProcess:
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_spawn)
    monkeypatch.setattr(classification, "process_rss_bytes", lambda pid: 1)
    result, peak_rss_bytes = await classification.classify_image_with_peak(
        Path("/local/model"),
        Path("/local/output.png"),
        reservation_bytes=1000,
        policy_explicit=True,
    )
    assert result.tag is ImageContentTag.EXPLICIT
    assert result.score == Decimal("0.1")
    assert peak_rss_bytes == 1


def test_result_contract_rejects_unbounded_or_inconsistent_fields() -> None:
    with pytest.raises(ValueError):
        _result(ImageContentTag.NORMAL, score=None)
    with pytest.raises(ValueError):
        ImageClassificationResult.model_validate(
            {**_result(ImageContentTag.NORMAL, score=Decimal("0.1")).model_dump(), "score": 2}
        )


def test_local_loader_uses_cpu_safetensors_and_disables_remote_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model_dir = tmp_path / "model"
    model_dir.mkdir()
    with (model_dir / "model.safetensors").open("wb") as weight:
        weight.truncate(classification.CLASSIFIER_WEIGHT_BYTES)
    image_path = tmp_path / "output.png"
    image_path.write_bytes(b"private")
    calls: list[tuple[str, object]] = []

    def fake_digest(path: Path) -> str:
        return (
            classification.CLASSIFIER_WEIGHT_SHA256
            if path.name == "model.safetensors"
            else "a" * 64
        )

    class FakeProcessor:
        @classmethod
        def from_pretrained(cls, path: str, **kwargs: object) -> FakeProcessor:
            calls.append(("processor", kwargs))
            return cls()

        def __call__(self, *, images: object, return_tensors: str) -> dict[str, object]:
            assert return_tensors == "pt"
            return {"pixel_values": object()}

    class FakeModel:
        config = SimpleNamespace(id2label={0: "normal", 1: "nsfw"})

        @classmethod
        def from_pretrained(cls, path: str, **kwargs: object) -> FakeModel:
            calls.append(("model", kwargs))
            return cls()

        def to(self, device: str) -> None:
            calls.append(("device", device))

        def eval(self) -> None:
            calls.append(("eval", True))

        def __call__(self, **kwargs: object) -> SimpleNamespace:
            return SimpleNamespace(logits=object())

    class FakeScores:
        def __getitem__(self, key: object) -> FakeScores:
            return self

        def item(self) -> float:
            return 0.7

    class FakeImage:
        def __enter__(self) -> FakeImage:
            return self

        def __exit__(self, *args: object) -> None:
            pass

        def convert(self, mode: str) -> object:
            assert mode == "RGB"
            return object()

    monkeypatch.setattr(classification, "_digest", fake_digest)
    monkeypatch.setitem(
        sys.modules,
        "torch",
        SimpleNamespace(inference_mode=nullcontext, softmax=lambda logits, dim: FakeScores()),
    )
    monkeypatch.setitem(
        sys.modules, "PIL", SimpleNamespace(Image=SimpleNamespace(open=lambda path: FakeImage()))
    )
    monkeypatch.setitem(
        sys.modules,
        "transformers",
        SimpleNamespace(
            AutoImageProcessor=FakeProcessor, AutoModelForImageClassification=FakeModel
        ),
    )
    result = classification._classify_local(model_dir, image_path)
    assert result.tag is ImageContentTag.EXPLICIT
    assert result.score == Decimal("0.7")
    assert calls[0] == ("processor", {"local_files_only": True, "trust_remote_code": False})
    assert calls[1] == (
        "model",
        {"local_files_only": True, "trust_remote_code": False, "use_safetensors": True},
    )
    assert ("device", "cpu") in calls
