# Feature Specification: Typed Private File Worker Client

**Feature Branch**: `feat/014ae-file-worker-client`  
**Parent acceptance**: [014 Chat Web UI](../014-chat-web-ui/spec.md)

## Requirements

- FR-AE1: The scheduler reaches the private worker only through a dedicated bearer credential and configured internal service URL. Requests and responses use strict `coire-core` processing/status/cancel models; unexpected IDs or schemas fail safely.
- FR-AE2: Busy, missing and unavailable worker states are distinct content-free failures. Worker body, parser diagnostics, response URLs and credentials never enter public exceptions or logs.
- FR-AE3: An inspect request may reserve one generated output ID for a possible still-image derivative. Text and PDF inspection return no asset for that reserved ID; still-image inspection publishes exactly that ID. More than one inspect output ID is refused.
- FR-AE4: An oversized image decoder guard yields the stable `image_too_large` code even if Pillow refuses before opening an image object.

## Independent acceptance

Mock HTTP contracts cover auth, paths, typed process/status/cancel, busy/missing/internal/schema/ID failures and empty credentials. Real parser fixtures cover reserved output IDs for text, PDF and still images. Ruff and strict mypy pass. Scheduler DBOS dispatch is the next child; native Chat remains default-off.
