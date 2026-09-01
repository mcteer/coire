# syntax=docker/dockerfile:1
FROM docker.io/library/golang:1.26-bookworm@sha256:e8c859f5632dcfde7b32d2012b4351728f6437930887c2f6a91ea242459e5514 AS build
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
COPY --from=build /src/web/ui /web/ui
COPY --from=build --chown=65534:65534 /data/prometheus /prometheus
USER 65534:65534
ENTRYPOINT ["/bin/prometheus"]
