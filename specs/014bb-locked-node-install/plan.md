# Implementation Plan: Locked Native Node Install

**Branch**: `feat/014bb-locked-node-install` | **Parent**: [014 plan](../014-chat-web-ui/plan.md)

Export the node dependency graph from `uv.lock` as hashed requirements and a pylock. Select only Python 3.13 macOS arm64 wheels whose markers and tags match the Studio target, fetch from the pinned wheel source and verify digest/size before atomic publication. Build the local core/node wheels separately. The installer checks the wheelhouse, provisions a digest-named virtual environment, installs the locked graph offline, checks package consistency and invokes a small smoke/publish helper. The helper verifies both bare engine command entry points before atomically replacing `envs/current`. Unit tests use disposable directories and subprocess stubs; a local prefix verifies real locked installation without starting an engine or contacting a Studio.

## Constitution Check

| Principle | Check |
| --- | --- |
| I | Only bare `mlx_lm.server` and `mlx_vlm.server` entry points are staged. |
| II | Native wheels target Studio nodes; no model loads on core. |
| III | No wire contract changes. |
| IV | Hash verification and offline install bound executable inputs. |
| V | This install never acquires models. |
| VI | Existing node health/engine telemetry covers activated versions; smoke is operator-visible. |
| VII | Digest, activation/rollback tests and a disposable real install gate the installer; tiny-model acceptance remains open. |

No new runtime dependency is introduced; all staged wheels are selected from the existing pinned lock and retain the licences reviewed in the parent design.
