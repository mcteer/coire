# Feature Specification: Explicit PDF Page Images

**Feature Branch**: `feat/014al-pdf-pages`
**Parent acceptance**: [014 Chat Web UI](../014-chat-web-ui/spec.md)

## Requirements

- FR-AL1: A ready PDF owner can explicitly select one to ten distinct pages within its verified page count. The API queues a generated-key render job and reserves worst-case derived bytes under owner/conversation quota locks. It never renders in the API.
- FR-AL2: The selected pages, request ID and expected revision are bound durably. A repeated identical request returns the same attachment state. A failed render may be retried once only after its output is purged and with the same page selection; changing the selection requires reupload.
- FR-AL3: API publication verifies exact worker page IDs/digests/dimensions before ready. It retains the prior PDF Unicode extraction while publishing page previews, actual derived quota and page metadata. A text-only model cannot silently treat an empty scan as useful text; turn admission will enforce that in the visual/context phase.
- FR-AL4: Owner/Origin/quota/page/attempt contracts and render publication tests pass. Chat remains default-off pending file UI, visual inference and remaining feature gates.
