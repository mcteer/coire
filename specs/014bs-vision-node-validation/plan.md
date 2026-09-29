# Implementation Plan: Local Vision Validation

**Branch**: `feat/014bs-vision-node-validation` | **Parent**: [014 plan](../014-chat-web-ui/plan.md)

Extend the validation job's strict core request contract with a default text backend, then route visual jobs to a local-only processor check and runtime-generated-image smoke. Return the existing validation result shape with visual smoke success and noncomparable text perplexity. Keep the job worker process bound to node ownership and avoid inherited Hub credentials. Cover missing processor, invalid path, model failure and text compatibility with mocks before any real engine test.

## Constitution Check

| Principle | Check |
| --- | --- |
| I/II | Only coire-node's job worker loads the bare VLM on a Studio or disposable local Mac. |
| III | Validation request/result shapes are strict core Pydantic contracts. |
| IV/V | Caller cannot provide a model path; node resolves a generated store slug and admin workflow owns admission. |
| VI | Existing node acquisition job spans, outcomes and logs apply with backend identity. |
| VII | Mock contracts and later local tiny-model smoke gate completion. |

No dependency or migration.
