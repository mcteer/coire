# Feature Specification: Bounded Chat File Processor

**Feature Branch**: `feat/014z-file-processor`  
**Parent acceptance**: [014 Chat Web UI](../014-chat-web-ui/spec.md)

## Requirements

- FR-Z1: A CPU-only processor accepts strict generated-key `FileProcessRequest` objects and never accepts client paths or network locations. It verifies the original size and SHA-256 before parsing.
- FR-Z2: UTF-8 text/code extraction is bounded to 1 MiB. PDFium extracts Unicode text with page labels from at most 50 pages and renders explicitly selected pages for scanned PDFs.
- FR-Z3: Pillow accepts only still PNG, JPEG and WebP files, rejects animation and inputs above 20 MP, applies orientation, bounds derived rasters to 2048 pixels per side and 4 MP, removes metadata, and stores PNG derivatives under generated job/asset keys.
- FR-Z4: Each derivative is atomically published, never overwrites an existing asset, and records its digest, size and dimensions. The sum of returned derivative sizes is at most 32 MiB per job.
- FR-Z5: Malformed, unsupported, oversized, expired or mismatched inputs produce stable content-free error codes. The serving worker will enforce a hard native-call deadline and cleanup in the next child; this parser checks the deadline around each operation.

## Independent acceptance

Runtime-generated text, image and PDF fixtures cover extraction, scanned-page render, bounds, malformed input, symlink refusal, expiry, digest and no-overwrite behavior. Strict mypy and Ruff pass. Parent T047 remains open until the service enforces a hard native-call deadline and parser crash recovery is tested.
