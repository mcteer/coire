# Feature Specification: Chat Parser, File and Vision Observability

**Feature Branch**: `feat/014bj-chat-observability`  
**Parent acceptance**: [014 Chat Web UI](../014-chat-web-ui/spec.md)

Operators can distinguish native Chat engine stream parse failures, private file processing outcomes and bare VLM load time on the Coire Chat dashboard. Parser and processing failures raise baseline alerts. Metrics contain fixed reason/outcome/backend labels and no user content or identifiers.

Acceptance: a malformed or incomplete native stream increments one bounded parser failure reason; the dashboard references emitted parser, worker and backend-labeled engine metrics; alert rules cover parser and worker failures.
