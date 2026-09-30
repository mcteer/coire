# Implementation Plan: Compatible Multimodal Contracts

**Branch**: `feat/014c-compatible-contracts` | **Parent**: [014 plan](../014-chat-web-ui/plan.md)

Extend the existing strict shared models with additive defaults. Keep the OpenAI `ChatMessage` extra allowance needed for tool calls, but make content parts discriminated and validate bounded inline data. Put `EngineBackend` in `registry.py`; add backend and measured limits to server-supplied projections and node engine shapes. Keep visual measurements out of admin `CapabilityProfileUpdate`. Harness visual inputs reference trusted assets, never paths/URLs. Activity is metadata only. Tests precede changes. No DB/API/engine runtime behavior is introduced here.

## Constitution Check

| Principle | Check |
| --- | --- |
| I, II, II-a | No process/container changes; backend merely discriminates future bare engines. |
| III | Every new wire field/type is in coire-core, with compatibility tests. |
| IV | Trusted references carry opaque IDs; activity excludes content. |
| V | Model path remains registry resolved; visual measurement is server-controlled. |
| VI | Runtime telemetry is handled in later 014 child changes. |
| VII | Core/compatibility tests and type gates must pass. |

No constitution exception, dependency, or migration.
