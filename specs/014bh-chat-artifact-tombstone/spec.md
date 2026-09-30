# Feature Specification: Chat Artifact Tombstone Guard

**Feature Branch**: `feat/014bh-chat-artifact-tombstone`  
**Parent acceptance**: [014 Chat Web UI](../014-chat-web-ui/spec.md)

Deleting a conversation immediately makes branch artifacts produced by its coding turns unavailable, including through the preexisting MCP artifact metadata and download URLs. The deletion transaction expires those artifacts before committing the tombstone. An ordinary MCP artifact without a Chat turn keeps its existing owner and expiry behavior.

Acceptance: the owner receives 404 from either existing artifact URL after a Chat tombstone; an owner deletion expires linked artifacts in the same transaction; foreign and expired artifacts continue to receive 404.
