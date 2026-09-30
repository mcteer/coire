# Implementation Plan: Bounded Coding Run Activity Spool

**Branch**: `feat/014bk-run-activity-spool` | **Parent**: [014 plan](../014-chat-web-ui/plan.md)

Create a small append-only writer around the published `RunActivity` model. Restrict names to an internal allowlist and states to the model contract, cap record count and bytes, and append with no-follow and mode 0600 on the isolated output mount. Instrument the actual context, model, edit, test and bundle operations in `run_coding`; do not serialize arguments or exception text. Include agent tests in the default pytest discovery, test lifecycle, failure and caps, then run agent gates. Node read, durable collection and UI remain dependent work.

## Constitution Check

| Principle | Check |
| --- | --- |
| I/II | The existing isolated Studio user harness writes only its output mount. |
| III | Records use the existing strict `coire-core` RunActivity contract. |
| IV/V | The private spool omits user content and does not change verification or write gates. |
| VI | Activity records and existing harness metrics describe the run. |
| VII | Agent lifecycle and limit tests plus image gates cover the change. |

No dependency, migration or architecture deviation.
