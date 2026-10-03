# Implementation Plan: Image node and worker commands

**Spec**: [spec.md](spec.md)

## Constitution Check

| Principle | Compliance |
| --- | --- |
| I | Worker backend discriminator is bare mflux only. |
| II/II-a | Contracts only; no core engine work or container changes. |
| III | All node/worker/transfer commands are strict coire-core models. |
| IV | Attempt/fence, node and digest bindings reject implicit trust. |
| V | Model/variant IDs come from registry UUIDs; no arbitrary path or fetch. |
| VI | No running path; telemetry follows implementation. |
| VII | Schema tests precede service implementation. |

## Approach

Add `models/image_worker.py` with separate load, run, status, input, grant, receipt and cleanup commands. Use existing UUID, ULID and SHA-256 conventions. Validate relationships within messages; database authorization and idempotency remain service duties. No OpenAPI change until routes consume the commands.
