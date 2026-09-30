# Implementation Plan: Bounded Chat File Processor

**Branch**: `feat/014z-file-processor` | **Parent**: [014 plan](../014-chat-web-ui/plan.md)

Use the existing exact-pinned PDFium and Pillow dependencies in the isolated worker package. Read originals through a generated UUID key with `O_NOFOLLOW`, check byte limit and digest, then sniff PDF/image/text in memory. Bound PDF page count and text characters before extraction; render selected pages only. Normalize still rasters to metadata-free RGB PNG, then write a unique temporary file and atomically link it to a generated asset key. Return the shared Pydantic result manifest. Errors carry codes without source data or native exception text.

The worker process, private authentication, 30-second process watchdog and durable scheduler dispatch remain a separate child of parent T046–T049. The API remains default-off for Chat and has no file admission path yet.

## Constitution Check

| Principle | Check |
| --- | --- |
| II | CPU-only parsing, rasterization and image normalization; no model or harness. |
| III | Input and output use strict `coire-core` Pydantic contracts and generated IDs. |
| IV | Original keys are generated, symlinks refused, output filenames generated, and errors contain no content. |
| V | No model acquisition or caller-supplied engine identifier. |
| VI | Worker service instrumentation is planned with T046/T068; processor will run only inside that service. |
| VII | Real parser fixtures exercise malformed and valid paths; no runtime route is released yet. |

No constitution exception or new dependency. `pypdfium2` lacks a `py.typed` marker; the mypy override is restricted to its module namespace.
