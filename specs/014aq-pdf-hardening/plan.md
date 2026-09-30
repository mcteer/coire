# Implementation Plan: PDF Failure and Watchdog Proof

**Branch**: `feat/014aq-pdf-hardening` | **Parent**: [014 plan](../014-chat-web-ui/plan.md)

Classify PDFium's typed password error separately from malformed PDF failures in the isolated worker. Add two small upstream PDFium fixtures with their redistribution license and fixed digests. Exercise extraction, encrypted refusal and the worker's `os._exit(124)` deadline in a subprocess. Render the password code as plain language in Chat, and run worker/browser/image gates.

## Constitution Check

| Principle | Check |
| --- | --- |
| II | CPU-only parser and process isolation; no model or Metal work on core. |
| II-a | Worker image policy and native import verified. |
| III | Existing strict worker status contract and stable error code. |
| IV | No content in worker errors or logs; private fixture only in tests. |
| VI | Existing worker refusal metric records this outcome. |
| VII | Real fixtures, subprocess deadline and repository/image gates. |

No constitution exception or new dependency. PDFium fixture source and licence are documented beside the test files.
