# Coire

Coire is the control plane for a three-node Apple Silicon AI lab. A Mac Mini runs the API, scheduler, web frontend, database, and local monitoring. Two Mac Studios hold model weights and run the inference engines and user agent containers. The Mini never loads a model.

The gateway exposes OpenAI-compatible `/v1` endpoints. The admin API manages model acquisition, placement, runs, and audit history. An optional MCP service offers the coding loop `research`, `plan`, and `apply`; `apply` produces a reviewable branch artifact rather than pushing code to a repository.

## How it fits together

| Host | Main responsibilities |
| --- | --- |
| `coire-core` · Mac Mini M4 Pro, 24 GB | OrbStack control plane, Postgres, gateway, scheduler, web, baseline metrics and alerts; optional MCP, ops, and diagnostic history |
| `coire-edge-a` · Mac Studio | Native `coire-node`, MLX engines, model copies, Studio agent containers |
| `coire-edge-b` · Mac Studio | Native `coire-node`, MLX engines, model copies, Studio agent containers |

The three hosts communicate on an isolated control VLAN. The Studios use a direct Thunderbolt link for model replication and distributed MLX work. Coire starts bare MLX and mflux engines through the node agent; it does not use an inference wrapper. User coding, general, and image harnesses run in isolated containers on a Studio. The only harness allowed on core is the platform's own `ops` service.

For the full design and feature order, see [Architecture](docs/ARCHITECTURE.md) and [Roadmap](docs/ROADMAP.md). [ADR 0007](docs/adr/0007-lean-control-plane-diagnostics.md) explains why baseline monitoring runs continuously while historical traces and logs are optional.

## Repository layout

| Path | Purpose |
| --- | --- |
| [`packages/coire-core/`](packages/coire-core/) | Shared Pydantic contracts and configuration |
| [`apps/coire-api/`](apps/coire-api/) | Gateway, admin API, scheduler, MCP service, persistence, CLI |
| [`apps/coire-node/`](apps/coire-node/) | Studio node agent, engines, workspace and run broker |
| [`apps/coire-agent/`](apps/coire-agent/) | Studio user harness and separate core ops harness |
| [`apps/coire-web/`](apps/coire-web/) | React frontend and nginx ingress |
| [`deploy/compose/`](deploy/compose/) | Core deployment, optional profiles, image policy |
| [`deploy/launchd/`](deploy/launchd/) | Studio node-agent service template |
| [`specs/`](specs/) | Spec Kit specifications, plans, tasks, validation records |
| [`docs/runbooks/`](docs/runbooks/) | Operator procedures and rollback instructions |

## Develop locally

Use Python 3.13, [uv](https://docs.astral.sh/uv/), Node.js with pnpm, and a container runtime for composed tests. The unit and contract suites do not need model weights or live Studios.

```sh
uv sync --all-packages
pnpm -C apps/coire-web install
uv run ruff check
uv run mypy apps/coire-api/src apps/coire-node/src apps/coire-agent/src packages/coire-core/src
uv run pytest -q -m 'not integration'
pnpm -C apps/coire-web test
pnpm -C apps/coire-web lint
pnpm -C apps/coire-web build
uv run python -m coire_api.openapi --check
uv run coire --help
```

For the composed integration suite, use the disposable local test deployment described in [Contributing](CONTRIBUTING.md) and the relevant feature quickstart. Engine tests require a locally available tiny model. Do not aim CI or local integration commands at the real Studios.

## Run on core

Core deployment uses Keychain-sourced secrets and digest-pinned images. Read the [compose deployment guide](deploy/compose/README.md) and [bootstrap runbook](docs/runbooks/bootstrap.md) before first startup. Set the required Cloudflare Access configuration, provision Keychain items with `scripts/coire-secrets-init.sh`, and install the Studio node agents using the [cluster instructions](deploy/cluster/README.md).

From the repository root on core:

```sh
deploy/compose/coire-up
```

`coire-up` starts the lean control plane and prints its health URL. It uses already available first-party images; `deploy/compose/coire-up --build` explicitly builds them. Optional profiles can be combined:

```sh
COMPOSE_PROFILES=ops,mcp deploy/compose/coire-up
COMPOSE_PROFILES=diagnostics deploy/compose/coire-up
```

The `mcp` profile exposes `/mcp` through the same nginx ingress and requires a user-bound API key with the `mcp` scope. The `diagnostics` profile adds bounded Loki, Tempo, and Grafana storage; Prometheus and Alertmanager remain in the lean profile. `deploy/compose/coire-down` stops the stack while preserving data volumes by default. See the deployment guide for credential rotation, state directories, ports, recovery, and profile behavior.

No secrets, `.env` files, model weights, datasets, or generated images belong in Git. Model acquisition is an audited admin operation. The runtime accepts registry model IDs, never arbitrary engine paths.

## Contributing and operations

Changes are developed spec-first on feature branches and merged through PRs. Start with [Contributing](CONTRIBUTING.md), the [agent guidance](AGENTS.md), and the binding [constitution](.specify/memory/constitution.md). API wire changes begin in `coire-core`, include contract tests, and regenerate OpenAPI and web types.

Use [runbooks](docs/runbooks/) to diagnose, kill, or roll back services and runs. Report vulnerabilities through GitHub's private vulnerability reporting process described in [Contributing, §9](CONTRIBUTING.md#9-security).
