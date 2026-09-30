# Implementation Plan: Chat Event Retention and Purge Alert

**Branch**: feat/014y-chat-retention-alerts | **Parent**: [014 plan](../014-chat-web-ui/plan.md)

Extend the existing API maintenance pass with a 1000-row expired-event batch. Count deleted events without logging payload. Query the oldest deleted/unpurged conversation timestamp after cleanup and set a low-cardinality age gauge, using zero when no backlog exists. Add a Chat dashboard time series and a Prometheus alert over the 24-hour deadline; retain existing request-failure alert.

## Constitution Check

| Principle | Check |
| --- | --- |
| III | Uses existing ChatEvent persistence and cursor/snapshot contracts. |
| IV | Content is removed from event storage and excluded from telemetry. |
| VI | Gauge, dashboard panel and overdue alert. |
| VII | Retention/gauge tests and dashboard/alert parse checks. |

No constitution exception or dependency.
