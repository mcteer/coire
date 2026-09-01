# syntax=docker/dockerfile:1
FROM docker.io/library/golang:1.26-bookworm@sha256:e8c859f5632dcfde7b32d2012b4351728f6437930887c2f6a91ea242459e5514 AS grafana-build
ARG GRAFANA_VERSION=13.2.0
WORKDIR /src
RUN curl -fsSLo source.tar.gz "https://github.com/grafana/grafana/archive/refs/tags/v${GRAFANA_VERSION}.tar.gz" \
 && tar -xzf source.tar.gz --strip-components=1 \
 && rm source.tar.gz \
 && go mod edit -replace=golang.org/x/crypto=golang.org/x/crypto@v0.55.0 \
 && go mod tidy \
 && CGO_ENABLED=0 go build -buildvcs=false -trimpath -tags oss -ldflags="-s -w -X main.version=${GRAFANA_VERSION}" -o /out/grafana ./pkg/cmd/grafana

FROM docker.io/grafana/grafana:13.2.0@sha256:3fd54ae1214669f8355f065ec9f6445d5279a3d77095ab048ca045685272429b AS loki-plugin
USER root
ARG LOKI_DATASOURCE_VERSION=13.1.1
RUN wget -qO /tmp/loki.zip "https://github.com/grafana/grafana-loki-datasource/releases/download/v${LOKI_DATASOURCE_VERSION}/loki-${LOKI_DATASOURCE_VERSION}.linux_arm64.zip" \
 && unzip -q /tmp/loki.zip -d /out \
 && rm -rf \
      /usr/share/grafana/data/plugins-bundled/elasticsearch \
      /usr/share/grafana/data/plugins-bundled/mssql \
      /usr/share/grafana/data/plugins-bundled/stackdriver \
 && rm -rf /usr/share/grafana/data/plugins-bundled/loki \
 && cp -R /out/loki /usr/share/grafana/data/plugins-bundled/loki \
 && mkdir -p /var/lib/grafana/plugins /var/log/grafana \
 && chown -R 472:472 /var/lib/grafana /var/log/grafana

FROM docker.io/library/golang:1.26-bookworm@sha256:e8c859f5632dcfde7b32d2012b4351728f6437930887c2f6a91ea242459e5514 AS entrypoint-build
WORKDIR /src
COPY deploy/compose/grafana-entrypoint/main.go .
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/entrypoint main.go

FROM scratch
COPY --from=grafana-build /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-certificates.crt
COPY --from=loki-plugin /usr/share/grafana /usr/share/grafana
COPY --from=loki-plugin /etc/grafana/grafana.ini /etc/grafana/grafana.ini
COPY --from=loki-plugin --chown=472:472 /var/lib/grafana /var/lib/grafana
COPY --from=loki-plugin --chown=472:472 /var/log/grafana /var/log/grafana
COPY --from=grafana-build /out/grafana /usr/share/grafana/bin/grafana
COPY --from=entrypoint-build /out/entrypoint /entrypoint
ENV GF_PATHS_CONFIG=/etc/grafana/grafana.ini \
    GF_PATHS_DATA=/tmp/grafana \
    GF_PATHS_HOME=/usr/share/grafana \
    GF_PATHS_LOGS=/tmp/grafana/log \
    GF_PATHS_PLUGINS=/tmp/grafana/plugins \
    GF_PATHS_PROVISIONING=/etc/grafana/provisioning \
    GF_PLUGINS_PREINSTALL_DISABLED=true \
    GF_PLUGINS_PUBLIC_KEY_RETRIEVAL_DISABLED=true \
    GF_ANALYTICS_REPORTING_ENABLED=false \
    GF_ANALYTICS_CHECK_FOR_UPDATES=false \
    GF_ANALYTICS_CHECK_FOR_PLUGIN_UPDATES=false \
    HOME=/tmp/grafana
WORKDIR /usr/share/grafana
USER 472:472
ENTRYPOINT ["/entrypoint"]
