# Implementation Plan: Reclaim Failed File Quota

**Branch**: `feat/014am-file-quota-reclaim` | **Parent**: [014 plan](../014-chat-web-ui/plan.md)

Add a bounded maintenance scan joining failed jobs, active reservations and attachments, filtered by the JSONB `output_purged` marker and excess reserved bytes. Lock owner then conversation/job/attachment/reservation, recheck conditions and shrink to original plus published derived bytes. Extract one helper that expands a reservation under owner/conversation quota checks for explicit inspect/render retries and first render. Preserve the immutable job source and user-visible failure if capacity is no longer available.

## Constitution Check

| Principle | Check |
| --- | --- |
| II | No model or parsing work in quota maintenance. |
| III | Existing core file/quota contracts and typed refusal. |
| IV | Owner/parent locks and worker purge marker before release. |
| VI | Content-free quota outcome counter/span. |
| VII | Release/re-reserve/concurrency tests and repository gates. |

No constitution exception or dependency.
