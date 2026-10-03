# Implementation Plan: Fenced Studio image job executor

**Spec**: [spec.md](spec.md)

## Constitution Check

| Principle | Compliance |
| --- | --- |
| I/II | Execution exists only in coire-node and calls its bare mflux pipeline. |
| II-a | No new service/container. |
| III | Existing strict `ImageWorkerRunRequest` and load request bind the attempt. |
| IV/V | No caller path or model string; private fenced scratch and manifest match. |
| VI | Bounded content-free progress at 4 Hz; node stage telemetry follows process supervision. |
| VII | Fake-pipeline tests before code; native-model acceptance remains open. |

## Approach

Add `image_worker.py` with a single-attempt executor. Validate load/run identity and a private scratch root; derive a safe directory solely from the ULID, attempt and fence. Generate through the existing pipeline, write every PNG through canonical metadata, and return immutable internal records. Clean the directory on failure. Deadline checks run before generation and on every synchronized progress callback; node hard-kill supervision will later enforce a deadline outside callbacks.
