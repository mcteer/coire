# Tasks: Private Chat Persistence

- [X] D001 Write migration and ORM metadata tests for owner FKs, request identity, active-turn uniqueness, model defaults and downgrade guard.
- [X] D002 Add ORM rows for conversations, messages, turns, events, attachments, processing jobs and quota reservations.
- [X] D003 Implement `0015_chat_conversations.py` with matching DDL, populated defaults and guarded reversal.
- [X] D004 Add bounded chat/file-worker settings and strict typed chat failures without changing existing error behavior.
- [X] D005 Validate migration on a disposable populated PostgreSQL target, then run Ruff, strict mypy and relevant tests.
