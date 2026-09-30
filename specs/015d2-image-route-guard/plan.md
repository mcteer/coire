# Implementation Plan: Audited image route and owner guard

**Spec**: [spec.md](spec.md)

## Constitution Check

| Principle | Compliance |
| --- | --- |
| I/II/II-a | API guard only, no model or container. |
| III | Safe ImageForbidden/ImageNotFound RFC 9457 contracts. |
| IV | Refusal audit and owner-only ordinary reads, including admins. |
| V | No acquisition behavior. |
| VI | Existing audit span/logs; image route metrics connect when routes ship. |
| VII | Contract tests before dependency implementation. |

## Approach

Use the prior preflight/live resolver in a route dependency. Commit refusal audit in a separate session after rejecting a request, with fixed action/reason and no prompt or credentials. Add owner-only lookup helpers for jobs, inputs and published outputs. Later route/service code integrates these helpers and writes explicit admission/completion audits.
