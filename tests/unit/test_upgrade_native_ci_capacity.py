"""Disk preparation must preserve selected tools and reject foreign applications."""

import plistlib
from pathlib import Path

import pytest
from integration.upgrade_native_ci_capacity import unused_xcodes


def _xcode(root: Path, name: str, identity: str = "com.apple.dt.Xcode") -> Path:
    app = root / name
    developer = app / "Contents/Developer"
    developer.mkdir(parents=True)
    with (app / "Contents/Info.plist").open("wb") as metadata:
        plistlib.dump({"CFBundleIdentifier": identity}, metadata)
    return app


def test_selected_xcode_is_preserved_even_when_selected_through_alias(tmp_path: Path) -> None:
    active = _xcode(tmp_path, "Xcode_26.app")
    older = _xcode(tmp_path, "Xcode_25.app")
    alias = tmp_path / "active-tools"
    alias.symlink_to(active / "Contents/Developer")
    assert unused_xcodes(tmp_path, alias) == [older]


@pytest.mark.parametrize("linked", [False, True])
def test_unused_xcode_cannot_authorize_a_foreign_or_linked_application(
    tmp_path: Path, linked: bool
) -> None:
    active = _xcode(tmp_path, "Xcode_26.app")
    foreign = _xcode(tmp_path, "Foreign.app", "org.example.other")
    if linked:
        (tmp_path / "Xcode_old.app").symlink_to(foreign)
    else:
        foreign.rename(tmp_path / "Xcode_old.app")
    with pytest.raises(RuntimeError, match=r"linked|different application identity"):
        unused_xcodes(tmp_path, active / "Contents/Developer")
