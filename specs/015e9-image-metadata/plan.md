# Implementation Plan: Canonical generated PNG metadata

**Spec**: [spec.md](spec.md)

## Constitution Check

| Principle | Compliance |
| --- | --- |
| I/II | PNG serialization remains inside the Studio node runtime. |
| II-a | No new service or container. |
| III | Reuse `ImageRecipe`, canonical encoding and `pixel_digest` from coire-core. |
| IV/V | Private exclusive file, no caller path in recipe, no engine metadata. |
| VI | Output write telemetry follows the supervised worker stage; this pure writer adds no public path. |
| VII | Round-trip, size and failure tests precede implementation. |

## Approach

Construct a strict `ImageRecipe` from the resolved settings and RGB pixels. Use Pillow's PNG writer with a fresh `PngInfo` containing one uncompressed iTXt entry. Wrap an exclusive 0600 file in a byte-limited, hashing writer. Flush and fsync on success; unlink on failure. Verify the resulting metadata with the existing isolated parser in tests.
