# Contract: First-Party Image Policy

**Feature**: `000-bootstrap` · Enforced by `.github/workflows/ci.yml` job `image-policy`, one
matrix entry per first-party image. A failure names the image and the rule.

Applies to every image CI builds: `coire-api`, `coire-mcp`, `coire-scheduler`,
`coire-migrate`, `coire-web`, `coire-agent`, and the derived `coire-otel` (upstream collector +
static probe). Third-party images pulled unmodified (`postgres:17`, socket proxy) are scanned
and digest-pinned but are not subject to rules 1–3 (research R3; spec FR-004 as amended).

| # | Rule | Check | Fails with |
|---|---|---|---|
| 1 | No shell | `docker run --rm --entrypoint /bin/sh $IMG -c true` must exit non-zero; `docker create` + `docker export` tar listing must contain none of `bin/sh`, `bin/bash`, `bin/ash`, `bin/dash`, `usr/bin/sh` | `policy: shell present in <img>: <path>` |
| 2 | No package manager | Exported tar must contain none of `usr/bin/apt*`, `sbin/apk`, `usr/bin/pip*`, `usr/local/bin/pip*`, `usr/bin/dpkg` | `policy: package manager in <img>: <path>` |
| 3 | Non-root | `docker inspect --format '{{.Config.User}}'` must be non-empty and not `0`/`root` | `policy: <img> runs as root` |
| 4 | Read-only compatible | Container starts and passes its healthcheck with `--read-only --tmpfs /tmp` | `policy: <img> requires writable rootfs` |
| 5 | arm64 present | Manifest must include `linux/arm64`; absent fails (FR-016) | `policy: <img> has no linux/arm64 variant` |
| 6 | Single process | `ENTRYPOINT` is an exec-form array invoking the service directly; no init wrapper or shell form | `policy: <img> entrypoint is not exec-form` |
| 7 | Pinned bases | Every `FROM` in the Dockerfile carries `@sha256:` | `policy: unpinned FROM in <dockerfile>` |

Also run on every image (first- and third-party):

| Job | Tool | Threshold |
|---|---|---|
| `scan` | Trivy (image mode) | fail on any `CRITICAL`; `HIGH` reported |
| `sbom` | Syft → SPDX JSON | attached to the tag as a workflow artefact and OCI referrer |

**SC-008 fixture**: a PR that adds `COPY --from=busybox /bin/sh /bin/sh` to any first-party
Dockerfile must fail job `image-policy` with rule 1's message. This is a required status on
`main` once branch protection is enabled.
