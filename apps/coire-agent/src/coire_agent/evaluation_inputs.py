"""Read bounded digest-bound private mounts; never execute or follow input links."""

import hashlib
import os
import stat
from pathlib import Path

from coire_core.models.evaluation import (
    EvaluationOutput,
    EvaluationWorkerResult,
    EvaluationWorkload,
)


def read_previous_outputs(workload: EvaluationWorkload, root: Path) -> list[EvaluationOutput]:
    if root.is_symlink():
        raise ValueError("evaluation inputs must not be linked")
    outputs: list[EvaluationOutput] = []
    for item in workload.input_files:
        if item.purpose != "previous_outputs":
            continue
        descriptor = os.open(root / item.name, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as source:
            info = os.fstat(source.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or info.st_size != item.bytes
                or info.st_size > 8 * 1024**2
            ):
                raise ValueError("evaluation input is unsafe")
            data = source.read(item.bytes + 1)
        if len(data) != item.bytes or hashlib.sha256(data).hexdigest() != item.sha256:
            raise ValueError("evaluation input differs from declared digest")
        try:
            worker = EvaluationWorkerResult.model_validate_json(data)
        except ValueError:
            raise ValueError("prior evaluation evidence has unsupported shape") from None
        if (
            worker.evaluation_id != workload.evaluation_id
            or worker.fence != workload.fence
            or worker.suite_sha256 != workload.suite.content_sha256
            or worker.cases_sha256 != workload.suite.template.cases_sha256
            or worker.outcome != "succeeded"
            or worker.phase not in {"base", "candidate"}
        ):
            raise ValueError("prior evaluation evidence differs from current workload")
        outputs.extend(worker.outputs)
    return outputs


def read_input(workload: EvaluationWorkload, root: Path, name: str) -> bytes:
    """Read one exact declared inode; no caller paths or undeclared private files."""
    item = next((item for item in workload.input_files if item.name == name), None)
    if item is None or root.is_symlink():
        raise ValueError("private evaluation input is undeclared")
    descriptor = os.open(root / name, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size != item.bytes:
            raise ValueError("private evaluation input is unsafe")
        data = source.read(item.bytes + 1)
    if len(data) != item.bytes or hashlib.sha256(data).hexdigest() != item.sha256:
        raise ValueError("private evaluation input digest differs")
    return data
