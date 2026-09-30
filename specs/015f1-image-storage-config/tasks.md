# Tasks: Disabled image storage topology

- [X] C001 Add failing topology and route-body-bound tests.
- [X] C002 Wire disabled image settings, private volume and narrow ingress limits.
- [X] C003 Run Compose, Python and web checks; document rollout and rollback.

## Verification

- Focused topology tests: 3 passed after expected initial failures and updated three-route assertion.
- Full Python suite: 1,244 passed, 142 existing conditional skips.
- Strict mypy on 495 files, Ruff, OpenAPI freshness, web 93 tests/lint, production and integration Compose render, and Nginx syntax passed.
- API image built. A fresh local named volume was writable by the non-root container under read-only rootfs, then removed. No Studio was contacted.
- Critical image scan remains blocked by inherited PyJWT 2.13.0 CVE-2026-102268 (fixed in 2.14.0); the scan gate was not altered.
