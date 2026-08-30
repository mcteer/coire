# syntax=docker/dockerfile:1
#
# coire-web — nginx serving the built SPA. The sole ingress (FR-008).
#
# Chainguard's nginx has no shell, which is why the healthcheck is a static Go binary rather
# than curl (research R2). nginxinc/nginx-unprivileged was rejected: it contains /bin/sh,
# which image-policy rule 1 forbids for first-party images.
#
# No Node runtime reaches production — the SPA is built here and only its output is copied.

FROM docker.io/library/node:22-bookworm-slim@sha256:83f487e0a63425e5b4d146fb5e5be574bcbe1b7b843d3ebafdd95eaf7767a7e5 AS web
WORKDIR /src
COPY apps/coire-web/package.json apps/coire-web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY apps/coire-web/ ./
RUN npm run build

FROM docker.io/library/golang:1.26-bookworm@sha256:e8c859f5632dcfde7b32d2012b4351728f6437930887c2f6a91ea242459e5514 AS probe
WORKDIR /src
COPY apps/coire-web/healthcheck/ ./
RUN CGO_ENABLED=0 GOOS=linux go build -ldflags="-s -w" -o /healthcheck .

FROM cgr.dev/chainguard/nginx:latest@sha256:f801aa4ac908f6c3847efe81b577e4f34e1f237b5556dfbe698127ad464db269

COPY --from=web   /src/dist               /usr/share/nginx/html
COPY --from=probe /healthcheck            /healthcheck
COPY apps/coire-web/nginx/nginx.conf      /etc/nginx/nginx.conf

EXPOSE 8080

# Probes the local, non-proxied path so an upstream restart never marks this container
# unhealthy (spec SC-002).
HEALTHCHECK --interval=5s --timeout=2s --retries=3 --start-period=2s \
    CMD ["/healthcheck", "http://127.0.0.1:8080/nginx-health"]
