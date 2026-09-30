# Implementation Plan: Admin-imported image preset templates

**Spec**: [spec.md](spec.md)

## Constitution Check

| Principle | Compliance |
| --- | --- |
| I/II/II-a | Data-only templates; no engine/container. |
| III | Validate substituted JSON through `ImagePresetCreate`. |
| IV | Existing human-admin route and audit own import; no privilege fields. |
| V | Admin supplies registry UUID; no HF pull or acquisition action. |
| VI | Existing preset mutation audit/metric covers import. |
| VII | Template validation test and repository gates. |

## Approach

Add two compact JSON examples with a non-UUID `__MODEL_ID__` slot and a README that explains binding and POSTing through the admin route. Test that raw templates fail and substituted templates validate with no extra authority-bearing fields.
