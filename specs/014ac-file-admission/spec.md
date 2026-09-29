# Feature Specification: Chat File Admission Contracts

**Feature Branch**: `feat/014ac-file-admission`  
**Parent acceptance**: [014 Chat Web UI](../014-chat-web-ui/spec.md)

## Requirements

- FR-AC1: Multipart metadata uses a strict Pydantic `ChatUploadMetadata` with a safe basename and positive conversation revision. Caller paths, URLs and unknown fields are refused.
- FR-AC2: Explicit file deletion and processing requests have strict owner-visible revision, request identity, operation and selected-page bounds.
- FR-AC3: Private worker requests refuse duplicate output asset IDs and incompatible inspect-page selections before any native work.

## Independent acceptance

Core model tests cover unsafe filenames, unknown fields, revisions, selected pages and generated output identity. Ruff, strict mypy and the full Python suite pass. API upload routes and multipart parsing are in the next stacked child.
