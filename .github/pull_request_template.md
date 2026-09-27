## Spec and scope

- Spec: `specs/NNN-slug/spec.md`
- Plan and tasks reviewed: [ ]
- User-visible behavior and rollback path documented: [ ]

## Validation

- [ ] Unit and contract tests
- [ ] Integration test (or explain why not applicable)
- [ ] `uv run ruff format --check && uv run ruff check`
- [ ] `uv run mypy apps/ packages/`
- [ ] Web tests/lint and generated OpenAPI/TypeScript types (if applicable)
- [ ] Production images built, policy checked, scanned, and SBOMs generated (if applicable)

## Dependencies and licences

List every dependency added or upgraded, the reason, exact pinned version, and licence:

| Dependency | Version | Reason | Licence |
| --- | --- | --- | --- |
| _none_ | — | — | — |

## Constitution check

Explain how this change complies with each principle, or link the ADR/exception that closes it.

- **I. Bare engines:**
- **II. Core hosts no models / one service, one container:**
- **III. Contracts first:**
- **IV. Zero implicit trust:**
- **V. Models are data; only admins acquire them:**
- **VI. Observable or it doesn't ship:**
- **VII. Spec-driven, test-gated:**

## Operational evidence

- Runbook/ADR updated: [ ]
- Dashboard and alert updated (if applicable): [ ]
- Real-cluster evidence or explicit blocker recorded: [ ]
- Secrets, model weights, datasets, and generated images excluded: [ ]
