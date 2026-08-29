# Contributing to Coire

Thanks for contributing. Coire is a control plane for a small Apple Silicon inference cluster, developed spec-first with GitHub Spec Kit. This document is the process; `.specify/memory/constitution.md` is the set of binding engineering principles every change is checked against, and `AGENTS.md` is the condensed version for automated coding agents. Where they conflict, the constitution wins.

## 1. Ground rules

- Be respectful and assume good faith; the project follows the [Contributor Covenant](https://www.contributor-covenant.org/version/2/1/code_of_conduct/) v2.1 (`CODE_OF_CONDUCT.md`).
- Every change arrives as a pull request from a branch; nothing is committed directly to `main`.
- Every feature is a spec before it is code. Every bug fix is an issue before it is code.
- Commits are signed off (DCO) and, ideally, GPG/SSH-signed. By signing off you certify the [Developer Certificate of Origin](https://developercertificate.org/).
- Security issues are reported privately (see §9), never as public issues.

## 2. Branching model

`main` is always deployable and protected: no direct pushes, linear history (squash merges), required status checks, at least one approving review, and up-to-date-with-base enforced.

One branch per unit of work, named by kind:

| Kind | Branch name | Must reference | Created from |
|---|---|---|---|
| Feature | `feat/NNN-<slug>` | a Spec Kit spec in `specs/NNN-<slug>/` | `main` |
| Bug fix | `fix/<issue#>-<slug>` | a GitHub issue with reproduction | `main` |
| Hotfix (production-breaking) | `hotfix/<issue#>-<slug>` | an issue labelled `severity:critical` | `main` (or the release tag) |
| Chore / docs / CI | `chore/<slug>`, `docs/<slug>`, `ci/<slug>` | an issue if non-trivial | `main` |
| Spike / experiment | `spike/<slug>` | a short note in the PR; never merged, only learned from | `main` |

Rules:

- `NNN` is the Spec Kit feature number (`specs/007-observability/` → `feat/007-observability`). Spec Kit's `/speckit.specify` creates the branch and directory together; keep them in sync.
- One spec, one branch, one PR. If a spec is too large for a reviewable PR (> ~800 changed lines excluding generated files), split the *spec* into `NNNa`, `NNNb` via `/speckit.specify` rather than stacking unspecced PRs.
- A bug fix branch fixes exactly one issue. A fix that requires a design change becomes a spec.
- Rebase onto `main` before requesting review; never merge `main` into a feature branch. Force-pushing your own unreviewed branch is fine; rewriting a branch after review has started is not.
- Delete branches after merge (enabled automatically).

## 3. Spec-driven workflow (features)

1. **Propose.** Open a discussion or issue labelled `proposal` describing the problem and how it fits `docs/ROADMAP.md`. Maintainers confirm the feature number.
2. **Specify.** `/speckit.specify` → `specs/NNN-<slug>/spec.md`: user stories, functional requirements, acceptance criteria, out-of-scope. No technology choices here.
3. **Clarify.** `/speckit.clarify` until the spec has no `[NEEDS CLARIFICATION]` markers.
4. **Plan.** `/speckit.plan` → `plan.md` with the **Constitution Check** section filled in (each principle: complies / exception with ADR link), data model, contracts (OpenAPI fragments, Pydantic models), and research notes.
5. **Tasks.** `/speckit.tasks` → `tasks.md`. Tasks are ordered so tests precede implementation.
6. **Implement** on the feature branch, committing per task. Keep every commit green.
7. **Analyse.** `/speckit.analyze` before opening the PR; resolve inconsistencies between spec, plan, and code.
8. **Pull request** (§6). Spec and plan are reviewed *with* the code; a PR whose code diverges from its spec updates the spec in the same PR and explains why.

The spec directory is merged with the code and is the durable record of why the feature exists.

## 4. Bug-fix workflow

1. Open an issue using the *Bug* template: expected vs. actual behaviour, minimal reproduction, versions (`coire --version`, macOS, mlx/mlx-lm), logs or trace id, and severity (`critical` = data loss, security, or the cluster cannot serve; `major`; `minor`).
2. A maintainer triages and labels it. Don't start a fix on an untriaged issue unless it's obviously trivial.
3. Branch `fix/<issue#>-<slug>`. Write a failing test that reproduces the bug **first**, then the fix. The PR must include that test.
4. If the root cause is a design gap, stop and file a spec; link it from the issue.
5. PR title `fix(<scope>): …` and body `Fixes #<issue#>`.

Hotfixes follow the same path with expedited review (one maintainer) and are back-merged by the normal squash to `main`.

## 5. Commits

Use [Conventional Commits](https://www.conventionalcommits.org/en/v1.0.0/):

```
<type>(<scope>): <imperative summary ≤ 72 chars>

<body: what and why, not how; wrap at 100>

Spec: specs/007-observability
Fixes: #123
Signed-off-by: Your Name <you@example.com>
```

Types: `feat`, `fix`, `perf`, `refactor`, `test`, `docs`, `build`, `ci`, `chore`, `revert`. Scopes are the service or package names (`api`, `mcp`, `scheduler`, `node`, `agent`, `ops`, `web`, `core`, `compose`, `spec`). Breaking changes to a public contract (`/v1`, `/api/v1`, MCP tools, node-agent API, `TrainingSpec`/`ImageSpec`) use `!` and a `BREAKING CHANGE:` footer, and require a migration note in `docs/runbooks/upgrades.md`. Commit messages are linted in CI (commitlint) and drive the changelog (release-please).

Commit small and often on your branch; the PR is squash-merged with a curated message, so history on the branch may be messy but each commit should still build.

## 6. Pull requests

**Before opening:** rebased on `main`; `uv run ruff format && ruff check`, `mypy`, `pytest`, web `lint`/`test` all green locally; OpenAPI and TS types regenerated if a schema changed; a migration included if the DB changed; images build.

**Title:** conventional-commit style, becomes the squash commit.

**Body (template enforced):**

- *What / why* — two or three sentences.
- *Spec or issue* — link (`specs/NNN-…` or `#123`). No link, no review.
- *Constitution check* — list the principles touched and how the change complies, or link the ADR for an exception.
- *Testing* — what was run, including whether the integration suite ran against the tiny model and, for cluster-only features (sharding, training, images, upgrades), the manual verification performed on real hardware with results.
- *Observability* — spans/metrics/alerts/dashboard panels added.
- *Operational notes* — runbook updated? migration? config change? rollback path?
- *Screenshots* for UI changes (light and dark).

**Review:**

- One approving review from a maintainer is required; two for changes to auth, entitlements, the acquisition pipeline, node-agent process spawning, container hardening, or anything under `deploy/`.
- Reviewers check the spec as much as the code: does the implementation do what the spec says, no more, no less?
- Keep PRs focused; unrelated drive-by refactors go in their own `refactor/` PR.
- Respond to every comment (fix, or explain and resolve). Re-request review after pushing.
- Merge is squash-only, performed by a maintainer once checks pass. The author deletes nothing manually; automation cleans up.

**CI gates (all required):** lint + format, mypy strict, unit + contract tests, web tests, OpenAPI freshness, commitlint, DCO, image build + Trivy/Grype scan (fail on critical), SBOM generation, `docker compose config` validation, `specify check`/`/speckit.analyze` consistency on the spec directory, and the "no `/bin/sh` in production images" check.

## 7. Coding standards

**Python (3.13, `uv` workspace).** Ruff for formatting and linting (config in `pyproject.toml`; line length 100; import sorting on). mypy strict; no `Any` in public signatures; no `# type: ignore` without a reason code. Async I/O throughout (`httpx`, SQLAlchemy async); no blocking calls in request handlers. Pydantic v2 for every wire type with `extra="forbid"`; schemas live in `packages/coire-core` and nowhere else. Errors are `CoireError` subclasses mapped to RFC 9457 problem details at the gateway. Logging is structured (`structlog`) with `run_id`/`job_id`/`instance_id`/`model_id`/`user_id` context; no `print`. Tracing via OpenTelemetry (`coire.<service>.<op>` span names). Docstrings in Google style on public functions; comments explain *why*. Tests with pytest: unit tests next to code (`tests/unit`), contract tests per route (`tests/contract`), integration tests marked `integration` that run against `$COIRE_TEST_MODEL` (≤ 1 GB). Coverage is reported, not gated, but a PR that lowers coverage on the scheduler/ledger or auth modules will be asked to add tests. Property-based tests (Hypothesis) are encouraged for the ledger and placement logic.

**Engine boundaries.** Only `apps/coire-node` may spawn processes or touch Docker. Subprocess calls use explicit argv lists, never shell strings. Anything that allocates memory on a Studio registers a ledger reservation first.

**Web (React + TypeScript, Vite, pnpm).** TypeScript strict; ESLint + Prettier; function components and hooks; API types generated from OpenAPI (`pnpm gen:api`); all HTTP in `src/api/`; SSE via the shared hook; accessible components (keyboard, focus, ARIA, both themes); Vitest + Testing Library for units, Playwright for a small smoke suite. No new UI dependency without discussion.

**Containers and deploy.** One process per container; multi-stage Dockerfiles from `cgr.dev/chainguard/*` or `scratch`; non-root `USER`; `read_only: true`, `cap_drop: [ALL]`, `security_opt: [no-new-privileges:true]`; healthcheck; explicit networks; exact image tags (no `latest`). Compose changes ship with `docker compose config` passing and a runbook note.

**Database.** One Alembic migration per PR, reversible, named `NNNN_<slug>`; no data migrations mixed with schema migrations; never edit a merged migration.

**Documentation.** Update `docs/ARCHITECTURE.md` when behaviour changes; add an ADR (`docs/adr/NNNN-<slug>.md`, MADR format) for any decision that departs from it; keep runbooks current. API docs are generated; don't hand-write endpoint docs.

## 8. Dependencies and licences

Add a dependency only when it removes meaningful code and is actively maintained. State the reason in the PR. Pin exact versions in `uv.lock` / `pnpm-lock.yaml`; Renovate opens upgrade PRs weekly. Acceptable licences: MIT, BSD, Apache-2.0, ISC, MPL-2.0, PSF; anything else (including AGPL) needs maintainer approval. Engine and model-side packages (`mlx`, `mlx-lm`, `jaccl`, `mflux`, `mlx-lm-lora`) are pinned in `deploy/cluster/engines.lock` and upgraded only through the node upgrade job with its smoke test — never ad hoc on a Studio.

## 9. Security

Report vulnerabilities privately to the maintainer via GitHub's private vulnerability reporting on this repository (or the address in `SECURITY.md`); expect an acknowledgement within 72 hours. Do not open public issues for security bugs. Never commit secrets; CI runs gitleaks. Changes to auth, API keys, entitlements, container hardening, the acquisition pipeline, or network policy require two reviews and a threat-model note in the PR.

## 10. Releases

`main` is continuously deployable to core. Releases are tagged `vMAJOR.MINOR.PATCH` by release-please from conventional commits; CI builds and pushes the per-service images at that tag and publishes SBOMs. Upgrades on core are `docker compose` pulls of the new tag (rollback = previous tag); Studio engine upgrades go through the admin upgrade job. Breaking contract changes bump MAJOR and ship with a migration note.

## 11. Getting help

Open a discussion for design questions before writing a spec, use issue templates for bugs and proposals, and read `docs/ROADMAP.md` to see what's next and what's deliberately later. If you are unsure whether something needs a spec, it probably does — ask.
