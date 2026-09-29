# Implementation Plan: Chat File Admission Contracts

**Branch**: `feat/014ac-file-admission` | **Parent**: [014 plan](../014-chat-web-ui/plan.md)

Add strict upload/delete/process Pydantic models to `coire-core` and reuse one safe-basename validator with attachment projections. Strengthen the generated worker request contract to reject duplicate output IDs and pages on inspect. Keep model-only changes additive; the following child implements storage and routes.

## Constitution Check

| Principle | Check |
| --- | --- |
| III | All new metadata wire fields are strict shared Pydantic contracts. |
| IV | Caller paths and unsafe filenames are refused; generated worker output IDs are unique. |
| VII | Contract tests exercise accepted and rejected shapes before route implementation. |

No constitution exception or dependency.
