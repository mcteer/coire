# syntax=docker/dockerfile:1
#
# coire-otel — the upstream collector plus a static health probe.
#
# The upstream image has no shell, so its container healthcheck needs the same self-contained
# binary the web image uses (research R2). Derived images built by CI are subject to the same
# image policy as first-party images (contracts/image-policy.md).

FROM docker.io/library/golang:1.26-bookworm@sha256:e8c859f5632dcfde7b32d2012b4351728f6437930887c2f6a91ea242459e5514 AS probe
WORKDIR /src
COPY apps/coire-web/healthcheck/ ./
RUN CGO_ENABLED=0 GOOS=linux go build -ldflags="-s -w" -o /healthcheck .

FROM docker.io/otel/opentelemetry-collector-contrib:latest@sha256:1f2c54a30e713fac6b3ae77a1ec84010c2007e29ced8ec666214fc2f6739c1cc

COPY --from=probe /healthcheck /healthcheck
COPY deploy/compose/otel-collector.yaml /etc/otelcol-contrib/config.yaml

EXPOSE 4317 4318 13133

HEALTHCHECK --interval=5s --timeout=2s --retries=3 --start-period=5s \
    CMD ["/healthcheck", "http://127.0.0.1:13133/"]
