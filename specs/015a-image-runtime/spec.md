# Feature Specification: Native image runtime packaging

**Feature Branch**: `feat/015a-image-runtime`  
**Parent**: `specs/015-image-generation/` (T002–T003)  
**Status**: In implementation

## Goal

Install the bare `mflux==0.20.0` Python runtime only on Apple Silicon Studios through the existing frozen node wheelhouse and immutable versioned-environment upgrade path. A failed image smoke must keep the previous active runtime. Core images and Linux installations must remain free of image engine dependencies. Text and VLM smoke must continue to run.

## Acceptance

1. The node package declares exact `mflux==0.20.0` with a Darwin platform marker and `uv.lock` resolves its full dependency graph.
2. The wheel staging path selects and verifies all Darwin-active locked wheels and refuses a missing mflux wheel or invalid provenance.
3. Before a symlink flip, staged Python imports the existing text/VLM runtimes and mflux, and checks that mflux's supported model entry points exist without loading a model.
4. Failing any smoke leaves the previous runtime active and the staged tree available for inspection.
5. The image engine dependency is absent from core service images and Linux dependency resolution.

## Out of scope

Model acquisition, generation, publishing, and user-facing routes remain disabled. Model weights and their licences are reviewed separately during admin acquisition (parent T027/T033).
