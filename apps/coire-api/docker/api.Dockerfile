# syntax=docker/dockerfile:1
#
# coire-api — the control plane.
#
# Runtime is distroless with a pinned CPython 3.13 copied from the builder. Neither
# off-the-shelf distroless Python base satisfies the constitution's 3.13 pin:
# gcr.io/distroless/python3-debian12 ships 3.11.2, and Chainguard's free tier is latest-only
# at 3.14.7 (research R1, both probed). The builder is Debian bookworm and the runtime is
# base-debian12, so the interpreter's glibc matches.
#
# No shell, no package manager, non-root, read-only-root compatible (image-policy rules 1-4).

FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim@sha256:531f855bda2c73cd6ef67d56b733b357cea384185b3022bd09f05e002cd144ca AS builder

# UV_PROJECT_ENVIRONMENT is what uv honours for the target venv; VIRTUAL_ENV is ignored by
# `uv sync`, which would otherwise build /build/.venv and leave /app/.venv empty.
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /build
COPY pyproject.toml uv.lock ./
COPY packages/coire-core/pyproject.toml packages/coire-core/
COPY apps/coire-api/pyproject.toml apps/coire-api/
COPY apps/coire-node/pyproject.toml apps/coire-node/
COPY apps/coire-agent/pyproject.toml apps/coire-agent/

# Dependencies first so a source-only change does not re-resolve the world.
RUN uv venv --python 3.13 --relocatable /app/.venv \
 && uv sync --frozen --no-dev --no-editable --no-install-workspace --package coire-api

COPY packages/coire-core packages/coire-core
COPY apps/coire-api apps/coire-api
# --no-editable: workspace packages must be copied into the venv, not linked back to /build,
# which does not exist in the runtime stage.
RUN uv sync --frozen --no-dev --no-editable --package coire-api

# The runtime must contain no package manager (image-policy rule 2). pip ships with the
# builder's interpreter and would otherwise arrive via the /usr/local copy.
RUN rm -rf /usr/local/bin/pip /usr/local/bin/pip3 /usr/local/bin/pip3.13 \
           /usr/local/bin/easy_install* \
           /usr/local/lib/python3.13/site-packages/pip \
           /usr/local/lib/python3.13/site-packages/pip-*.dist-info \
           /app/.venv/bin/pip /app/.venv/bin/pip3 /app/.venv/bin/pip3.13

FROM gcr.io/distroless/base-debian12:nonroot@sha256:7f0c72cd138b442ae0deeb69c08b1acf5525439ba251a49ad93c320a061567e5

# distroless/base-debian12 ships glibc, libssl and libcrypto but not these. Determined by
# diffing `ldd` over the interpreter and every extension module against the runtime's own
# shared objects. Tcl/Tk, ncurses, readline and dbm are deliberately excluded: they back
# GUI and interactive stdlib modules that a server never imports, and leaving them out keeps
# the image minimal.
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
COPY --from=builder /build/apps/coire-api/alembic /app/alembic
COPY --from=builder /build/apps/coire-api/alembic.ini /app/alembic.ini
COPY deploy/cluster/nodes.yaml /app/nodes.yaml

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER 65532:65532
WORKDIR /app
EXPOSE 8000

# There is no shell and no curl in this image, so the probe is the interpreter itself.
HEALTHCHECK --interval=5s --timeout=2s --retries=3 --start-period=10s \
    CMD ["/app/.venv/bin/python3", "-c", \
         "import sys,urllib.request;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/ready',timeout=2).status==200 else 1)"]

ENTRYPOINT ["/app/.venv/bin/python3"]
CMD ["-m", "coire_api"]
