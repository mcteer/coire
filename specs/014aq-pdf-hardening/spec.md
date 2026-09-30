# Feature Specification: PDF Failure and Watchdog Proof

**Feature Branch**: `feat/014aq-pdf-hardening`
**Parent acceptance**: [014 Chat Web UI](../014-chat-web-ui/spec.md)

## Requirements

- FR-AQ1: A PDF that requires a password fails with a stable, safe `pdf_password_required` code. The browser explains that the PDF is password-protected; malformed PDFs retain the separate invalid-PDF code.
- FR-AQ2: Real PDFium fixtures prove page-attributed Unicode extraction and password refusal, alongside the existing scan, malformed, page-limit, image-bomb, metadata-stripping and atomic-output cases.
- FR-AQ3: An isolated test process proves the worker's native-call watchdog exits with status 124 when parsing stalls beyond its deadline. The normal test process must survive.
