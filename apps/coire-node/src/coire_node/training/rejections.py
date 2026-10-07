"""Durable fenced no-start evidence for a pristine refused native preparation."""

from pathlib import Path

from pydantic import TypeAdapter

from coire_core.errors import TrainingConflict
from coire_core.models.training_node import TrainingPrepareRequest, TrainingStopRequest
from coire_core.models.training_types import TrainingId
from coire_node.store import write_atomic
from coire_node.training.checkpoints import _fsync_directory
from coire_node.training.journal import TrainingJournal
from coire_node.training.process_inventory import attempt_process_absent
from coire_node.training.worker import read_private


class PrepareRejections:
    def __init__(self, journal: TrainingJournal, artifact_root: Path) -> None:
        self.journal, self.artifact_root = journal, artifact_root

    def pristine(self, attempt: str) -> bool:
        return (
            not any(r["attempt_id"] == attempt for r in self.journal.records())
            and not any(
                path.exists() or path.is_symlink()
                for path in (
                    self.journal.root / attempt,
                    self.artifact_root / attempt,
                    self.journal.root / f"measurement-{attempt}.json",
                )
            )
            and attempt_process_absent(attempt)
        )

    def read(self, attempt: str) -> TrainingPrepareRequest | TrainingStopRequest | None:
        TypeAdapter(TrainingId).validate_python(attempt)
        path = self.journal.root / f"rejected-attempt-{attempt}.json"
        if not path.exists() and not path.is_symlink():
            return None
        prepared: TrainingPrepareRequest | TrainingStopRequest = TypeAdapter(
            TrainingPrepareRequest | TrainingStopRequest
        ).validate_json(read_private(path, 1024**2))
        if (
            prepared.attempt_id != attempt
            or prepared.node != self.journal.node
            or not self.pristine(attempt)
        ):
            raise TrainingConflict("Rejected training ownership is unresolved")
        return prepared

    def record(self, command: TrainingPrepareRequest) -> None:
        if command.node != self.journal.node or not self.pristine(command.attempt_id):
            return
        prior = self.read(command.attempt_id)
        if prior is not None:
            if prior != command:
                raise TrainingConflict("Rejected preparation intent changed")
            return
        write_atomic(
            self.journal.root / f"rejected-attempt-{command.attempt_id}.json",
            command.model_dump_json().encode(),
        )
        _fsync_directory(self.journal.root)

    def record_stop(self, command: TrainingStopRequest) -> None:
        """Fence a never-prepared attempt before acknowledging its absent process."""
        if command.node != self.journal.node or not self.pristine(command.attempt_id):
            raise TrainingConflict("Unprepared training ownership is unresolved")
        write_atomic(
            self.journal.root / f"rejected-attempt-{command.attempt_id}.json",
            command.model_dump_json().encode(),
        )
        _fsync_directory(self.journal.root)
