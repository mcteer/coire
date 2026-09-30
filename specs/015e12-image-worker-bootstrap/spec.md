# Feature Specification: Offline Studio image worker bootstrap

**Feature Branch**: `feat/015e12-image-worker-bootstrap`
**Parent**: `specs/015-image-generation/` (part of T024/T029/T030/T033)
**Dependency**: draft PR #81 and native runtime draft PR #31

## Goal

Start the resident image control app from a node-created, private configuration file without inheriting Hub credentials or importing mflux before offline mode is set.

## Acceptance

1. The node-to-worker launch file is a strict coire-core model containing only a registry-resolved load request, local Store/scratch paths, loopback port and token file path. It cannot include prompts or arbitrary code targets.
2. Bootstrap reads at most 16 KiB from a regular, owner-only 0600 config file and an owner-only 0600 token file. It rejects symlinks, wrong owner, weak/oversized secrets and invalid paths before a native import.
3. Bootstrap strips Hub credentials, sets offline environment flags, verifies the exact local copy through the existing pipeline loader, and serves the authenticated app only on 127.0.0.1.
4. Tests inject fake modules and local files; no mflux, model or Studio starts. Node launch, PID persistence and hard cancellation follow separately.
