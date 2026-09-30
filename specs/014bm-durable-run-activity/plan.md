# Implementation Plan: Durable Coding Activity Events

**Branch**: `feat/014bm-durable-run-activity` | **Parent**: [014 plan](../014-chat-web-ui/plan.md)

Extend the strict Chat event union with content-free `run.activity` and final `run.activity_status` payloads and regenerate API/TS types. Add a reversible migration for the last-activity sequence and final state on Chat turns. Implement the node-client page method and one persistence collector that locks the conversation and turn, checks the run identity, deduplicates by sequence, appends events and advances the durable sequence in one transaction. Poll while the scheduler's WAIT command blocks, perform a final drain before REMOVE or KILL, and keep ordinary MCP runs unaffected. Test restart/dedup, ownership and finalization, then run API/scheduler/image gates.

## Constitution Check

| Principle | Check |
| --- | --- |
| I/II | Studio node remains the only harness/container owner; core handles metadata only. |
| III | New event payload is a strict `coire-core` model; OpenAPI and browser types are regenerated. |
| IV/V | Run-to-turn ownership and durable sequence are checked before private event publication. |
| VI | Collector spans, fixed-outcome metrics and content-free logs cover failures. |
| VII | Contract, migration, scheduler and image gates cover the new path. |

No new dependency or architecture deviation. One reversible migration adds the sequence cursor and final state.
