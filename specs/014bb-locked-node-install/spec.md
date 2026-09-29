# Feature Specification: Locked Native Node Install

**Feature Branch**: `feat/014bb-locked-node-install`  
**Parent acceptance**: [014 Chat Web UI](../014-chat-web-ui/spec.md)

## Requirements

- FR-BB1: Stage the exact locked macOS arm64/Python 3.13 node dependencies as locally verified wheels alongside the versioned core and node wheels. Reject a missing, incompatible or digest-mismatched wheel before publication.
- FR-BB2: Install only from that wheelhouse into a new digest-named environment. Preserve an existing active environment and refuse to mutate an incomplete environment at the same digest.
- FR-BB3: Import both bare engines and smoke their command entry points before atomically activating the new environment. A failed smoke leaves the prior active link in place.
- FR-BB4: Operators can stage locally without contacting a Studio and have documented activation/rollback steps.

## Acceptance

- A disposable local prefix installs the locked wheelhouse offline, reports installed `mlx-vlm`, `mlx-lm` and `coire-node`, and activates only after smoke.
- Unit tests cover successful activation, failed smoke and a rejected wheel digest.
- Parent T060 may close; tiny-model, cluster rollout and visual acceptance stay parent T074/T075 gates.
