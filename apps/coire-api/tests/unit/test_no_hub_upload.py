from __future__ import annotations

from pathlib import Path


def test_control_plane_contains_no_hub_publish_or_upload_operation() -> None:
    roots = [
        Path(__file__).resolve().parents[2] / "src" / "coire_api",
        Path(__file__).resolve().parents[2] / "src" / "coire_scheduler",
    ]
    forbidden = ("upload_file(", "upload_folder(", "create_repo(", "create_commit(")
    offenders: list[str] = []
    for root in roots:
        for path in root.rglob("*.py"):
            text = path.read_text()
            if any(token in text for token in forbidden):
                offenders.append(str(path.relative_to(root)))
    assert offenders == []
