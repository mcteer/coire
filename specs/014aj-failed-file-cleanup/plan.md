# Implementation Plan: Failed File Output Cleanup

**Branch**: `feat/014aj-failed-file-cleanup` | **Parent**: [014 plan](../014-chat-web-ui/plan.md)

Scan terminal failed file jobs whose JSONB manifest lacks the cleanup marker. For each, call the worker's existing idempotent purge route using the typed private client, then lock the row and replace the failed output manifest with `{"output_purged": true}` if it remains failed. Leave safe_error and attempt unchanged. Repeat on refusal/unavailability. Keep the scanner separate from the DBOS execution lane to avoid delaying ordinary dispatch. Unit tests cover status preservation, retry, idempotence and query bounding.

## Constitution Check

| Principle | Check |
| --- | --- |
| II | CPU worker alone writes/deletes derived volume. |
| III | Reuses strict core purge contract. |
| IV | Generated IDs and dedicated private token. |
| VI | Content-free outcome metric and span. |
| VII | Restart/refusal tests and repository gates. |

No constitution exception or dependency.
