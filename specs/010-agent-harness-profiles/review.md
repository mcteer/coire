# Review: Agent Harness and Capability Profiles

## Automated evidence

- Python: 559 passed, 2 skipped; strict mypy passed across 149 source files; Ruff clean.
- Composed integration: 5 passed, including an authenticated `/v1` harness call against the
  deterministic tiny-model topology and pre-call write refusal.
- Web: Vitest, ESLint, TypeScript build, generated OpenAPI, and generated TypeScript schema pass.
- Images: `coire-agent:010` and `coire-agent-ops:010` build on arm64, pass all seven image-policy
  rules, and runtime inspection proves `coire_ops` is absent from the user image.
- Supply chain: SPDX JSON SBOMs generated under a temporary build-artifact directory; Trivy found
  zero CRITICAL vulnerabilities in either image.
- Migration: a fresh composed PostgreSQL volume migrated through `0010_harness_evaluations`; the
  enum declaration was corrected to avoid implicit duplicate type creation.

## Manual real-cluster gate

T039 / SC-001 remains deliberately open. Repository policy forbids this development environment
from launching models or Docker workloads on the real Studios. An operator must run
`coire eval harness <variant-id>` for three open-weight variants on the cluster and append the three
scorecard IDs here before merge. This is evidence collection, not remaining implementation.

## Constitution

- I: all inference goes through the gateway; the harness has no engine client.
- II / II-a: user and ops harnesses are separate minimal images; only ops contains an admin client.
- III: harness, target, submission, and scorecard wire shapes live in `coire-core`; generated
  OpenAPI and TypeScript types are refreshed.
- IV: evaluation routes require admin identity and mutations append audit records.
- V: strategy and reasoning policy derive from registry capability data; verification is stored on
  and checked for the exact variant.
- VI: spans, bounded metrics, dashboard panels, and failure/regression alerts ship together.
- VII: unit, contract, deterministic composed integration, image-policy, and security gates pass.
