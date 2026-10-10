# syntax=docker/dockerfile:1
FROM docker.io/library/golang:1.26-bookworm@sha256:e8c859f5632dcfde7b32d2012b4351728f6437930887c2f6a91ea242459e5514 AS build
COPY apps/coire-web/healthcheck/ /probe/
RUN mkdir -p /out && cd /probe && CGO_ENABLED=0 go build -ldflags="-s -w" -o /out/healthcheck .
ARG PROMETHEUS_VERSION=3.14.0
WORKDIR /src
RUN curl -fsSLo prometheus.tar.gz "https://github.com/prometheus/prometheus/archive/refs/tags/v${PROMETHEUS_VERSION}.tar.gz" \
 && tar -xzf prometheus.tar.gz --strip-components=1 \
 && rm prometheus.tar.gz \
 && go mod edit -replace=golang.org/x/crypto=golang.org/x/crypto@v0.55.0 \
 && go mod download \
 && CGO_ENABLED=0 go build -trimpath -tags netgo -ldflags="-s -w" -o /out/prometheus ./cmd/prometheus \
 && CGO_ENABLED=0 go build -trimpath -tags netgo -ldflags="-s -w" -o /out/promtool ./cmd/promtool \
 && mkdir -p /data/prometheus

FROM scratch
COPY --from=build /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-certificates.crt
COPY --from=build /out/prometheus /bin/prometheus
COPY --from=build /out/promtool /bin/promtool
COPY --from=build /out/healthcheck /healthcheck
COPY --from=build /src/LICENSE /licenses/LICENSE
COPY deploy/compose/prometheus/prometheus.yml /etc/prometheus/prometheus.yml
COPY deploy/compose/prometheus/rules/ /etc/prometheus/rules/
COPY deploy/observability/alerts/control-plane-efficiency.yaml /etc/prometheus/rules/control-plane-efficiency.yml
COPY deploy/observability/alerts/chat.yaml /etc/prometheus/rules/chat.yml
COPY deploy/observability/alerts/image.yaml /etc/prometheus/rules/image.yml
COPY deploy/observability/alerts/training.yaml /etc/prometheus/rules/training.yml
COPY deploy/observability/alerts/evaluations.yaml /etc/prometheus/rules/evaluations.yml
COPY deploy/observability/alerts/feedback.yaml /etc/prometheus/rules/feedback.yml
COPY --from=build /src/web/ui /web/ui
COPY --from=build --chown=65534:65534 /data/prometheus /prometheus
USER 65534:65534
ENTRYPOINT ["/bin/prometheus"]
