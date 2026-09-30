# Implementation Plan: Verified File Publication

**Branch**: `feat/014ag-file-publication` | **Parent**: [014 plan](../014-chat-web-ui/plan.md)

Mount the private derived volume read-only in the API image. Add an API maintenance pass for `processed` file jobs. Lock owner and conversation, then job/attachment/reservation; revalidate persisted worker manifests and generated key relationships. Read each PNG via no-follow directory/file descriptors and compare bounded size, digest and IHDR dimensions. On success, set ready metadata, release unused derived quota, emit an attachment event and commit atomically. On failure, record a safe terminal status while retaining the reservation until content cleanup. Do not expose extracted text or derived bytes through public routes in this slice.

## Constitution Check

| Principle | Check |
| --- | --- |
| II | API verifies bounded bytes and headers only; native parsing stays in isolated worker. |
| III | Strict `coire-core` request/result/attachment/event contracts. |
| IV | Generated no-follow keys, owner locks, tombstone checks and scoped events. |
| VI | Publication span, metric and content-free logs. |
| VII | Manifest, storage, quota and publication tests plus repository gates. |

No constitution exception or dependency.
