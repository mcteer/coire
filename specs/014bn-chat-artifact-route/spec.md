# Feature Specification: Chat Apply Artifact Download

**Feature Branch**: `feat/014bn-chat-artifact-route`
**Parent acceptance**: [014 Chat Web UI](../014-chat-web-ui/spec.md)

A Chat owner can download the branch bundle produced by a completed Apply turn through a Chat route. The route binds the active conversation, turn, MCP call, run, typed Apply result and retained artifact before using the existing digest-verified Studio stream. Foreign, deleted, cross-parent, failed, expired and mismatched references remain unavailable. The existing MCP artifact URL also refuses a deleted Chat conversation.
