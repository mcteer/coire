"""Publish administrative bootstrap intent; readiness still requires physical proof."""

import json
import sys
from pathlib import Path
from typing import BinaryIO

from coire_core.errors import CoireError, UpgradeConflict
from coire_core.models.upgrades import CoreBootstrapSeed, UpgradeReleaseRegister
from coire_core.settings import Settings
from coire_core.upgrade_artifacts import verify_prepared_release
from coire_core.upgrade_journal import PrivateUpgradeStore

MAX_SEED_BYTES = 1024 * 1024


def publish_bootstrap_intent(
    stream: BinaryIO, prepared_root: Path, state_root: Path, *, project: str
) -> CoreBootstrapSeed:
    payload = stream.read(MAX_SEED_BYTES + 1)
    if len(payload) > MAX_SEED_BYTES:
        raise UpgradeConflict("Administrative bootstrap intent exceeds its bound")

    def unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate bootstrap field")
            result[key] = value
        return result

    try:
        json.loads(payload, object_pairs_hook=unique)
        seed = CoreBootstrapSeed.model_validate_json(payload)
    except ValueError:
        raise UpgradeConflict("Administrative bootstrap intent is malformed") from None
    if seed.source.manifest.project != project:
        raise UpgradeConflict("Administrative bootstrap intent belongs to another project")
    for release in (seed.source, seed.executor_source):
        verified = verify_prepared_release(
            prepared_root,
            UpgradeReleaseRegister(
                prepared_artifact_id=release.prepared_artifact_id,
                expected_sha256=release.manifest_sha256,
            ),
        )
        if verified != release:
            raise UpgradeConflict("Administrative bootstrap catalog differs from sealed intent")
    store = PrivateUpgradeStore(state_root / "bootstrap")
    with store.ownership("seed.lock"):
        initial = store.read("initial-seed.json", CoreBootstrapSeed)
        selected = store.read("selected-seed.json", CoreBootstrapSeed)
        requested = store.read("initial-seed-request.json", CoreBootstrapSeed)
        if (
            selected is not None
            or (initial is not None and initial != seed)
            or (requested is not None and requested != seed)
        ):
            raise UpgradeConflict(
                "Administrative bootstrap cannot replace retained recovery ownership"
            )
        if requested is None:
            store.write("initial-seed-request.json", seed, replace=False)
    return seed


def main() -> int:
    settings = Settings()
    try:
        seed = publish_bootstrap_intent(
            sys.stdin.buffer,
            Path(settings.upgrade_prepared_dir),
            Path(settings.upgrade_state_dir),
            project=settings.upgrade_project,
        )
    except (CoireError, OSError, ValueError):
        print("administrative bootstrap publication failed", file=sys.stderr)
        return 2
    print(seed.model_dump_json())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
