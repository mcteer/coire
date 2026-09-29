# Feature Specification: Vision Node Hardening

**Feature Branch**: `feat/014bv-vision-node-hardening`
**Parent acceptance**: [014 Chat Web UI](../014-chat-web-ui/spec.md)

Before a node starts a registry-selected bare VLM, it must use a verified local copy, strip every Hub credential from the engine environment, reserve capacity, and retain bounded cache/concurrency flags. Cancellation and re-adoption must keep the backend identity. Node contract tests must cover these safety and lifecycle boundaries without starting Metal on core. Parent T052/T053 remain open until a real Studio tiny-model lifecycle acceptance passes.
