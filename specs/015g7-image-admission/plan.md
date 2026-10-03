# Implementation Plan: Durable image job admission transaction

**Spec**: [spec.md](spec.md)

## Constitution Check

| Principle | Compliance |
| --- | --- |
| I/II/II-a | Core persists orchestration only; no engine process. |
| III | Shared request/job/event/snapshot models; no new untyped wire payload. |
| IV | Live user/key/entitlement recheck, per-owner key and mandatory audit commit. |
| V | Only ready Studio registry UUIDs and measured profiles reach the stored job. |
| VI | Existing `coire.api.image.submit` span and fixed-label metrics called by the eventual route. |
| VII | Replay and refusal tests before implementation; public route remains gated. |

## Approach

Acquire the transaction advisory lock before checking `(owner, idempotency_key)`. On replay, compare the stored canonical intent digest and recheck current authority, then return the existing receipt. On new work, load a direct or preset policy, resolve a basic spec, reserve capacity under the same lock, add job/event/audit and commit. Keep the state snapshot separate from runtime facts until the Studio reports them. The later route owns request telemetry and authenticates browser origin.
