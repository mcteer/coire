"""A stand-in for the Hugging Face Hub.

`huggingface_hub` is pointed at it with `HF_ENDPOINT`, so the real client code — including its
error classes, which the inspection path branches on — runs unmodified against synthetic
repositories. Three exist, one per decision the pipeline has to make:

  * `fake/mlx-tiny`   — MLX-format, ungated: the happy path.
  * `fake/raw-torch`  — PyTorch safetensors with no MLX quantisation: rejected here, feature 002's job.
  * `fake/gguf-only`  — GGUF: rejected with guidance.
  * `fake/gated`      — 403 on metadata, so gating surfaces as gating rather than a generic error.
  * `fake/missing`    — 404.
"""

from __future__ import annotations

import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

MLX_CONFIG = {
    "model_type": "qwen3",
    "architectures": ["Qwen3ForCausalLM"],
    "num_hidden_layers": 4,
    "num_key_value_heads": 2,
    "num_attention_heads": 8,
    "head_dim": 64,
    "hidden_size": 512,
    "max_position_embeddings": 32768,
    "quantization": {"group_size": 64, "bits": 4, "mode": "affine"},
}
RAW_CONFIG = {
    "model_type": "llama",
    "architectures": ["LlamaForCausalLM"],
    "num_hidden_layers": 4,
    "num_key_value_heads": 2,
    "hidden_size": 512,
    "num_attention_heads": 8,
    "torch_dtype": "bfloat16",
    "max_position_embeddings": 8192,
}
TOKENIZER_CONFIG = {"chat_template": "{% for m in messages %}{{ m.content }}{% endfor %}"}

WEIGHTS = b"\xab" * 8192


def _blobs(kind: str) -> dict[str, bytes]:
    if kind == "gguf":
        return {"model.gguf": WEIGHTS, "README.md": b"gguf only"}
    config = MLX_CONFIG if kind == "mlx" else RAW_CONFIG
    return {
        "config.json": json.dumps(config).encode(),
        "tokenizer_config.json": json.dumps(TOKENIZER_CONFIG).encode(),
        "model-00001-of-00002.safetensors": WEIGHTS,
        "model-00002-of-00002.safetensors": WEIGHTS[:4096],
        "model.safetensors.index.json": b'{"weight_map": {}}',
    }


REPOS: dict[str, dict[str, Any]] = {
    "fake/mlx-tiny": {"kind": "mlx", "tags": ["mlx", "safetensors"], "gated": False},
    "fake/raw-torch": {"kind": "raw", "tags": ["transformers", "safetensors"], "gated": False},
    "fake/gguf-only": {"kind": "gguf", "tags": ["gguf"], "gated": False},
    "fake/gated": {"kind": "mlx", "tags": ["mlx"], "gated": True},
}
SHA = "0" * 36 + "abcd"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args: object) -> None:
        pass

    def _send(self, status: int, payload: object, *, headers: dict[str, str] | None = None) -> None:
        body = json.dumps(payload).encode() if not isinstance(payload, bytes) else payload
        self.send_response(status)
        if not isinstance(payload, bytes):
            self.send_header("Content-Type", "application/json")
        else:
            self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _repo_from(self, prefix: str) -> str | None:
        rest = self.path[len(prefix) :].split("?", 1)[0]
        parts = [p for p in rest.split("/") if p]
        return "/".join(parts[:2]) if len(parts) >= 2 else None

    def _not_found(self) -> None:
        # huggingface_hub keys its exception classes on this header, not on the body: without
        # it a 404 surfaces as a generic HfHubHTTPError and the inspection path cannot tell
        # "no such repo" from "the Hub is unwell".
        self._send(404, {"error": "Repository not found"}, headers={"X-Error-Code": "RepoNotFound"})

    def _gated(self) -> None:
        self._send(
            403,
            {"error": "Access to model is restricted. You must accept the licence."},
            headers={"X-Error-Code": "GatedRepo"},
        )

    def _tree(self, repo: str) -> None:
        """`/api/models/<repo>/tree/<rev>` — what snapshot_download actually enumerates."""
        spec = REPOS.get(repo)
        if spec is None:
            self._not_found()
            return
        if spec["gated"]:
            self._gated()
            return
        entries = []
        for name, data in sorted(_blobs(spec["kind"]).items()):
            entry: dict[str, Any] = {
                "type": "file",
                "path": name,
                "size": len(data),
                "oid": hashlib.sha1(data).hexdigest(),
            }
            if name.endswith((".safetensors", ".gguf")):
                entry["lfs"] = {
                    "oid": hashlib.sha256(data).hexdigest(),
                    "size": len(data),
                    "pointerSize": 134,
                }
            entries.append(entry)
        self._send(200, entries)

    def _model_info(self, repo: str) -> None:
        spec = REPOS.get(repo)
        if spec is None:
            self._not_found()
            return
        if spec["gated"]:
            self._gated()
            return
        blobs = _blobs(spec["kind"])
        siblings = []
        for name, data in sorted(blobs.items()):
            entry: dict[str, Any] = {"rfilename": name, "size": len(data)}
            # The Hub publishes an LFS sha256 only for LFS files - weights, not config.json.
            if name.endswith((".safetensors", ".gguf")):
                entry["lfs"] = {
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "size": len(data),
                    "pointerSize": 134,
                }
            else:
                entry["blobId"] = hashlib.sha1(data).hexdigest()
            siblings.append(entry)
        self._send(
            200,
            {
                "_id": "x",
                "id": repo,
                "modelId": repo,
                "sha": SHA,
                "gated": False,
                "private": False,
                "tags": spec["tags"],
                "library_name": "transformers",
                "siblings": siblings,
                "lastModified": "2026-01-01T00:00:00.000Z",
            },
        )

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path.startswith("/api/models/"):
            repo = self._repo_from("/api/models/")
            if repo is None:
                self._not_found()
                return
            if "/tree/" in path:
                self._tree(repo)
            elif "/revision/" in path:
                self._model_info(repo)
            else:
                self._model_info(repo)
            return

        # /<org>/<name>/resolve/<revision>/<file>
        if "/resolve/" in path:
            head, _, filename = path.partition("/resolve/")
            repo = head.strip("/")
            filename = filename.split("/", 1)[1] if "/" in filename else filename
            spec = REPOS.get(repo)
            if spec is None:
                self._not_found()
                return
            if spec["gated"]:
                self._gated()
                return
            data = _blobs(spec["kind"]).get(filename)
            if data is None:
                self._send(
                    404, {"error": "Entry not found"}, headers={"X-Error-Code": "EntryNotFound"}
                )
                return
            etag = hashlib.sha256(data).hexdigest()
            self._send(
                200,
                data,
                headers={
                    "ETag": f'"{etag}"',
                    "X-Repo-Commit": SHA,
                    "Accept-Ranges": "bytes",
                    "X-Linked-Size": str(len(data)),
                    "X-Linked-ETag": f'"{etag}"',
                },
            )
            return

        self._send(404, {"error": "not found"})


class FakeHub:
    """Run the fake Hub on an ephemeral port; use as a context manager."""

    def __init__(self) -> None:
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self.port = self._server.server_address[1]
        self.endpoint = f"http://127.0.0.1:{self.port}"

    def __enter__(self) -> FakeHub:
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()
