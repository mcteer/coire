# Research and decisions

- The UI reads capability bounds. `image_validation.py` sets all min/max/default
  dimensions to its 512px smoke size, so this is an advertised capability restriction.
- The installed pinned mflux Config rounds dimensions DOWN to multiples of 16.
  Its Z-Image latent creator uses width/height //8; directly accepting 1980 would
  silently produce 1968. Native canvas must round UP to 1984 and crop exactly.
- Existing ImageSpec/Submit/Capability enforce multiples of eight, while recipe and
  transfer verify exact final dimensions. Relax dimensions without changing shape;
  preserve 16MP cap on padded canvas and outputs.
- Registry lacks an audited capability requalification operation. Reusing the
  VERIFY_REPLICA durable cursor on existing copies avoids downloads, preserves
  restart idempotency and keeps current published capability until both copies pass.
- Read-only SSH inspected installed mflux code, no engine/model was started directly.
- No user clarification is needed: 1980 is literal; ratio output remains exact in
  whole pixels and displays actual dimensions before submit.

## Real-cluster findings during qualification
The first attempt exposed missing forwarding of resolution_bounds through node job params
and reconstruction; add route/worker boundary regressions rather than weakening evidence.
The second attempt was cancelled through authenticated node DELETE when a live process sample
reported a 167.2 GiB physical footprint against a 112 GiB reservation. mflux was waiting in MLX evaluation.
Pinned mflux 0.20.0 provides native TilingConfig/VAETiler and ZImage.tiling_config. Expanded images
now use its 512px overlapping VAE tiles; existing 512px recipes retain their untiled decode path.
See official upstream release notes: https://github.com/mflux-community/mflux/releases .
A worker watchdog now writes failure and exits the owned validation process during an overage
or unavailable footprint, including while native code waits. A subprocess regression verifies
failure persistence and actual termination without inference. Do not rely on post-run peaks
alone as enforcement. Expanded limits still require both-copy measured evidence.
