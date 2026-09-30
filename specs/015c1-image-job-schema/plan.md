# Implementation Plan: Private image job and preset persistence

**Spec**: [spec.md](spec.md)

## Constitution Check

| Principle | Compliance |
| --- | --- |
| I/II/II-a | Schema only; no engine, core model load or container. |
| III | Database state matches typed core image contracts. |
| IV | Owner and audit-relevant authorization fields are durable; no route enabled. |
| V | Registry IDs remain UUID foreign keys. |
| VI | No executable route yet; service telemetry follows in T013. |
| VII | Schema tests precede implementation; migration guarded on downgrade. |

## Approach

Add job, event, preset and immutable-revision tables with relational uniqueness/checks. Keep state transitions in the later service transaction; schema supplies a version and fence for compare-and-swap publication. Test metadata constraints and guarded migration behavior. Split remaining input/output/quota/lease persistence into following child PRs.
