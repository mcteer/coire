# Feature Specification: Failed and orphan image input cleanup

**Feature Branch**: `feat/015h6-image-input-cleanup`
**Parent**: `specs/015-image-generation/` (part of T026/T039/T054)
**Dependency**: draft PR #70

## Goal

Remove failed recipe input bytes and uncertain-commit orphan files without releasing storage capacity before physical deletion or deleting any committed owner input.

## Acceptance

1. The API maintenance pass finds failed, nonpurged recipe inputs. It locks quota and input state, removes only the generated regular file under the configured image input root, then releases its held bytes and records `purged_at` in one transaction. A missing file is retry-safe.
2. A cleanup failure keeps the row and hold for retry. Owner status remains `failed` with a content-free safe error after purge.
3. An orphan sweep removes only canonical generated UUID files and temporary `.uploading` files older than one hour, after checking no committed input row references the UUID. It never follows symlinks or removes an active/ready/processing input.
4. Tests cover unsafe names/symlinks, missing files, quota-release order, retry after unlink-before-commit, and row-preserving orphan checks. The default image admission flag stays false.
