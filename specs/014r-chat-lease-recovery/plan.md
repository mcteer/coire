# Implementation Plan: Plain Chat Lease Recovery

**Branch**: feat/014r-chat-lease-recovery | **Parent**: [014 plan](../014-chat-web-ui/plan.md)

Assign each admitted text turn a process ID and 30-second lease. A generator heartbeats the persisted expiry every five seconds. API lifespan runs a bounded five-second sweep when Chat is enabled. Each candidate is rechecked after taking the conversation and turn locks; the winner atomically saves a terminal event and clears the active turn. Others see a terminal state and skip it. No generation retry occurs.

## Constitution Check

| Principle | Check |
| --- | --- |
| I, II | Reconciliation only changes gateway state; core never loads a model. |
| III | Existing shared terminal event contract. |
| IV | Maintenance is internal and does not expose private content. |
| VI | Recovery span, outcome counter, structured turn ID log, dashboard and failure alert. |
| VII | Idempotency and partial-output unit tests plus regression gates. |

No constitution exception or dependency.
