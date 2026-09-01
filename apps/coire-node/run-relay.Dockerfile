# syntax=docker/dockerfile:1
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim@sha256:531f855bda2c73cd6ef67d56b733b357cea384185b3022bd09f05e002cd144ca AS builder
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /build
COPY pyproject.toml uv.lock ./
COPY packages/coire-core/pyproject.toml packages/coire-core/
COPY apps/coire-api/pyproject.toml apps/coire-api/
COPY apps/coire-node/pyproject.toml apps/coire-node/
COPY apps/coire-agent/pyproject.toml apps/coire-agent/
RUN uv venv --python 3.13 --relocatable /app/.venv \
 && uv sync --frozen --no-dev --no-editable --no-install-workspace --package coire-node
COPY packages/coire-core packages/coire-core
COPY apps/coire-node apps/coire-node
RUN uv sync --frozen --no-dev --no-editable --package coire-node \
 && rm -rf /usr/local/bin/pip* /usr/local/lib/python3.13/site-packages/pip* /app/.venv/bin/pip*

FROM gcr.io/distroless/base-debian12:nonroot@sha256:7f0c72cd138b442ae0deeb69c08b1acf5525439ba251a49ad93c320a061567e5
COPY --from=builder /usr/lib/aarch64-linux-gnu/libz.so.1 /usr/lib/aarch64-linux-gnu/libffi.so.8 /usr/lib/aarch64-linux-gnu/libgcc_s.so.1 /usr/lib/aarch64-linux-gnu/libstdc++.so.6 /usr/lib/aarch64-linux-gnu/libuuid.so.1 /usr/lib/aarch64-linux-gnu/liblzma.so.5 /usr/lib/aarch64-linux-gnu/libbz2.so.1.0 /usr/lib/aarch64-linux-gnu/libsqlite3.so.0 /usr/lib/aarch64-linux-gnu/
COPY --from=builder /usr/local /usr/local
COPY --from=builder /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:${PATH}" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
USER 65532:65532
WORKDIR /app
EXPOSE 8080
HEALTHCHECK --interval=5s --timeout=2s --retries=3 --start-period=5s \
    CMD ["/app/.venv/bin/python3", "-c", "import sys,urllib.request;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/ready',timeout=2).status==200 else 1)"]
ENTRYPOINT ["/app/.venv/bin/python3"]
CMD ["-m", "coire_node.run_relay"]
