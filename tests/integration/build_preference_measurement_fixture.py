"""Acquire and node-convert the small, approved native CI measurement base.

The Apache-2.0 AMD 135M base uses the existing Llama architecture/tokenizer rules.
The complete numerical/runtime matrix independently retains its Qwen assets.
"""

from __future__ import annotations

import build_preference_fixture as fixture


def main() -> None:
    fixture.REPOSITORY = "mlx-community/AMD-Llama-135m-4bit"
    fixture.QUANTIZED = fixture.REPOSITORY.replace("/", "--")
    fixture.DENSE_PRECISION = "fp16"
    fixture.DENSE = fixture.QUANTIZED + ".preference-dense-fp16"
    fixture.CONVERSION_MEMORY_BYTES = 2 * 1024**3
    fixture.main()


if __name__ == "__main__":
    main()
