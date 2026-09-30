# Feature Specification: Safe Chat Code and Links

**Feature Branch**: `feat/014av-chat-markdown`  
**Parent acceptance**: [014 Chat Web UI](../014-chat-web-ui/spec.md)

## Requirements

- FR-AV1: Fenced code has an explicit copy action that copies its plain text without formatting.
- FR-AV2: Raw HTML and automatic remote images remain inert. Executable and protocol-relative links do not become active links; ordinary HTTPS, mail and local relative links remain available.
- FR-AV3: Copy failure gives a readable status rather than silently claiming success.
