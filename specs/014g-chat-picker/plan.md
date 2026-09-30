# Implementation Plan: Native Chat Picker and Creation

**Branch**: `feat/014g-chat-picker` | **Parent**: [014 plan](../014-chat-web-ui/plan.md)

Use shared `ChatPicker*` and `ChatConversation*` contracts. Query registry and engine rows in the API, apply the strict Chat eligibility predicate, and project only safe metadata. Create conversations in one short database transaction, deriving owner and defaults on the server. Register the native route and map `CoireError` to problem details. Add chat-specific span/counter and operational inspection metadata; include dashboard and alert alongside this new path. Test contracts before implementation. Turn admission, replay and inference follow separate children.

Picker size classes use the registry memory estimate: at most 16 GiB is small, at most 64 GiB is medium, above that is large, and missing/nonpositive is unknown. Warm-up comes only from the latest measured engine load. Visual acceptance requires both the VLM backend and a verified measured visual capability.

## Constitution Check

| Principle | Check |
| --- | --- |
| I, II, II-a | No engine or harness change; API reads registry state only. |
| III | Shared Pydantic contracts already exist; regenerate OpenAPI and TS types. |
| IV | Verified Chat principal, exact browser Origin and owner-derived row. |
| V | Published, ready, entitled registry IDs only; no acquisition. |
| VI | OTel span, count, content-free log, dashboard and alert ship with routes. |
| VII | Contract tests and full regression gate. |

No constitution exception, dependency or migration.
