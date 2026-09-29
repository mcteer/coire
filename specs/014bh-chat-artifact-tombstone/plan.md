# Implementation Plan: Chat Artifact Tombstone Guard

**Branch**: `feat/014bh-chat-artifact-tombstone` | **Parent**: [014 plan](../014-chat-web-ui/plan.md)

Join an artifact's durable call ID to a Chat turn and conversation in the existing owner/expiry lookup. Refuse a tombstoned conversation. On owner deletion, expire linked artifacts before committing the tombstone, so a later Chat purge cannot restore access by removing the join row. Keep the existing digest and node status validation. Add direct contracts for both guards, then run API gates.

## Constitution Check

| Principle | Check |
| --- | --- |
| I/II | Existing Studio artifact storage and download path remain in use. |
| III | No wire shape changes. |
| IV/V | Owner scope, deletion and expiry are enforced before node access; no model or acquisition change. |
| VI | Existing artifact download and Chat deletion telemetry cover the path. |
| VII | Contract regressions, API checks and image policy gate the change. |

No dependency, migration or architecture deviation.
