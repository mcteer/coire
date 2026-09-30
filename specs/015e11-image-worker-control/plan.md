# Implementation Plan: Authenticated resident image worker control

**Spec**: [spec.md](spec.md)

## Constitution Check

| Principle | Compliance |
| --- | --- |
| I/II | Bare mflux pipeline remains in a node-owned Studio process. |
| II-a | No new container or service; this is a child process of coire-node. |
| III | Add output manifest fields to strict `ImageWorkerStatus`; all commands are coire-core models. |
| IV/V | Per-worker bearer, exact attempt fence and no caller path/model string. |
| VI | Existing generate telemetry wraps the job executor; status omits content. |
| VII | ASGI contract and fake-worker tests precede code. |

## Approach

Extend `ImageWorkerStatus` with typed output manifests. Add a FastAPI factory that receives an already verified and loaded pipeline from the later bootstrap. Require a long bearer on every route. Run the executor in `asyncio.to_thread` and keep a locked in-memory status for one active attempt; compare the entire run request on replay. The node process supervisor will later provide the secret, bind 127.0.0.1, persist PID/port and enforce hard cancellation.
