# Feature Specification: Browser Conversation Observer

**Feature Branch**: feat/014u-chat-browser-observer  
**Parent acceptance**: [014 Chat Web UI](../014-chat-web-ui/spec.md)

## Requirements

- FR-U1: A selected viewing tab follows persisted conversation events with GET and a scoped cursor. It must never repeat a generation POST or gain Stop authority from observation.
- FR-U2: Another tab's committed progress appears within two seconds while connected. A replacement snapshot refreshes the selected conversation; stale responses after navigation do not replace the new selection.
- FR-U3: The observer reconnects after transient transport loss, resets an invalid future cursor, stops on revoked/private/deleted access, and pauses while this tab owns a generation stream.

## Independent acceptance

Hook and page tests prove scoped GET replay and other-tab updates without POST. Web tests, typecheck/build and lint pass.
