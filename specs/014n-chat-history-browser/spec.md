# Feature Specification: Browser Chat History Navigation

**Feature Branch**: feat/014n-chat-history-browser  
**Parent acceptance**: [014 Chat Web UI](../014-chat-web-ui/spec.md)

## Requirements

- FR-N1: An owner sees newest-first saved conversations, can reopen one after reload, and can request older conversation/message pages.
- FR-N2: Reopened messages retain saved model-name attribution and partial answer text. A recorded active turn is labelled, and the browser does not silently restart it.
- FR-N3: History navigation keeps model and message state scoped to the selected conversation. Stale async responses cannot replace a newer selection. A new conversation clears the current view.
- FR-N4: Below 1200px, history opens as a keyboard-accessible drawer. Navigation during an active generation remains disabled until cancellation support is added.

## Independent acceptance

Browser tests cover reload/open, older pages, model snapshots, selection races, and responsive drawer operation. Build/lint and existing admin tests pass.
