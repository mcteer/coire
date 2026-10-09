"""Full orphan discovery keeps arbitrary executable names and unknown identities visible."""

from pathlib import Path
from types import SimpleNamespace

import psutil
import pytest

from coire_core.models.engine import EngineState
from coire_core.settings import Settings
from coire_node import engines
from coire_node.store import Store


@pytest.mark.parametrize("creation_known", [True, False])
def test_orphan_scan_reads_identity_only_after_full_command_line_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, creation_known: bool
) -> None:
    settings = Settings(_secrets_dir="/nonexistent", node_state_dir=str(tmp_path / "state"))  # type: ignore[call-arg]
    store = Store(str(tmp_path / "models"))
    store.ensure_root()
    manager = engines.EngineManager(settings, store, "127.0.0.1")
    manager._engines["owned"] = SimpleNamespace(pid=99)  # type: ignore[assignment]

    class Process:
        def __init__(self, pid: int) -> None:
            self.pid = pid

        def cmdline(self) -> list[str]:
            assert self.pid != 99, "Owned process metadata need not be scanned again"
            if self.pid == 2:
                raise psutil.AccessDenied(self.pid)
            if self.pid == 3:
                return [
                    "/arbitrarily-renamed-runtime",
                    "-m",
                    "mlx_vlm.server",
                    "--model",
                    str(store.root / "model"),
                    "--port",
                    "9501",
                ]
            return ["unrelated-program"]

        def create_time(self) -> float:
            assert self.pid == 3, "Unrelated processes must not require creation metadata"
            if not creation_known:
                raise psutil.AccessDenied(self.pid)
            return 123.0

    monkeypatch.setattr(psutil, "process_iter", lambda: iter(Process(pid) for pid in (99, 1, 2, 3)))
    monkeypatch.setattr(manager, "_sample", lambda engine: None)

    def alive(pid: int | None, created: float | None, *, needle: str | None = None) -> bool:
        assert pid == 3 and needle == str(store.root)
        assert created == (123.0 if creation_known else None)
        return True

    monkeypatch.setattr(engines, "_alive", alive)
    found = manager.find_orphans()
    assert len(found) == 1
    assert found[0].state is EngineState.ORPHAN
    assert found[0].pid == 3
    assert found[0].backend.value == "mlx_vlm"
    assert found[0].process_create_time == (123.0 if creation_known else None)
