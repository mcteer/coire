# Implementation Plan: Native image runtime packaging

**Branch**: `feat/015a-image-runtime`  
**Spec**: [spec.md](spec.md)

## Constitution Check

| Principle | Compliance |
| --- | --- |
| I | Install bare mflux; no inference wrapper. |
| II | Darwin-only dependency belongs to coire-node; no engine on core. |
| II-a | Core images and hardening remain unchanged. |
| III | No wire contract is added. |
| IV | No new route or secret is added. |
| V | Runtime packaging is separate from admin model acquisition. |
| VI | Existing upgrade smoke and rollback remain the observable gate; generation telemetry follows in the parent feature. |
| VII | Regression tests precede implementation; no engine starts in tests. |

## Approach

Pin mflux 0.20.0 (MIT) in the node package and lockfile. Extend the existing frozen wheelhouse selection and staged Python smoke with a no-model import/API check. Keep staged upgrade atomic. Test lock selection, package placement, old smoke preservation, and failed-upgrade behavior. Node smoke on a real Studio is an operator acceptance step, not performed by this change.

## Dependency and licence note

mflux is the direct, bare image engine required by parent feature 015 and is MIT licensed. This child installs code only. Every model and auxiliary asset remains subject to a separate licence review before admin acquisition and publication.
