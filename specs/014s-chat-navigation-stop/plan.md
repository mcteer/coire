# Implementation Plan: Navigation Stop for Plain Chat

**Branch**: feat/014s-chat-navigation-stop | **Parent**: [014 plan](../014-chat-web-ui/plan.md)

Enable navigation after a turn is accepted. For a tab with the controlling stream, persist `navigation` Stop through the typed owner endpoint, abort its transport and advance the view generation so late events are ignored. A tab only viewing another process's active turn does not call Stop. At stream cleanup, read the durable Stop state before final usage and terminal persistence.

## Constitution Check

| Principle | Check |
| --- | --- |
| III | Navigation uses the generated Stop request/response contract. |
| IV | Only the owner endpoint changes cancellation state; viewer transport has no authority. |
| VI | Existing stopped terminal and usage outcomes are recorded. |
| VII | Browser and backend cancellation tests plus regression gates. |

No constitution exception or dependency.
