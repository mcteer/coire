# Implementation Plan: Bare Vision Engine Ownership

**Branch**: `feat/014ay-vision-node` | **Parent**: [014 plan](../014-chat-web-ui/plan.md)

Use the predeclared `EngineStartRequest.backend` and the pinned Darwin-only `mlx-vlm==0.7.3`. Build a separate fixed `python -m mlx_vlm.server` argv from the verified node store path, with one sequence and one vision cache entry by default. Strip remote-code trust and credentials from the subprocess environment. Carry the backend through the manager's durable record and process discovery; reject mismatched duplicate starts. Add a reversible `engine_processes.backend` column with a text default for existing rows. Pass the registry backend from each existing control-plane load path and keep admin/reconciler projections honest. Retain the generation-based readiness probe and memory estimate admission. Real tiny-model and visual request validation remain later 014 gates.

## Constitution Check

| Principle | Check |
| --- | --- |
| I | Bare `mlx_vlm.server`, no inference wrapper or public engine route. |
| II | Only Studio coire-node starts the process; core sends typed control requests. |
| III | Existing core backend fields drive node requests and status; migration 0019 persists the projection. |
| IV | Local verified store path, no inherited credentials or remote-code trust. |
| V | Backend is registry-selected; callers cannot provide an engine path. |
| VI | Existing node engine spans, logs and metrics cover the lifecycle. |
| VII | Mocked node contract, full regression, local image policy/scan; actual tiny-VLM gate remains open. |

No constitution exception or new dependency; the exact pinned VLM package and license were accepted in parent ADR-0008.
