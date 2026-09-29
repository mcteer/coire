# Implementation Plan: Durable Chat File Dispatch

**Branch**: `feat/014af-file-dispatch` | **Parent**: [014 plan](../014-chat-web-ui/plan.md)

Add a DBOS step/workflow in `coire_scheduler/files.py` and scan queued/running jobs in the scheduler dispatcher. Build a strict worker request from persisted generated keys/digest and one reserved still-image output ID (or selected render-page IDs). Commit the running marker and immutable request manifest before calling the worker. When recovering a running row, query status only; missing status fails visibly. A known 429 refusal may retry the unchanged request within its original deadline because the worker did not accept it. Poll bounded status, recheck attachment/conversation tombstones, cancel if deleted, validate returned IDs/digests/pages, then store a processed manifest. The API will independently verify file bytes and quota before publishing ready in the following child.

## Constitution Check

| Principle | Check |
| --- | --- |
| II | Only the CPU worker parses; scheduler orchestrates and never loads weights/harnesses. |
| III | Worker request/status/result are shared strict core models. |
| IV | Dedicated token, generated keys, immutable request and deletion/cancel guards. |
| VI | Scheduler span, outcome counter and content-free correlation logs. |
| VII | DBOS recovery and worker refusal/manifest unit tests; no automatic crash loop. |

No constitution exception or dependency.
