# Feature Specification: Private Chat Persistence

**Feature Branch**: `feat/014d-chat-persistence`  
**Parent acceptance**: [014 Chat Web UI](../014-chat-web-ui/spec.md)

## Requirements

- FR-D1: Persist owner-scoped conversations, ordered messages, turns, and replay events with one active turn per conversation, unique request identity, immutable model attribution and revision.
- FR-D2: Persist private attachment metadata, processing jobs, output quota reservations and tombstones. Processing job IDs are ULIDs. Physical content keys remain server-generated and private.
- FR-D3: Existing model and variant rows gain text backend defaults and nullable measured visual capabilities without changing populated text rows or public eligibility.
- FR-D4: The migration is reversible against a populated disposable database; downgrade must not silently discard live chat content. Runtime settings have bounded defaults and secret paths.
- FR-D5: No route or engine behavior is introduced by this child; later children own admission, processing, cleanup and observability.

## Independent acceptance

ORM metadata and migration agree, including foreign keys, unique constraints and partial active-turn index. Populated upgrade and guarded downgrade are tested against disposable PostgreSQL, along with Ruff and strict mypy. Parent 014 runtime acceptance remains open.
