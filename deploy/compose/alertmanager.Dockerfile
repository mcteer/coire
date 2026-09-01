# syntax=docker/dockerfile:1
FROM docker.io/library/golang:1.26-bookworm@sha256:e8c859f5632dcfde7b32d2012b4351728f6437930887c2f6a91ea242459e5514 AS build
ARG ALERTMANAGER_VERSION=0.33.0
WORKDIR /src
RUN curl -fsSLo source.tar.gz "https://github.com/prometheus/alertmanager/archive/refs/tags/v${ALERTMANAGER_VERSION}.tar.gz" \
 && tar -xzf source.tar.gz --strip-components=1 \
 && rm source.tar.gz \
 && go mod edit -replace=golang.org/x/crypto=golang.org/x/crypto@v0.55.0 \
 && go mod tidy \
 && mkdir -p ui/app/dist \
 && printf '%s\n' '<!doctype html><title>Coire Alertmanager</title>' > ui/app/dist/index.html \
 && CGO_ENABLED=0 go build -trimpath -tags netgo -ldflags="-s -w" -o /out/alertmanager ./cmd/alertmanager \
 && CGO_ENABLED=0 go build -trimpath -tags netgo -ldflags="-s -w" -o /out/amtool ./cmd/amtool

FROM scratch
COPY --from=build /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-certificates.crt
COPY --from=build /out/alertmanager /bin/alertmanager
COPY --from=build /out/amtool /bin/amtool
USER 65534:65534
ENTRYPOINT ["/bin/alertmanager"]
CMD ["--storage.path=/tmp/alertmanager"]
