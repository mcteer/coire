# Implementation Plan: Private Chat History Reads

**Branch**: feat/014m-chat-history-read | **Parent**: [014 plan](../014-chat-web-ui/plan.md)

Add list/detail selectors in the Chat service using shared projections and typed page models. The list cursor carries timestamp and UUID in base64url JSON and is bounded/validated. Detail acquires a PostgreSQL shared conversation lock before reading a bounded descending message page and related turns, then returns ascending display order, event cursor and the next older position. Routes use the existing user-bound Chat guard and safe error mapping. Generate OpenAPI and browser types; browser navigation follows in a later child.

## Constitution Check

| Principle | Check |
| --- | --- |
| I, II, II-a | Database reads only; no engine or core harness. |
| III | Shared Pydantic page/detail contracts and generated OpenAPI. |
| IV | Owner and tombstone filter even for admins; consistent snapshot. |
| V | Historical registry names are snapshots; no acquisition. |
| VI | Route spans/metrics/content-free logs. |
| VII | Contract and persistence tests. |

No constitution exception or dependency.
