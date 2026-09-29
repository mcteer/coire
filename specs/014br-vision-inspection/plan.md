# Implementation Plan: Preconverted Vision Inspection

**Branch**: `feat/014br-vision-inspection` | **Parent**: [014 plan](../014-chat-web-ui/plan.md)

Add an additive backend to the strict inspection result. Classify the pinned MLX-VLM Idefics3 family only when the repository is preconverted and has the required processor, tokenizer, configuration and safetensors files. Reject any request that would convert/dequantize a visual model. Keep an audited admin refusal for complete visual sources until node visual smoke, scheduler publication and measured limits are wired. Cover complete, missing, raw, unsupported and text cases. No caller supplies the backend.

## Constitution Check

| Principle | Check |
| --- | --- |
| I/II | Classification is metadata-only on core; only the node may later load the bare VLM. |
| III | The additive inspection field is in coire-core; OpenAPI/TypeScript regenerate together. |
| IV/V | The admin route audits and refuses visual downloads until its validated acquisition path exists; no user/Chat path downloads. |
| VI | Existing acquisition audit, spans and outcome metrics record typed refusals. |
| VII | Contract/unit tests and full gates precede completion. |

No dependency, migration or architecture deviation.
