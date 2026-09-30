# Implementation Plan: Opt-in Private File Worker Deployment

**Branch**: `feat/014ab-file-worker-deploy` | **Parent**: [014 plan](../014-chat-web-ui/plan.md)

Build a separate pinned multi-stage distroless image with the locked worker-only dependency tree and one Uvicorn process. Seed private volume target ownership as UID 65532 in the image. Use a credentialed Python loopback health probe. Add a `chat-files` Compose profile so partial 014 work does not start in the lean control plane. The worker joins only the new internal scheduler processing network and existing internal telemetry network; it has no database or published port. Attach generated originals read-only and derivatives writable. Generate a dedicated Keychain token and include it in staged secret generations, profile preflight and bounded cleanup. Extend CI image build, policy, CRITICAL CVE and SBOM matrix.

## Constitution Check

| Principle | Check |
| --- | --- |
| II/II-a | Separate one-process non-root distroless CPU image; no model, harness, shell, package manager or public port. |
| III | Health uses the shared strict response contract; no new public wire shape. |
| IV | Dedicated mounted secret, internal network and read-only original volume. |
| VI | Worker retains its processing span/counter/logs; joins existing internal telemetry network. |
| VII | Compose/secret tests, native image build/policy, vulnerability scan and SBOM evidence. |

No constitution exception or dependency. The additional scheduler network is the new per-concern network specified by parent 014; existing edge/database/socket networks remain unchanged.
