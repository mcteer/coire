# Implementation Plan: Private Chat Persistence

**Branch**: `feat/014d-chat-persistence` | **Parent**: [014 plan](../014-chat-web-ui/plan.md)

Add one reversible Alembic revision after `0014_mcp_calls`. Define tables in `coire_api.db` and matching DDL in `0015_chat_conversations.py`. Use UUIDs for conversation entities and a 26-character ULID for processing jobs. Use Postgres constraints for positions, revision, bytes and one nonterminal turn, plus owner FKs and owner/time indexes. Add text backend defaults and nullable visual JSON on models/variants. Downgrade checks for chat content before dropping tables. Bounded settings point only to private service paths; credentials remain file-mounted secrets. Test DDL and populated data in a disposable local database.

## Constitution Check

| Principle | Check |
| --- | --- |
| I, II, II-a | DB/settings only; no core model, harness or worker process. |
| III | Shared models from 014a/014c precede persistence. |
| IV | Owner FKs and generated private keys; no user-owned path or token stored as content. |
| V | Existing rows default to `mlx_lm`; visual measurement remains nullable/server controlled. |
| VI | Later runtime paths attach telemetry; schema alone emits none. |
| VII | Migration and metadata tests gate this child. |

No constitution exception. One reversible migration; no dependency change.
