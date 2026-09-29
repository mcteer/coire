# Feature Specification: First-Use Chat UI

**Feature Branch**: `feat/014j-chat-first-use`  
**Parent acceptance**: [014 Chat Web UI](../014-chat-web-ui/spec.md)

## Requirements

- FR-J1: An ordinary authenticated user opens a Chat surface without entering the admin console. Admin users can reach both Chat and Admin by keyboard-accessible navigation.
- FR-J2: The Chat picker lists server-filtered eligible models grouped by task tag. It shows description, tags, context window, size class, load state and measured warm-up estimate or an explicit unknown.
- FR-J3: A user can select a model, create a private conversation and send text. The answer grows from persisted native events, with model attribution visible across switches. The composer retains the draft when admission fails.
- FR-J4: Loading, running and failed states are clear. Unsafe HTML, executable links and external images in model output stay inert.
- FR-J5: The incomplete Chat release flag remains off by default. History, coding, files, observer replay and explicit Stop follow later children.

## Independent acceptance

Component tests cover ordinary/admin routing, empty picker, selection, safe rendering, draft preservation and streamed updates. Existing admin/failover tests, web typecheck/build and lint pass.
