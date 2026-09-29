# Implementation Plan: Tiny Model Local Acceptance

**Branch**: `feat/014bu-vision-local-acceptance` | **Parent**: [014 plan](../014-chat-web-ui/plan.md)

Add a composed integration test that submits the pinned tiny visual repo through the admin API and verifies a Linux node's missing native engine fails closed. Run it alongside the existing tiny text admin acquisition test in isolated Compose. The current `coire-core.lab` host must never load model weights or start Metal work. Native acceptance uses the real Studios through Coire's admin acquisition and node paths, as directed by the operator. Do not fetch from Hugging Face outside the admin path. Update the evidence ledger with the outcome and remaining gaps.

## Constitution Check

| Principle | Check |
| --- | --- |
| I/II | The disposable Linux topology uses fake text validation and fails closed for vision; native smoke uses Studio node jobs through Coire, never core. |
| III/V | Admin and node contracts select the registry model; no caller path reaches an engine. |
| IV | Admin API audits acquisition and publishes only after validation and replication. |
| VI | Existing acquisition telemetry records the failing and passing local paths. |
| VII | Composed contracts plus real local tiny-model smoke gate acceptance. |

No dependency or migration.
