# Feature Specification: Plain Chat Lease Recovery

**Feature Branch**: feat/014r-chat-lease-recovery  
**Parent acceptance**: [014 Chat Web UI](../014-chat-web-ui/spec.md)

## Requirements

- FR-R1: Each accepted plain-chat turn has a process-owned, expiring lease that its live generator renews without changing the public event cursor.
- FR-R2: After API process loss, a bounded maintenance pass commits one terminal `interrupted` event, or `stopped` for a pending Stop, retaining saved partial text and clearing the active turn.
- FR-R3: Recovery is idempotent across API processes and never restarts generation. A fresh lease or terminal turn is untouched.

## Independent acceptance

Unit tests cover expired running/stopping turns, repeated sweeps and fresh leases. Existing stream/admission contracts, full Python tests, strict types and Ruff pass.
