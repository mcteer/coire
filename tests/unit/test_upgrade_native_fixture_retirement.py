"""Fixture retirement refuses foreign roots before touching process ownership."""

from pathlib import Path

import pytest
from integration.upgrade_native_fixture_retirement import retire_adopted_fixture

from coire_core.errors import UpgradeUnavailable
from coire_core.settings import Settings


@pytest.mark.parametrize(
    "fault", ["platform", "architecture", "name", "parent", "permissions", "linked"]
)
def test_fixture_retirement_refuses_foreign_scope_without_any_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    from integration import upgrade_native_fixture_retirement as producer

    root = tmp_path / "coire-019-acceptance/native-upgrade-lab-unit"
    root.mkdir(parents=True, mode=0o700)
    monkeypatch.setattr(producer.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(producer.platform, "machine", lambda: "arm64")
    if fault == "platform":
        monkeypatch.setattr(producer.platform, "system", lambda: "Linux")
    elif fault == "architecture":
        monkeypatch.setattr(producer.platform, "machine", lambda: "x86_64")
    elif fault == "name":
        root = root.rename(root.parent / "operator-node")
    elif fault == "parent":
        root = root.rename(tmp_path / "native-upgrade-lab-unit")
    elif fault == "permissions":
        root.chmod(0o755)
    elif fault == "linked":
        alias = root.parent / "native-upgrade-lab-link"
        alias.symlink_to(root, target_is_directory=True)
        root = alias
    settings = Settings(
        node_name="coire-edge-a",
        upgrade_bootstrap_root=str(root / "upgrade-bootstrap"),
        upgrade_environment_root=str(root / "envs"),
        node_state_dir=str(root / "state"),
    )
    with pytest.raises(UpgradeUnavailable, match="owned isolated native root"):
        retire_adopted_fixture(settings, "01ARZ3NDEKTSV4RRFFQ69G5FAW")
    assert list(root.iterdir()) == []
