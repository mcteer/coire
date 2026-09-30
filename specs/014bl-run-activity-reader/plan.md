# Implementation Plan: Authenticated Studio Run Activity Reader

**Branch**: `feat/014bl-run-activity-reader` | **Parent**: [014 plan](../014-chat-web-ui/plan.md)

Add a fixed `/node/runs/{run_id}/activity` GET to the existing guarded router. In `RunManager`, inspect the container's managed, node and run labels and verify a separate coding output mount before requesting `/coire-output/activity-<run_id>.jsonl` through Docker archive. Cap archive and payload bytes, require one regular file with the expected name, validate all lines with `RunActivity`, strict run ID and contiguous sequence, then return at most 100 records after the cursor. Expose missing output as `available=false` and an overflow marker as `truncated=true`. Add malformed/cursor/auth contracts and run node gates.

## Constitution Check

| Principle | Check |
| --- | --- |
| I/II | Only coire-node reads its Studio run container; core does not execute a harness. |
| III | The route returns the published strict `RunActivityPage` wire model. |
| IV/V | Existing node auth, container ownership labels and a fixed archive path enforce scope. |
| VI | The existing run operation span/counter records activity reads and failures. |
| VII | Route and archive contracts, type checks and native node wheel gate the change. |

No dependency, migration or architecture deviation.
