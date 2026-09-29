# Implementation Plan: Admin Vision Acquisition Publication

**Branch**: `feat/014bt-vision-acquisition-publication` | **Parent**: [014 plan](../014-chat-web-ui/plan.md)

Route supported preconverted Idefics3 inspection through the existing admin workflow. Require an upstream recipe that matches source precision, propagate registry backend through the durable validate command, and verify validation result backend and visual capability before marking the variant validated. At final publication, recheck validation and both checksums before storing model backend and visual capability. Cover refusals and publication guards in contract and unit tests, then run the full gates.

## Constitution Check

| Principle | Check |
| --- | --- |
| I/II | Bare engine remains in node worker; core runs only scheduler and API. |
| III | Reuse strict core inspection and validation contracts; no new ad hoc wire shape. |
| IV/V | Admin-only audited acquisition and registry-selected backend; no caller model path. |
| VI | Existing acquisition spans, validation counter, dashboard and alert apply with backend attribute. |
| VII | Add contract tests before enabling visual submission and preserve text gates. |

No dependency or migration.
