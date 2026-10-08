"""Authored immutable serving probes; only Studio tokenizers count their rendered input."""

import hashlib
import json

PROMPTS = (
    "Explain in two short sentences why a shared computer should account for memory before starting work.",
    "List three concise steps for verifying that a background job completed successfully.",
    "Write a short plain-language description of the difference between a queue deadline and an execution deadline.",
    "Summarize why an interrupted operation should have an idempotent retry in two sentences.",
)


def prompt_digest() -> str:
    return hashlib.sha256(
        json.dumps(PROMPTS, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()
