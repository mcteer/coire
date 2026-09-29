# Implementation Plan: Provider-Agnostic Chat Routing

**Branch**: `feat/014bw-frontier-chat-routing` | **Parent**: [014 plan](../014-chat-web-ui/plan.md)

Add a strict core provider/model contract and one reversible registry migration. Admin registration records a provider kind, fixed official endpoint, provider model ID, capabilities and a secret slot; availability requires a mounted Keychain-sourced secret. The picker shows source/provider while preserving the same entitlement and publication filter. Gateway execution dispatches by registry target and translates provider streams into the existing Coire event/usage contract. Shared text canonicalization and Stop apply; public `/v1` keeps its OpenAI shape. Add mocked provider contract and cancellation tests, then run complete gates. Real paid API calls require operator-provided credentials and a bounded acceptance budget.

## Constitution Check

| Principle | Check |
| --- | --- |
| I/II | Studio engines remain bare; core only proxies HTTPS and hosts no weights or user harness. |
| II-a | No new service container is required; the existing API gateway owns provider I/O. |
| III | Strict core Pydantic registry, picker, admin and result shapes; `/v1` remains compatible. |
| IV/V | Admin-audited registration, entitlement filter, fixed provider hosts and Keychain-sourced credentials. |
| VI | Provider spans, metrics, structured logs, dashboard and alert added with the dispatch path. |
| VII | Contract, stream, Stop, entitlement and budget tests; migration reversible. |

No new dependency.
