# Feature Specification: Fenced Studio image job executor

**Feature Branch**: `feat/015e10-image-worker-job`
**Parent**: `specs/015-image-generation/` (part of T029/T032/T039)
**Dependency**: draft PR #79

## Goal

Connect the resident txt2img pipeline to canonical output writing for one fenced job attempt in private Studio scratch. Process supervision and network transfer follow separately.

## Acceptance

1. Validate that a typed worker run refers to the loaded instance, model, manifest and runtime. Reject nonempty inputs and expired deadlines before creating scratch.
2. Create an exclusive 0700 attempt directory under a node-owned 0700 root. Write each output as `<index>.png` via the canonical writer; return only internal output records with index, path, recipe and digests. Replaying the same attempt cannot overwrite outputs.
3. Throttle intermediate progress to four per second per job while always reporting each output's final denoising step. Check the deadline at every callback.
4. On any generation/write failure, close images and remove the attempt directory and its partial files. Tests use fake pipeline outputs; no native engine or Studio is run.
