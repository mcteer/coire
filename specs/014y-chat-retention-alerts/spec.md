# Feature Specification: Chat Event Retention and Purge Alert

**Feature Branch**: feat/014y-chat-retention-alerts  
**Parent acceptance**: [014 Chat Web UI](../014-chat-web-ui/spec.md)

## Requirements

- FR-Y1: Maintenance deletes expired Chat SSE event rows in bounded pages without removing saved message history; observer cursor gaps continue to produce replacement snapshots.
- FR-Y2: Maintenance reports the age of the oldest deleted conversation awaiting verified purge. A provisioned dashboard panel and alert show an overdue 24-hour deletion backlog, including attached conversations waiting on later blob cleanup.
- FR-Y3: Event payloads, private titles and user IDs never become metric labels or alert annotations.

## Independent acceptance

Unit tests cover bounded event deletion and overdue gauge age; dashboard JSON and alert YAML parse and reference the expected metric. Full Python/web gates pass.
