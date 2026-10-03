# Implementation Plan: Private recipe parsing handoff

**Spec**: [spec.md](spec.md)

## Constitution Check

| Principle | Compliance |
| --- | --- |
| I/II/II-a | CPU-only isolated file worker; no inference wrapper or core model work. |
| III | Request and result live in `coire-core` and are strict Pydantic models. |
| IV | Private token and generated ID only; no caller path. |
| V | Recipe is untrusted data; this route cannot acquire models. |
| VI | Fixed-label span, counter and content-free error codes. |
| VII | Contract tests before implementation. |

## Approach

Add one private endpoint to the existing file-worker container. It validates a typed request, rejects concurrent active processing, calls `parse_recipe_png` in a thread with a bounded timeout, and verifies size and digest from the same descriptor. The caller will persist the result only after owner and input identity checks in a later slice.
