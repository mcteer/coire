# Feature Specification: Opt-in Private File Worker Deployment

**Feature Branch**: `feat/014ab-file-worker-deploy`  
**Parent acceptance**: [014 Chat Web UI](../014-chat-web-ui/spec.md)

## Requirements

- FR-AB1: Build the worker as its own non-root, read-only-root-compatible, shell-free Linux arm64 image with exact locked Python/PDFium/Pillow packages and no model engine or user harness.
- FR-AB2: Run exactly one worker process under 512 MiB/1 CPU, drop all capabilities, keep it off published ports, and connect only scheduler and worker to a new internal processing network. Mount generated originals read-only and derivatives writable on private named volumes.
- FR-AB3: Materialize a dedicated worker token from Keychain into a Compose secret mounted only in scheduler and worker. Enabling the `chat-files` profile without that token fails before deployment; the default lean profile stays available while Chat admission is disabled.
- FR-AB4: The container's local health probe authenticates to the worker. Image policy checks bare-parser native import, no harness/model package, image hardening, vulnerability scan and SBOM generation.
- FR-AB5: Credential generations and `coire-down` cleanup include the new secret without touching unrelated credentials or private data volumes.

## Independent acceptance

Compose topology and credential-generation tests pass. Native arm64 image builds, image policy/native import, disposable named volume write, authenticated local health probe, CRITICAL vulnerability scan and SPDX SBOM pass on a local disposable Docker context. The deployment remains opt-in until API upload/volume ownership, scheduler dispatch and blob purge are complete.
