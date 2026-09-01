# syntax=docker/dockerfile:1
FROM docker.io/library/golang:1.26-bookworm@sha256:e8c859f5632dcfde7b32d2012b4351728f6437930887c2f6a91ea242459e5514 AS build
ARG LOKI_VERSION=3.7.4
WORKDIR /src
RUN curl -fsSLo source.tar.gz "https://github.com/grafana/loki/archive/refs/tags/v${LOKI_VERSION}.tar.gz" \
 && tar -xzf source.tar.gz --strip-components=1 \
 && rm source.tar.gz \
 && go mod edit -replace=golang.org/x/crypto=golang.org/x/crypto@v0.55.0 \
 && go mod tidy \
 && CGO_ENABLED=0 go build -mod=mod -buildvcs=false -trimpath -tags netgo -ldflags="-s -w" -o /out/loki ./cmd/loki \
 && mkdir -p /data/loki

FROM scratch
COPY --from=build /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-certificates.crt
COPY --from=build /out/loki /usr/bin/loki
COPY --from=build --chown=10001:10001 /data/loki /loki
USER 10001:10001
ENTRYPOINT ["/usr/bin/loki"]
