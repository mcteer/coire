# ADR-0008: Isolated attachment processing and bare visual inference

**Status**: Accepted for feature 014; runtime deployment remains gated by its child implementations
**Date**: 2026-09-28
**Spec**: [014 Chat Web UI](../../specs/014-chat-web-ui/spec.md)

## Context

The user confirmed text/code/PDF/image attachments and support for scanned PDFs. Existing Coire serves text through `mlx_lm.server`; neither storage alone nor its text-only gateway/harness messages can interpret images. PDF/image decoding also introduces native libraries and resource exhaustion risks that should not share the API process.

## Decision

Add a single-process `coire-file-worker` container on core for bounded CPU-only PDF text extraction, selected-page rasterization and still-image normalization. It owns no model, tokenizer, agent harness or database. API retains ownership/file metadata; scheduler owns the DBOS workflow. The worker reads originals read-only, writes generated derivative keys on a separate volume, and communicates only with the scheduler on a dedicated processing network using a scoped Keychain-sourced credential. Its distroless image is non-root with read-only rootfs, dropped capabilities, healthcheck, 512 MiB/1 CPU and one active conversion. A hard deadline ends stuck native work; scheduler records failure and API offers bounded explicit retry.

Add bare `mlx_vlm.server` as a registry-controlled Studio backend, initially single-node and preconverted supported MLX models. Node alone launches explicit argv, records/reserves/re-adopts/stops processes, and supplies offline local model paths. Admin acquisition/visual validation/replication establishes readiness and modality bounds. Gateway and coding contracts carry bounded image parts; arbitrary URLs, caller paths, remote code and implicit downloads are refused. Visual coding still needs normal coding capability and verified Apply admission.

The initial package declaration is exact and platform-scoped: `mlx-vlm==0.7.3` is a Darwin-only node dependency and is never installed in core images. The future node installer must stage the complete locked graph in a new versioned environment, run text and visual smoke checks, then flip its active symlink; a failed smoke keeps the prior environment active. Text and visual launch argv stay separate. The `coire-file-worker` package declares only CPU parsing libraries; the compose service is restricted to a private scheduler network and generated-key volumes. The core failover frontend remains stateless and text-only.

## Consequences

- Constitution I remains direct bare-engine control; II keeps all learned inference on Studios; II-a keeps one process/role per service. No constitutional exception is introduced.
- Core adds CPU preprocessing capacity, a hardened image and two private mounts. Native parser failure is isolated from API availability. All payloads, auth, telemetry and quotas remain typed and bounded per III–VI.
- New Python/native package pins, notices, arm64 linking/image scans, local tiny visual tests and reproducible node environment staging are release gates under VII.
- Stateful chat remains unavailable during core failover; the existing degraded frontend remains text-only and excludes unsupported visual backends.
- Existing text/API/MCP behavior stays compatible. Images are understood here; image generation remains feature 015. Retrieval, a separate OCR engine and mutable coding sessions remain outside 014.
- Deployment/architecture documentation will be updated with implementation. Acceptance must demonstrate parser isolation, no model execution on core, visual memory reservation/cancellation, and rollback to the prior text environment.

## Alternatives

Text-only uploads were declined by the user. A child parser process inside the API conflicts with literal II-a; in-process parsing shares crash and memory fate. External vision services, inference wrappers and model execution on core conflict with platform boundaries. A separate OCR engine is unnecessary when selected scanned pages can be passed to the chosen visual model.
