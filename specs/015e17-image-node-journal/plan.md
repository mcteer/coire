# Implementation Plan: Durable Studio image job journal

**Spec**: [spec.md](spec.md)

## Constitution Check

| Principle | Compliance |
| --- | --- |
| I/II | State resides on the Studio node; no core model work. |
| III | Existing coire-core command and status models define journal input/output. |
| IV/V | Journal has owner-only files, exact attempt/fence binding and no caller paths. |
| VI | Later dispatch slice records job metrics; journal errors are safe and content-free. |
| VII | Pure local restart/conflict tests precede node route wiring. |

## Approach

Write one owner-only JSON record per ULID under the configured node state root, using an atomic replace and directory fsync. Persist the immutable request and current typed `NodeImageJob`. Never overwrite an unreadable record. Keep node transport and worker dispatch in the next reviewable slice.
