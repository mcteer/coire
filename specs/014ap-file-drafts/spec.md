# Feature Specification: Same-Tab File Drafts

**Feature Branch**: `feat/014ap-file-drafts`
**Parent acceptance**: [014 Chat Web UI](../014-chat-web-ui/spec.md)

## Requirements

- FR-AP1: A conversation draft retains text, eligible model ID, selected file IDs, content modes and explicit PDF page choices for the same signed-in owner and browser tab.
- FR-AP2: Stored data contains no file bytes, credentials, tokens or arbitrary object fields; invalid IDs, duplicate files, invalid page choices and excessive selections are ignored.
- FR-AP3: Reopening a conversation restores choices only after its owner-scoped detail confirms the files and page counts. Missing choices are visibly reported, and incompatible visual choices still block Send.
- FR-AP4: Identity change, deletion and accepted send clear the appropriate draft data.
