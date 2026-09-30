# Feature Specification: Owner Stop for Plain Chat

**Feature Branch**: feat/014p-chat-stop  
**Parent acceptance**: [014 Chat Web UI](../014-chat-web-ui/spec.md)

## Requirements

- FR-P1: The owner can request Stop for an active plain-chat turn. Repeated Stop is idempotent; missing, foreign, deleted and cross-conversation IDs have a uniform 404.
- FR-P2: Stop records a durable stop-requested status before returning. The original generator sees the request across API processes within one second, closes its upstream stream and releases its engine slot/lease; cold loads are not canceled for other waiters.
- FR-P3: A stopped turn preserves already persisted answer text, emits a terminal stopped event and accounts usage once. Terminal and Stop races return the committed terminal outcome.
- FR-P4: Read-only observers and duplicate POST followers have no cancellation authority. Navigation/browser explicit Stop can use the same owner endpoint.

## Independent acceptance

Contract tests cover owner/idempotent/terminal/cross-parent behavior; unit tests cover active and cold cancellation, upstream close and once-only usage. Existing gateway and Chat tests remain green.
