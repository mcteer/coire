# Implementation Plan: Studio image worker copy preflight

**Spec**: [spec.md](spec.md)

## Constitution Check

| Principle | Compliance |
| --- | --- |
| I/II | Only coire-node resolves the local bare-engine copy; core has no model path. |
| II-a | No new service or container. |
| III | Add slug to shared `ImageWorkerLoadRequest`; OpenAPI freshness checked; route exposure follows. |
| IV | Strict slug and no-symlink checks; no caller path or network pull. |
| V | Exact admin manifest before a model can be loaded. |
| VI | Worker load telemetry follows the launch path; this pure preflight has no new running path. |
| VII | Forged-tree and contract tests before code. |

## Approach

Use the existing Store manifest and full checksum verifier. Scan the local copy with `lstat` first and refuse symlinks, special files and unsafe extensions, including inside bookkeeping paths. Bind `request.slug` to the Store path and `request.manifest_sha256` to the canonical manifest. Refuse any runtime version other than the pinned mflux 0.20.0. Return the path for the subsequent node-owned process launch.
