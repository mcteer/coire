"""Build a deterministic, local-only tiny mflux Z-Image fixture under /models.

The production loader never imports this test module. Engine tests may patch
ZImageInitializer._init_models to instantiate these exact small dimensions while
still using the real tokenizer, encoder, transformer, VAE and inherited mflux
generation path. No Hub call or existing model directory is touched.
"""

from __future__ import annotations

import gc
import importlib
import json
import os
import shutil
from pathlib import Path

import mlx.core as mx
from mlx.utils import tree_flatten

from coire_node.store import Store

MAX_FIXTURE_BYTES = 1_000_000_000
SEED = 15_015
REVISION = "0150" * 10


def tiny_models() -> tuple[object, object, object]:
    """Return real mflux modules with small encoder/transformer dimensions."""
    text_encoder = importlib.import_module(
        "mflux.models.z_image.model.z_image_text_encoder.text_encoder"
    )
    transformer = importlib.import_module(
        "mflux.models.z_image.model.z_image_transformer.transformer"
    )
    vae = importlib.import_module("mflux.models.z_image.model.z_image_vae.vae")

    return (
        vae.VAE(),
        transformer.ZImageTransformer(
            dim=128,
            n_layers=1,
            n_refiner_layers=1,
            n_heads=1,
            cap_feat_dim=128,
            axes_dims=[32, 48, 48],
        ),
        text_encoder.TextEncoder(
            vocab_size=256,
            hidden_size=128,
            num_hidden_layers=2,
            num_attention_heads=1,
            num_key_value_heads=1,
            intermediate_size=256,
            head_dim=128,
        ),
    )


def _save_module(root: Path, name: str, module: object) -> None:
    parameters = getattr(module, "parameters", None)
    if not callable(parameters):
        raise TypeError("tiny component is not an MLX module")
    component_dir = root / name
    component_dir.mkdir(mode=0o700)
    arrays = dict(tree_flatten(parameters()))
    mx.eval(*arrays.values())
    mx.save_safetensors(
        component_dir / "model.safetensors",
        arrays,
        metadata={"mflux_version": "0.20.0", "quantization_level": "None"},
    )
    (component_dir / "config.json").write_text(json.dumps({"coire_test_fixture": True}))


def _save_tokenizer(root: Path) -> None:
    tokenizers = importlib.import_module("tokenizers")
    vocabulary = {"[UNK]": 0, "[PAD]": 1, "[BOS]": 2, "[EOS]": 3}
    vocabulary.update({f"word{i}": i for i in range(4, 256)})
    raw = tokenizers.Tokenizer(tokenizers.models.WordLevel(vocabulary, unk_token="[UNK]"))
    raw.pre_tokenizer = tokenizers.pre_tokenizers.Whitespace()
    tokenizer_class = importlib.import_module("transformers").PreTrainedTokenizerFast
    tokenizer = tokenizer_class(
        tokenizer_object=raw,
        unk_token="[UNK]",
        pad_token="[PAD]",
        bos_token="[BOS]",
        eos_token="[EOS]",
        chat_template="{% for message in messages %}[BOS] {{ message['content'] }} [EOS]{% endfor %}",
    )
    directory = root / "tokenizer"
    directory.mkdir(mode=0o700)
    tokenizer.save_pretrained(directory)
    configuration = directory / "tokenizer_config.json"
    values = json.loads(configuration.read_text())
    values["chat_template"] = tokenizer.chat_template
    configuration.write_text(json.dumps(values, sort_keys=True) + "\n")
    (directory / "chat_template.jinja").unlink(missing_ok=True)


def build(target: Path) -> dict[str, object]:
    repository = Path(__file__).resolve().parents[4]
    ignored_root = (repository / "models").resolve()
    destination = target.resolve()
    if destination.parent != ignored_root or not destination.name.startswith("test--"):
        raise ValueError("COIRE_TEST_MODEL must be a direct test--* child of ignored /models")
    if destination.exists():
        raise FileExistsError("tiny fixture destination already exists")
    ignored_root.mkdir(mode=0o700, exist_ok=True)
    destination.mkdir(mode=0o700)
    try:
        mx.random.seed(SEED)
        (destination / "config.json").write_text(json.dumps({"coire_test_fixture": True}))
        for name, module in zip(("vae", "transformer", "text_encoder"), tiny_models(), strict=True):
            _save_module(destination, name, module)
            del module
            gc.collect()
        _save_tokenizer(destination)
        files = sorted(path for path in destination.rglob("*") if path.is_file())
        total = sum(path.stat().st_size for path in files)
        if total > MAX_FIXTURE_BYTES:
            raise ValueError("tiny fixture exceeds 1 GB")
        store = Store(ignored_root)
        manifest = store.hash_tree(
            destination.name, repo_id="coire-test/tiny-z-image", revision=REVISION
        )
        store.write_manifest(manifest)
        report: dict[str, object] = {
            "schema_version": 1,
            "seed": SEED,
            "runtime": "mflux-0.20.0",
            "bytes": total,
            "manifest_sha256": manifest.sha256(),
            "files": [
                {"path": str(path.relative_to(destination)), "bytes": path.stat().st_size}
                for path in files
            ],
        }
        (ignored_root / f"{destination.name}.fixture.json").write_text(
            json.dumps(report, indent=2) + "\n"
        )
        return report
    except BaseException:
        shutil.rmtree(destination)
        raise


if __name__ == "__main__":
    value = os.environ.get("COIRE_TEST_MODEL")
    if not value:
        raise SystemExit("set COIRE_TEST_MODEL to a new /models/test--* directory")
    result = build(Path(value))
    print(f"tiny fixture built: {result['bytes']} bytes; manifest {result['manifest_sha256']}")
