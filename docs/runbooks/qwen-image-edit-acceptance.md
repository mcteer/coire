# Qwen Image Edit acceptance — 2026-10-16

This records deployment evidence for [photo editing](qwen-image-edit.md). Implementation
extends the existing image feature directly, at the user's explicit instruction to omit
a new specification. PR: https://github.com/mcteer/coire/pull/111.

## Both-copy qualification

The authenticated admin acquisition pipeline downloaded the reviewed Q8 revision
`9e6e769beaf66eff466272e782668cf52d356407` and verified its local manifests on both
Studios. Model `b0edf865-6634-4856-9dfb-436d15db612b` reached `ready` before audited
publication. Both results use validator `qwen-edit-v1` and the same capability profile.

| Studio                 | Complete suite duration | Peak physical bytes | Physical delta bytes |
| ---------------------- | ----------------------: | ------------------: | -------------------: |
| coire-edge-b (origin)  |              2647.563 s |        118465182128 |         118227252936 |
| coire-edge-a (replica) |              2038.821 s |        118534240616 |         118297441920 |

Each suite edited an owned 800×600 geometric reference into 1024×1024 at 40 steps,
then 1980×1980, 1980×1113 and 1113×1980 at four steps. Exact dimensions, nondegenerate
RGB content and bounded physical memory passed on both copies. The large samples prove
size/memory bounds; four steps are not the recommended quality setting. The published
serving reservation is 141956930304 bytes, 20% above the larger measured physical delta.
Completed-copy qualification holds were released only after durable evidence and audit.

## Automated and deployment checks

- 462 image API tests, 102 native/shared image tests, 43 file-worker tests and 298 web tests passed.
- Strict mypy passed for 1322 files; Ruff, ESLint, Prettier, web build and generated
  OpenAPI freshness passed.
- API, scheduler, file-worker, web, Prometheus and Grafana production image policy checks, critical
  vulnerability scans and SBOM generation passed. Promtool validated 34 rules.
- Existing hardened containers were replaced individually. No new service, test container,
  production volume or dependency was introduced. Both Studios run immutable node builds
  using the existing pinned mflux 0.20.0; Core performs no inference.

## Owner browser acceptance

Headless Playwright WebKit submitted a real edit through the local HTTPS owner interface:
job `01M53GXVAH66Y6WT9QVVR464ND`. Its synthetic adult portrait reference is 1024×1024;
the requested output is 1024×768, 40 steps, guidance 4. The saved recipe retains the
original source dimensions and digest. Desktop, mobile and dark-theme screenshots were
captured; desktop/mobile photo-edit controls were visually inspected and mobile horizontal
overflow was checked. Submission showed the job progress panel with no browser errors.
The actual event stream showed generation steps advancing through 40. The job succeeded
in 790.290 seconds from submission, including loading, transfer and publication.

The downloaded PNG is exactly 1024×768 and matches its registered file hash. Its embedded
recipe confirms `edit`, 40 steps, guidance 4 and the original 1024×1024 reference digest.
Visual review found a woman at a lectern in an Oval Office-style setting with the source's
broad appearance and clothing retained. Facial details changed; this is useful reference
conditioning, not a guarantee of identity fidelity. No real family photograph was accessed.

Headless WebKit preview measurements and visual inspection passed at 1440×1000, 390×844
and 844×390, with the entire image contained inside the dialog. Settings reuse restored
Qwen, edit mode, the uploaded source, 40 steps and guidance 4 without browser errors.
A second edit reached `running`, was stopped through the actual UI and became `cancelled`
with zero outputs. Its job is `01M53HQVFX2HFJCF0MCMYK1MVP`.

The real UI permanently deleted all three owned history entries and their events, confirmed
the two library photos still existed and could be downloaded, then bulk-deleted both photos.
Database verification found zero remaining events and both output byte sets physically purged.
The owned source was deleted and the idle Qwen worker stopped through authenticated APIs.
The deployed cleanup fix physically purged both source files and released their quota charge.
No owned job, image worker, qualification hold or event remains active. Two unused intermediate
node environments from this task were removed after checking current pointers and process
arguments; current builds and the pre-change rollback baseline remain. Thirteen production
containers are healthy, with no test containers or additional volumes left running.

Final cleanup exposed an existing normalized-source purge defect: the API attempted to unlink
from its read-only derived volume. The scheduler now requests UUID-bound, authenticated
deletion from the file worker, which already owns the writable volume and service credential.
The API then removes the original and releases storage quota only after both files are absent.
No mount, network or credential scope was widened. Tests cover authentication, wrong identity,
extra path fields, symlinks, hard links, retry idempotence, active references, in-progress upload
grace and quota preservation on worker failure. This internal route uses shared Pydantic models;
the public OpenAPI and TypeScript remain current.

After deployment, another real WebKit session confirmed the published Qwen model, required
source input, portrait/landscape size controls and an error-free mobile dark theme. Exact
16:9 presets fit 1968×1107 inside the 1980px limit; custom dimensions may use the full bound.

## Separate local chat configuration prerequisite

The previously idle chat instance was drained through its authenticated lifecycle API to
free qualification memory. A replacement on Studio B loaded successfully, but the actual
gateway smoke failed closed with `not_elected`. Both root-owned node LaunchDaemons retain
`FAILOVER_MEMBER_NAME` from failover testing while the local control plane has no configured
election authority. The unusable replacement was drained rather than left as a ready route.

A checked, reversible LaunchDaemon correction is staged privately on both Studios. It
removes only the stale member-name setting, preserves authentication and all other settings,
and requires the macOS administrator to apply it. Studio A's script waits for its owned
qualification worker to exit. No authority guard was bypassed. Chat restoration and an
authenticated gateway completion must pass after the administrator applies the correction.

The prepared commands require the operator's macOS administrator password in Terminal:

```bash
ssh -t coire-edge-b.lab 'sudo env PYTHONNOUSERSITE=1 /opt/coire/envs/current/bin/python3 /Users/mcteer/coire-stage/local-chat-rollback/apply.py'
ssh -t coire-edge-a.lab 'sudo env PYTHONNOUSERSITE=1 /opt/coire/envs/current/bin/python3 /Users/mcteer/coire-stage/local-chat-rollback/apply.py'
```

Do not enter administrator passwords into chat. The candidate files and backups are private
on each Studio. The Studio B script also waits for the owned editing worker to exit.
