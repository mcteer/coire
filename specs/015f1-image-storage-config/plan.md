# Implementation Plan: Disabled image storage topology

**Spec**: [spec.md](spec.md)

## Constitution Check

| Principle | Compliance |
| --- | --- |
| I/II | No model or engine on core. |
| II-a | Existing containers; API-only blob volume, read-only file-worker original mount. |
| III | Settings map to previously typed core settings; no new wire shape. |
| IV | Default disabled, route-specific body bounds, no trust change. |
| V | No acquisition path. |
| VI | No executable image route yet; T013 follows. |
| VII | Topology tests before config, repository gates afterward. |

## Approach

Bind new image settings through Compose. Mount final blobs only in API. Give file worker image original/derived subpaths within existing volumes; later code will honor these settings. Add exact Nginx upload and internal transfer locations before the generic API proxy. Verify rendered topology and unchanged network/capability scope.
