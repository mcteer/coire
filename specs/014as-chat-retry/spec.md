# Feature Specification: Explicit Retry of Interrupted Chat

**Feature Branch**: `feat/014as-chat-retry`
**Parent acceptance**: [014 Chat Web UI](../014-chat-web-ui/spec.md)

## Requirements

- FR-AS1: A user may explicitly retry only the latest failed, stopped or interrupted plain-Chat response. A retry keeps the original user text and file choices, rechecks ownership/model/context, and has a new request identity linked to the prior turn.
- FR-AS2: Retry creates one new assistant attempt while retaining the original user message and saved partial assistant response. Repeating the same retry request ID follows the existing turn; it never starts another generation.
- FR-AS3: Later turns include only the latest assistant attempt for each user input in model history. The transcript still shows earlier partial attempts for the owner.
- FR-AS4: The browser offers Retry when the latest response ended early, preserves an unrelated unsent draft, and does not duplicate the user message in its optimistic transcript.
