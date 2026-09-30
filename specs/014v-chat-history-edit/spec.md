# Feature Specification: Versioned Chat History Edit

**Feature Branch**: feat/014v-chat-history-edit  
**Parent acceptance**: [014 Chat Web UI](../014-chat-web-ui/spec.md)

## Requirements

- FR-V1: An owner may rename a conversation with its expected revision. A stale revision refuses the edit without losing the typed title; missing, foreign or deleted IDs are uniform 404s.
- FR-V2: An eligible selected model may change only while no turn is active; a title edit may occur during a turn. A real edit increments revision and persists a `conversation.updated` event for observers.
- FR-V3: The history drawer exposes keyboard-accessible rename controls and refreshes saved revisions after a conflict. Code-mode transitions remain gated by their later coding implementation.

## Independent acceptance

Service and route contracts cover locking, owner scope, stale conflict, event and browser Origin. Browser component tests cover refused and accepted rename. Python and web gates pass.
