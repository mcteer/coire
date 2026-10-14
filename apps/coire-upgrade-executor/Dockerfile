# syntax=docker/dockerfile:1
# Independent CPU-only recovery service. Existing pinned bases, non-root distroless runtime.
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim@sha256:531f855bda2c73cd6ef67d56b733b357cea384185b3022bd09f05e002cd144ca AS builder
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /build
COPY pyproject.toml uv.lock ./
COPY packages/coire-core/pyproject.toml packages/coire-core/
COPY apps/coire-upgrade-executor/pyproject.toml apps/coire-upgrade-executor/
RUN uv venv --python 3.13 --relocatable /app/.venv \
 && uv sync --frozen --no-dev --no-editable --no-install-workspace --package coire-upgrade-executor
COPY packages/coire-core packages/coire-core
COPY apps/coire-upgrade-executor apps/coire-upgrade-executor
RUN uv sync --frozen --no-dev --no-editable --package coire-upgrade-executor \
 && mkdir -p /volume/state /volume/coordination /volume/prepared \
 && chmod 700 /volume/state /volume/coordination /volume/prepared \
 && rm -rf /usr/local/bin/pip /usr/local/bin/pip3 /usr/local/bin/pip3.13 \
           /usr/local/lib/python3.13/site-packages/pip \
           /usr/local/lib/python3.13/site-packages/pip-*.dist-info \
           /app/.venv/bin/pip /app/.venv/bin/pip3 /app/.venv/bin/pip3.13

FROM gcr.io/distroless/base-debian13:nonroot@sha256:a0d70d6a97cd697d9362bc2aae4a6560dd65817e365d0043b07325a97975dc91
COPY --from=builder \
     /usr/lib/aarch64-linux-gnu/libz.so.1 \
     /usr/lib/aarch64-linux-gnu/libffi.so.8 \
     /usr/lib/aarch64-linux-gnu/libgcc_s.so.1 \
     /usr/lib/aarch64-linux-gnu/libstdc++.so.6 \
     /usr/lib/aarch64-linux-gnu/libuuid.so.1 \
     /usr/lib/aarch64-linux-gnu/liblzma.so.5 \
     /usr/lib/aarch64-linux-gnu/libbz2.so.1.0 \
     /usr/lib/aarch64-linux-gnu/libsqlite3.so.0 \
     /usr/lib/aarch64-linux-gnu/
COPY --from=builder /usr/local /usr/local
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder --chown=65532:65532 --chmod=0700 /volume/prepared /opt/coire/upgrades/prepared
COPY --from=builder --chown=65532:65532 --chmod=0700 /volume/state /opt/coire/upgrades/state
COPY --from=builder --chown=65532:65532 --chmod=0700 /volume/coordination /opt/coire/upgrades/coordination
ENV PATH="/app/.venv/bin:${PATH}" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
USER 65532:65532
WORKDIR /app
EXPOSE 8011
HEALTHCHECK --interval=20s --timeout=3s --retries=3 --start-period=10s \
    CMD ["/app/.venv/bin/python3", "-m", "coire_upgrade_executor", "--healthcheck"]
ENTRYPOINT ["/app/.venv/bin/python3"]
CMD ["-m", "coire_upgrade_executor"]
