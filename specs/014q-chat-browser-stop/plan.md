# Implementation Plan: Browser Stop for Plain Chat

**Branch**: feat/014q-chat-browser-stop | **Parent**: [014 plan](../014-chat-web-ui/plan.md)

Use the generated ChatTurn response type in the web API client. Show a Stop button whenever the selected conversation has an active turn. Keep generation connected after the owner Stop request so the persisted terminal and any partial answer arrive through the original stream. Refresh saved detail if the turn was already terminal when Stop reached the API.

## Constitution Check

| Principle | Check |
| --- | --- |
| III | Browser request/response types come from generated OpenAPI. |
| IV | Owner authority remains enforced by the API; the browser exposes no observer cancellation token. |
| VI | The API records Stop outcomes; browser displays persisted state. |
| VII | Browser interaction test covers a live partial answer and terminal Stop. |

No constitution exception or dependency.
