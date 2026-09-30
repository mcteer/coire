# Feature Specification: Reclaim Failed File Quota

**Feature Branch**: `feat/014am-file-quota-reclaim`
**Parent acceptance**: [014 Chat Web UI](../014-chat-web-ui/spec.md)

## Requirements

- FR-AM1: After a failed file job's derived output is verifiably purged, API maintenance reduces its active quota reservation to the original plus any previously published derived bytes. It never shrinks before the worker purge marker.
- FR-AM2: An owner-requested retry (inspect or PDF render) re-reserves the full worst-case derived bound under the owner/conversation quota locks. If another upload used the freed capacity, the retry fails clearly and leaves the file in failed state.
- FR-AM3: Reclaim and retry are safe under concurrent API instances; the same reservation row is locked and only the newest job may claim it. Tests cover release, re-reserve, refusal and tombstones.
