# Feature Specification: Private Chat File Worker Service

**Feature Branch**: `feat/014aa-file-worker-service`  
**Parent acceptance**: [014 Chat Web UI](../014-chat-web-ui/spec.md)

## Requirements

- FR-AA1: Every private worker route, including health, requires a dedicated scheduler bearer secret. Empty, missing or incorrect credentials grant no access.
- FR-AA2: `POST /v1/process` accepts only the shared strict request contract, one active conversion, and a deadline no more than 30 seconds away. A generated job ID is immutable: identical repeats return the existing status, conflicting repeats fail, and completed/failed bytes never rerun automatically.
- FR-AA3: `GET /v1/jobs/{job_id}` returns the typed status and immutable result manifest; unknown jobs fail safely. Cancellation records a terminal flag and prevents a running result from publishing; partial derivatives are discarded.
- FR-AA4: A watchdog exits the sole service process if a blocking native parser call crosses its deadline. The durable scheduler will interpret missing status after process restart as failure in the later dispatch child.
- FR-AA5: Processing emits a content-free `coire.file_worker.process` span, `coire_file_processing_total{outcome}`, and structured job-ID/outcome logs. No file content, filenames, paths, URL or digest enters telemetry.

## Independent acceptance

ASGI contract tests cover authentication of every route, strict payload rejection, idempotency/conflicts, one-active admission, cancellation, safe failures and the watchdog timer. Full Ruff, strict mypy and Python tests pass. The service remains undeployed until its private Compose image/network/volume policy and scheduler dispatch are built.
