# syntax=docker/dockerfile:1
#
# coire-otel — a minimal collector distribution plus a static health probe.
#
# The upstream image has no shell, so its container healthcheck needs the same self-contained
# binary the web image uses (research R2). Derived images built by CI are subject to the same
# image policy as first-party images (contracts/image-policy.md).

FROM docker.io/library/golang:1.26-bookworm@sha256:e8c859f5632dcfde7b32d2012b4351728f6437930887c2f6a91ea242459e5514 AS build
WORKDIR /src
COPY apps/coire-web/healthcheck/ ./
RUN CGO_ENABLED=0 GOOS=linux go build -ldflags="-s -w" -o /healthcheck .

RUN go install go.opentelemetry.io/collector/cmd/builder@v0.159.0
COPY deploy/compose/otel-builder.yaml /src/otel-builder.yaml
RUN builder --config /src/otel-builder.yaml

FROM scratch

USER 10001:10001

COPY --from=build /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-certificates.crt
COPY --from=build /healthcheck /healthcheck
COPY --from=build /src/_build/coire-otel /otelcol-contrib
COPY deploy/compose/otel-collector.yaml /etc/otelcol-contrib/config.yaml

EXPOSE 4317 4318 13133

HEALTHCHECK --interval=5s --timeout=2s --retries=3 --start-period=5s \
    CMD ["/healthcheck", "http://127.0.0.1:13133/"]

ENTRYPOINT ["/otelcol-contrib"]
CMD ["--config", "/etc/otelcol-contrib/config.yaml"]
