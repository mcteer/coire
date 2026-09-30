# Feature Specification: Native Chat Picker and Creation

**Feature Branch**: `feat/014g-chat-picker`  
**Parent acceptance**: [014 Chat Web UI](../014-chat-web-ui/spec.md)

## Requirements

- FR-G1: `GET /api/v1/chat/models` returns only published, ready models entitled to the verified user, including an admin. It reveals user-safe picker fields, current load state and only measured warm-up estimates. No empty picker triggers acquisition.
- FR-G2: The picker identifies verified coding and measured image capability without exposing paths, repository IDs, ports, node IDs, or unpublished models. Text chat does not imply visual support.
- FR-G3: `POST /api/v1/chat/conversations` creates a private, owner-bound draft with a server-side UUID, revision one and optional eligible model. Caller-supplied owner, revision or internal fields fail validation. An ineligible or missing selected model has a uniform 404.
- FR-G4: Both routes use the native Chat principal guard. Browser mutations require the exact configured Origin. Domain failures return RFC 9457 problem details.
- FR-G5: The new paths emit an OTel span, content-free structured outcome log, count metric, dashboard panel and failure alert.

## Independent acceptance

Contract tests exercise user/admin entitlements, hidden/unready model omission, safe response shape, empty picker, owner-derived creation, Origin refusal and ineligible model refusal. OpenAPI and generated web types are fresh. No turn generation route is introduced in this child.
