# Execution record: native image runtime packaging

- `uv lock`: resolved `mflux==0.20.0` and its transitive graph; Darwin node dependency only.
- Frozen node `pylock.toml` export and `stage-node-wheels.py --check-only`: 87 compatible macOS arm64 wheels selected, including the hash-pinned mflux wheel.
- Staged Python smoke against the local locked environment: text and VLM help checks, mflux import and `Flux1` import all passed without loading weights.
- `uv run --locked pytest -q tests/unit/test_node_install.py`: 6 passed.
- `uv run --locked pytest -q`: 1179 passed, 141 skipped. Skips are existing suite markers, not new tests in this slice.
- `uv run --locked ruff format --check && uv run --locked ruff check`: passed across the repository.
- `uv run --locked mypy apps/ packages/`: passed across 481 source files.

The real Studio upgrade smoke and previous-runtime rollback remain an operator gate before deploying this child. No Studio was contacted and no model was loaded.
