# Implementation Plan: Studio CPU image tagging

**Spec**: [spec.md](spec.md)

## Constitution Check

| Principle | Compliance |
| --- | --- |
| I | Direct Transformers/PyTorch, no inference wrapper service. |
| II | Classifier starts only inside the Studio node runtime. |
| II-a | Native child process under the existing node service. |
| III | IPC result is a strict Pydantic contract in `coire-core`. |
| IV, V | Pinned local asset; no remote load, credential, or content in telemetry. |
| VI | Existing bounded node stage telemetry records outcome and duration. |
| VII | Contract/unit tests before implementation; real-model integration remains open. |

## Approach

The node supervises a short-lived CPU subprocess with an explicit argv and stripped environment. It enforces a 10-second wall deadline and measured RSS budget, killing the child group on either breach. The child loads only local safetensors, runs inference on CPU, and returns one bounded JSON result. The parent worker will integrate this stage after generation; no admission is enabled by this slice.
