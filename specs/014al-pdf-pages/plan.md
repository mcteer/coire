# Implementation Plan: Explicit PDF Page Images

**Branch**: `feat/014al-pdf-pages` | **Parent**: [014 plan](../014-chat-web-ui/plan.md)

Extend the file process route for `operation=render`. Lock owner, conversation, attachment and reservation; require a ready PDF and selected pages within its published count for the first render. Expand its active reservation from original-only to original plus the 32 MiB render bound after checking owner/conversation quotas, rebind to a new generated job and emit an attachment event. A failed first render can use attempt two after the existing output-purge marker, with identical pages and unchanged original source. Preserve client revision/pages through DBOS dispatch and cleanup. On verified ready publication, keep the inspection result as `text_result` while the main manifest holds the rendered page result, and preserve extraction status.

## Constitution Check

| Principle | Check |
| --- | --- |
| II | Only the isolated CPU worker rasterizes; API/scheduler queue and verify. |
| III | Existing strict core process/result/preview contracts and generated API types. |
| IV | Owner/revision/request binding, generated keys, quota locks and explicit pages. |
| VI | File action/dispatch/publication content-free spans and counters. |
| VII | Page/duplicate/quota/retry/publication contracts and repository gates. |

No constitution exception or dependency.
