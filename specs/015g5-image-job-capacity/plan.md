# Implementation Plan: Image job capacity reservation

**Spec**: [spec.md](spec.md)

## Constitution Check

| Principle | Compliance |
| --- | --- |
| I/II/II-a | API DB and disk-capacity accounting only; no model on core. |
| III | Existing quota schema and error contracts; no new wire shape. |
| IV | Called only by later authenticated admission; no public route here. |
| V | Does not name or acquire a model. |
| VI | No standalone request path; admission will emit spans and fixed labels. |
| VII | Counter/limit tests before implementation. |

## Approach

Reuse the global advisory lock and owner/global row lock order. Validate every limit and disk floor before any counter mutation. Store a worst-case hold in both rows. Keep daily consumed and held counts distinct; at UTC rollover clear consumed, retain held. Callers must guard lifecycle transitions on the locked job row in the same transaction so counter helpers execute once.
