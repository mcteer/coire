# Implementation Plan: Chat Runtime Prerequisites

**Branch**: `feat/014b-chat-runtime` | **Parent**: [014 plan](../014-chat-web-ui/plan.md)

Pin `react-markdown==10.1.0` (MIT) in both web locks, `pypdfium2==5.13.0` (Apache-2.0/BSD-3-Clause plus PDFium notices) and `Pillow==12.3.0` (MIT-CMU) in worker-only dependencies, and `mlx-vlm==0.7.3` (MIT) in Darwin node dependencies. Add the worker to the uv workspace and update the architecture/ADR with planned isolation and failover boundaries. Lock resolution and policy inspection precede checkoff. Actual worker image, node installation and Markdown rendering remain in later child changes.

## Constitution Check

| Principle | Check |
| --- | --- |
| I, II | Only the node declares the bare vision engine; core's worker gets CPU parsers only. |
| II-a | Architecture records a dedicated worker container, without implementing it yet. |
| III, IV, V | No wire/auth/acquisition behavior changes. |
| VI | No runtime path changes; later worker/engine children carry telemetry. |
| VII | Locks and architecture checks are the child's acceptance gate. |

No constitution exception or migration.
