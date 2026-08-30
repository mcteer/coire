# syntax=docker/dockerfile:1
#
# coire-node, Linux, for CI only. NEVER deployed.
#
# The node agent runs natively under launchd on a Studio (feature 000 research R5); this image
# exists so the Linux integration job can run two real agents on a simulated mesh and exercise
# registration, acquisition, the mesh-only export path, engine lifecycle and reconciliation
# (research R9). Only the model load itself is faked, because MLX is Apple-Silicon only.
#
# Deliberately NOT built to the production image policy: it needs a shell so the integration
# tests can kill and restart the agent to prove engines survive. `scripts/image-policy.sh`
# excludes it by name and `tests/integration/test_topology.py` asserts no service of the
# production compose project uses it.

FROM python:3.13-slim-bookworm@sha256:c45a22ea000adfd9cda29364bbe7edd23001ce5cc2ad15857cfbf7766943b9ca AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv

COPY --from=ghcr.io/astral-sh/uv:0.12.7@sha256:95f2aa1fe59274951cfe9b0cbc7972e879ff1004bc8945d130a32eb0dbd85945 /uv /usr/local/bin/uv

WORKDIR /build
COPY pyproject.toml uv.lock ./
COPY packages/coire-core/pyproject.toml packages/coire-core/
COPY apps/coire-api/pyproject.toml apps/coire-api/
COPY apps/coire-node/pyproject.toml apps/coire-node/
COPY apps/coire-agent/pyproject.toml apps/coire-agent/

# mlx-lm is marked `platform_system == "Darwin"`, so the resolver simply omits it here. The
# agent imports it lazily, and COIRE_ENGINE_COMMAND points at the fake engine in this image.
RUN uv venv --python 3.13 /app/.venv \
 && uv sync --frozen --no-dev --no-editable --no-install-workspace --package coire-node

COPY packages/coire-core packages/coire-core
COPY apps/coire-node apps/coire-node
RUN uv sync --frozen --no-dev --no-editable --package coire-node

FROM python:3.13-slim-bookworm@sha256:c45a22ea000adfd9cda29364bbe7edd23001ce5cc2ad15857cfbf7766943b9ca

# procps for `pkill`, which the restart test uses to kill the agent while its engines keep
# running — the whole point of this image.
RUN apt-get update \
 && apt-get install -y --no-install-recommends procps curl tini \
 && rm -rf /var/lib/apt/lists/*

COPY --from=builder /app/.venv /app/.venv
COPY apps/coire-node/docker/node-test-entrypoint.sh /usr/local/bin/node-test-entrypoint

ENV PATH="/app/.venv/bin:$PATH" \
    NODE_STORE_DIR=/opt/coire/models \
    NODE_STATE_DIR=/opt/coire/state \
    NODE_HF_CACHE_DIR=/opt/coire/hf-cache \
    HF_HOME=/opt/coire/hf-cache \
    HF_HUB_DISABLE_PROGRESS_BARS=1 \
    PYTHONUNBUFFERED=1

RUN mkdir -p /opt/coire/models /opt/coire/state/jobs /opt/coire/hf-cache \
 && chmod +x /usr/local/bin/node-test-entrypoint

EXPOSE 9400
# tini reaps the engines the agent abandons, which in a container has no launchd to do it.
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/node-test-entrypoint"]
