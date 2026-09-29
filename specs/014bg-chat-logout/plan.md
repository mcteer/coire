# Implementation Plan: Explicit Browser Sign-out

**Branch**: `feat/014bg-chat-logout` | **Parent**: [014 plan](../014-chat-web-ui/plan.md)

Add a narrow draft-clear helper using the verified `/me` owner ID. Render one sign-out link in the shared Chat/admin shell with a same-origin Cloudflare Access logout target. Clear drafts in the click handler before browser navigation. Keep expiry and identity-change paths separate: reauthentication retains same-tab drafts for the same owner. Test the storage helper and shell action, then run web and image gates.

## Constitution Check

| Principle | Check |
| --- | --- |
| III/IV | No wire changes; logout uses the edge's existing same-origin route and a verified owner ID. |
| II/V | No model or harness behavior changes. |
| VI | Existing edge/session and API authentication logs remain authoritative. |
| VII | Storage, shell, web suite and image policy gates cover the change. |

No new dependency or architecture deviation.
