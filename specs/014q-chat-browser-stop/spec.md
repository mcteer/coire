# Feature Specification: Browser Stop for Plain Chat

**Feature Branch**: feat/014q-chat-browser-stop  
**Parent acceptance**: [014 Chat Web UI](../014-chat-web-ui/spec.md)

## Requirements

- FR-Q1: The owner sees a Stop control while a plain-chat turn is active, including after loading a saved active conversation.
- FR-Q2: Stop sends the typed owner request once per click and leaves the stream connected until its terminal event so saved partial output remains visible.
- FR-Q3: The UI shows stop progress and terminal outcome. A failed Stop request remains retryable.

## Independent acceptance

A browser test holds a live stream, requests Stop, verifies the server request and then observes the stopped terminal with partial text retained. Web tests, typecheck/build and lint pass.
