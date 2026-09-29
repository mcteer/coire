# Implementation Plan: Chat Authorization and Eligibility

**Branch**: `feat/014e-chat-authorization` | **Parent**: [014 plan](../014-chat-web-ui/plan.md)

Add a narrow `CurrentChatUser` dependency and owner lookup to `coire_api.auth`; keep the existing admin and MCP guards. Configure one exact browser origin with a fail-closed empty default. Share one entitlement predicate from registry service across list and gateway resolution, and use it without admin bypass in future native Chat. Test before implementation. No new route, network setting or migration in this child.

## Constitution Check

| Principle | Check |
| --- | --- |
| I, II, II-a | No engine/container change. |
| III | Principal/model contracts already exist in coire-core. |
| IV | Verified user binding, owner check, exact Origin and scope gates. |
| V | Published/ready/entitled registry IDs only; no acquisition path. |
| VI | Existing auth telemetry is preserved; new routes instrument later. |
| VII | Auth/eligibility regressions run before route work. |

No constitution exception, dependency or migration.
