# Feature Specification: Chat Coding Run Bridge

**Feature Branch**: `feat/014bo-chat-coding-bridge`
**Parent acceptance**: [014 Chat Web UI](../014-chat-web-ui/spec.md)

A Chat code conversation admits Research, Plan and Apply against an owner registered workspace and a selected published coding model. Prior results belong to the same owner and workspace; Plan and Apply pin the source revision from the prior result. Apply requires an explicit successful plan and the existing harness-verified write gate. Admission saves the Chat turn, coding call, Studio run, event and audit together. The scheduler saves typed results and a terminal event before cleanup, and a controlling browser disconnect or Stop revokes the run token and queues a kill. MCP keeps its API-key scope gate while sharing the call and kill operations.

Visual coding attachments and the code-mode browser controls remain parent tasks T043/T058/T059.
