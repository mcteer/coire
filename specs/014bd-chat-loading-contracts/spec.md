# Feature Specification: Chat Load Contract Regression

**Feature Branch**: `feat/014bd-chat-loading-contracts`  
**Parent acceptance**: [014 Chat Web UI](../014-chat-web-ui/spec.md)

The picker keeps a published, entitled model selectable when its engine is evicted, and retains only a measured warm-up estimate. A starting engine with no measurement reports a null estimate. The server exposes no invented rank, percent or ETA. Parent stream tests cover real placement queue/loading statuses and a safe load failure.

Acceptance: HTTP contract tests verify the loaded-to-cold transition, historic measurement and unknown estimate without scheduling any work.
