# Feature Specification: Populated Chat Migration Proof

**Feature Branch**: `feat/014k-chat-migration-proof`  
**Parent acceptance**: [014 Chat Web UI](../014-chat-web-ui/spec.md)

## Requirements

- FR-K1: An automated PostgreSQL test upgrades the existing schema to migration 0014, seeds a model and user, upgrades to 0015, and proves backend defaults preserve the older model.
- FR-K2: The test populates chat content, attempts downgrade and proves the guard leaves all chat tables and rows intact.
- FR-K3: After deleting chat content, downgrade succeeds and removes the chat tables/backend columns while retaining older model data; re-upgrade succeeds.
- FR-K4: The test uses only an explicit disposable test database and never targets a configured production database. Normal unit tests remain engine/database independent.

## Independent acceptance

Run the test against a disposable local PostgreSQL instance, plus normal migration unit tests and Python gates.
