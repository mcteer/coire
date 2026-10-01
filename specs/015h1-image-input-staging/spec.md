# Feature Specification: Bounded image input staging

**Feature Branch**: `feat/015h1-image-input-staging`
**Parent**: `specs/015-image-generation/` (part of T012, T026, T049, T054)
**Dependency**: draft PR #64

## Goal

Provide a private, bounded staging primitive for image uploads. Generation inputs stop at 10 MiB; recipe-only PNGs stop at 64 MiB. The primitive never trusts the multipart filename, declared size or MIME type for storage paths and leaves no temporary bytes after failure.

## Acceptance

1. Staging reads in bounded chunks, enforces the purpose-specific limit on actual bytes, checks the declared byte count, computes SHA-256 and fsyncs before returning.
2. Storage uses a generated ID and exclusive no-follow file creation inside the configured image namespace. Publishing is an exclusive link, and failure/cancellation discards the temporary file.
3. A recipe file cannot be staged as a generation input simply by declaring a smaller size; actual size is authoritative.
4. Tests cover exact bounds, over-limit, forged count, empty upload, cancellation cleanup, and exclusive publication.
5. This slice supplies the storage primitive; authenticated upload routes, quota admission and file-worker processing remain disabled until later slices.
