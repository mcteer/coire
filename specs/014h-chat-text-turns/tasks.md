# Tasks: Persistent Text Chat Turns

- [X] H001 Add failing admission, idempotency, context and model-snapshot tests in `apps/coire-api/tests/contract/test_chat_turns.py`.
- [X] H002 Implement locked admission, persisted messages/events and status projection in `apps/coire-api/src/coire_api/chat/turns.py` and `routes/chat.py`.
- [X] H003 Add failing stream, cold-load, failure and disconnect tests in `apps/coire-api/tests/unit/test_chat_streaming.py`.
- [X] H004 Implement native SSE streaming through gateway proxy with short persistence sessions in `apps/coire-api/src/coire_api/chat/streaming.py`.
- [X] H005 Add send/status telemetry, dashboard/alert updates and runbook recovery procedure.
- [X] H006 Regenerate API/web types; run Ruff, strict mypy, full Python suite, web gates and OpenAPI freshness.
