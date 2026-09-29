# Tasks: Locked Native Node Install

- [X] B001 Export and verify exact locked macOS arm64 node wheels in `scripts/build-node-wheel.sh` and `scripts/stage-node-wheels.py`.
- [X] B002 Install the offline wheel graph into an immutable versioned environment and smoke before activation in `apps/coire-node/install.sh` and `install_runtime.py`.
- [X] B003 Cover successful activation, failed smoke and digest refusal in `tests/unit/test_node_install.py`.
- [X] B004 Document operator staging, installation and rollback in `docs/runbooks/instances.md` and the Chat runbook.
- [X] B005 Run repository gates and disposable local installation, then record evidence in the parent review and close T060.
