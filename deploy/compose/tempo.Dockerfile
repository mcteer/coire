# syntax=docker/dockerfile:1
FROM docker.io/library/golang:1.26-bookworm@sha256:e8c859f5632dcfde7b32d2012b4351728f6437930887c2f6a91ea242459e5514 AS build
COPY apps/coire-web/healthcheck/ /probe/
RUN mkdir -p /out && cd /probe && CGO_ENABLED=0 go build -ldflags="-s -w" -o /out/healthcheck .
ARG TEMPO_VERSION=2.10.7
WORKDIR /src
RUN curl -fsSLo source.tar.gz "https://github.com/grafana/tempo/archive/refs/tags/v${TEMPO_VERSION}.tar.gz" \
 && tar -xzf source.tar.gz --strip-components=1 \
 && rm source.tar.gz \
 && go mod edit -replace=golang.org/x/crypto=golang.org/x/crypto@v0.55.0 \
 && go mod tidy \
 && CGO_ENABLED=0 go build -mod=mod -buildvcs=false -trimpath -tags netgo -ldflags="-s -w" -o /out/tempo ./cmd/tempo \
 && mkdir -p /data/tempo

FROM scratch
COPY --from=build /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-certificates.crt
COPY --from=build /out/tempo /tempo
COPY --from=build /out/healthcheck /healthcheck
COPY --from=build /src/LICENSE /licenses/LICENSE
COPY deploy/compose/tempo/tempo.yml /etc/tempo/tempo.yml
COPY --from=build --chown=10001:10001 /data/tempo /var/tempo
USER 10001:10001
ENTRYPOINT ["/tempo"]
