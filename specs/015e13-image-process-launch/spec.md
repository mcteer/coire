# Feature Specification: Node-owned image process launch

**Feature Branch**: `feat/015e13-image-process-launch`
**Parent**: `specs/015-image-generation/` (part of T024/T030/T033)
**Dependencies**: draft PR #82 and native runtime draft PR #31

## Goal

Have coire-node reserve memory and launch one offline image child from a verified local copy, recording its PID, create time, port and reservation durably before acknowledging load.

## Acceptance

1. A node supervisor accepts only `ImageWorkerLoadRequest`, verifies its exact local Store manifest, and refuses over-budget or concurrent different loads. Identical active load returns the same starting/ready status.
2. The supervisor uses a dedicated loopback port and an explicit argv into the installed native Python environment. Its child environment contains no Hub token and requires offline flags. Private 0700 state holds exclusively created 0600 config and bearer-token files.
3. After spawn, the supervisor obtains PID create time, persists a strict process record atomically, and only then returns `starting`. Spawn or persistence failure kills the child and does not leave a live untracked reservation.
4. Tests fake subprocess/psutil and local manifests; no native engine or Studio is run. Readiness, re-adoption, hard cancellation and node routes follow separately.
