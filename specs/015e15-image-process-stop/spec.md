# Feature Specification: Fenced image worker process stop

**Feature Branch**: `feat/015e15-image-process-stop`
**Parent**: `specs/015-image-generation/` (part of T024/T030/T039/T041)
**Dependency**: draft PR #84

## Goal

Stop the exact resident Studio image process within the configured five-second grace, preserving memory reservation until death is proven.

## Acceptance

1. An unload command must match the resident instance. A live exact PID/create-time/bootstrap identity receives TERM and then KILL if still alive. No signal is sent to a reused PID or an unknown process.
2. Death is distinguished from uncertain inspection errors. A confirmed dead process releases its memory reservation and removes only its private launch/secret/state files; generated scratch remains for receipt-aware cleanup.
3. If signal, inspection or state cleanup cannot prove a safe stop, return a stable failure and retain the reservation and record. A replay in the same supervisor after confirmed cleanup returns the stopped result.
4. Fake-process tests cover graceful TERM, KILL escalation, PID reuse, inspection uncertainty and cleanup failure. No real Studio process is signalled.
