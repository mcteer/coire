# Feature Specification: Text File Chat Context

**Feature Branch**: `feat/014ao-text-file-context`
**Parent acceptance**: [014 Chat Web UI](../014-chat-web-ui/spec.md)

## Requirements

- FR-AO1: A user can select ready UTF-8 text/code files or a PDF's extracted Unicode as input to a plain Chat turn. Admission verifies the file owner, conversation, publication state and immutable source/result identity before sending.
- FR-AO2: A scan with no extracted Unicode fails visibly in text mode. Visual mode and still images remain refused until visual admission is implemented; no input is silently omitted.
- FR-AO3: Admission includes the selected file text in context preflight, stores the exact prompt snapshot and selected file modes with the user message, and reuses that snapshot for later history. The displayed user text remains the user's own text.
- FR-AO4: The browser sends only compatible ready text selections and shows each selected file and content mode beside the resulting message. It keeps incompatible selections visible and blocks Send.
