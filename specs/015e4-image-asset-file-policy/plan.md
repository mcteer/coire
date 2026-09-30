# Implementation Plan: Image asset file policy

**Spec**: [spec.md](spec.md)

## Constitution Check

| Principle | Compliance |
| --- | --- |
| I, II | Metadata validation and Studio node-owned acquisition helper only. |
| II-a | No container or service addition. |
| III | Existing `RepoInspection` and `ModelKind` contracts. |
| IV, V | Exact admin-only asset allowlist; no caller path reaches an engine. |
| VI | Parent admin acquisition will report safe refusal codes; helper logs no content. |
| VII | Unit tests before implementation and full workspace gates. |

## Approach

Validate the complete Hub sibling list before any image snapshot. Reject unsafe paths and duplicate names. Select only inert config/tokenizer/model-card files and safetensors; omit upstream pickle/executable/archive siblings from the exact download list. Require known pinned classifier facts from the parent research. Pass exact paths to `snapshot_download(allow_patterns=...)`; the future admin workflow must call this helper after licence and component checks.
