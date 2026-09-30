# Feature Specification: Chat Runtime Prerequisites

**Feature Branch**: `feat/014b-chat-runtime`  
**Parent acceptance**: [014 Chat Web UI](../014-chat-web-ui/spec.md)

## Purpose

Prepare exact, reviewable package inputs and architecture boundaries for 014's browser Markdown, CPU-only file worker, and bare Studio vision backend. This child introduces no serving behavior.

## Requirements

- FR-B1: The web Markdown renderer is pinned in npm and pnpm lock graphs and stays inert until 014's safe renderer is implemented.
- FR-B2: The worker is an independent Python package, with only CPU PDF/image parsing dependencies. Its dependency graph must not pull model engines onto core.
- FR-B3: The node package declares the bare vision engine only on Darwin, with an exact pin. The later node installer must stage this lock in a versioned environment before activation.
- FR-B4: Architecture and ADR identify the new worker/engine responsibilities, private networks/volumes, and text-only failover boundary without widening existing exposure.

## Independent acceptance

Both web package managers resolve consistent exact versions, `uv lock --check` passes, worker and node platform dependency separation is inspectable, and architecture/ADR checks match the parent 014 plan.
