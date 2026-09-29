# Feature Specification: Navigation Stop for Plain Chat

**Feature Branch**: feat/014s-chat-navigation-stop  
**Parent acceptance**: [014 Chat Web UI](../014-chat-web-ui/spec.md)

## Requirements

- FR-S1: Leaving a conversation while this browser tab owns its active stream requests a durable navigation Stop before switching view, then closes the transport.
- FR-S2: A viewer of an active turn owned by another tab may navigate without stopping that other tab.
- FR-S3: Once navigation Stop is committed, disconnect cleanup saves a `stopped` terminal and once-only stopped usage even if the generator has not polled the Stop state.
- FR-S4: A failed navigation Stop leaves the user in the current conversation with an actionable error; a stale stream cannot update the newly selected conversation.

## Independent acceptance

Browser tests cover navigation Stop and draft navigation; a backend test closes the generator after a durable Stop and checks terminal/usage outcome. Web and Python gates pass.
