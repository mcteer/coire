# Implementation Plan: Vision Node Hardening

**Branch**: `feat/014bv-vision-node-hardening` | **Parent**: [014 plan](../014-chat-web-ui/plan.md)

Extend the node engine environment scrub to all known Hugging Face credentials. Require a matching local checksum manifest before launching a visual engine and preserve existing text startup behavior. Add mocked node contract cases for corrupt visual copy, budget refusal, bounded request options, cancellation, and re-adoption. Run static, contract, wheel and web gates; leave the native Studio test open.

## Constitution Check

| Principle | Check |
| --- | --- |
| I/II | Bare `mlx_vlm.server` argv remains owned by coire-node only. |
| III | Existing strict core engine request/status contracts bound caller options. |
| IV/V | Engine receives a verified local store path with no Hub credentials or remote code. |
| VI | Existing node engine spans, metrics and logs record backend and refusal. |
| VII | Mock lifecycle tests and later real tiny Studio acceptance. |

No dependency or migration.
