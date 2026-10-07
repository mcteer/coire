# syntax=docker/dockerfile:1
#
# coire-failover — the Studio inference frontend. It is not the control plane.
#
# Runtime is distroless with the builder's CPython 3.13. gcr.io/distroless/python3-debian12
# ships 3.11, so the interpreter is copied from the bookworm builder onto base-debian12,
# matching the other Coire images.

FROM docker.io/library/node:22-bookworm-slim@sha256:83f487e0a63425e5b4d146fb5e5be574bcbe1b7b843d3ebafdd95eaf7767a7e5 AS web
WORKDIR /src
COPY apps/coire-web/package.json apps/coire-web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY apps/coire-web/ ./
COPY docs/runbooks/sft-training.md /docs/runbooks/sft-training.md
RUN npm run build

FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim@sha256:531f855bda2c73cd6ef67d56b733b357cea384185b3022bd09f05e002cd144ca AS builder
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /build
COPY pyproject.toml uv.lock ./
COPY packages/coire-core/pyproject.toml packages/coire-core/
COPY apps/coire-failover/pyproject.toml apps/coire-failover/
RUN uv venv --python 3.13 --relocatable /app/.venv \
 && uv sync --frozen --no-dev --no-editable --no-install-workspace --package coire-failover
COPY packages/coire-core packages/coire-core
COPY apps/coire-failover apps/coire-failover
RUN uv sync --frozen --no-dev --no-editable --package coire-failover \
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
COPY --from=web /src/dist /app/static
ENV PATH="/app/.venv/bin:${PATH}" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
USER 65532:65532
WORKDIR /app
EXPOSE 8004
ENTRYPOINT ["/app/.venv/bin/python3"]
CMD ["-m", "coire_failover"]
