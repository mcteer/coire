"""Actual dump/isolated restore; no production database, network or credentials."""

import json
import os
import runpy
import time
from pathlib import Path
from uuid import uuid4

import pytest

from coire_api.upgrades.backup_preparation import command, prepare_backup, schema_identity
from coire_core.errors import UpgradeUnavailable
from coire_core.upgrade_backups import select_prepared_backup


@pytest.mark.integration
def test_dump_restore_validation_preserves_source_and_removes_its_owned_fixture(
    tmp_path: Path,
) -> None:
    project = "coire-backup-test-" + uuid4().hex[:12]
    image = json.loads(command("image", "inspect", "postgres:17"))[0]["Id"]
    source = command(
        "create",
        "--pull=never",
        "--name",
        project + "-postgres",
        "--network=none",
        "--label",
        "com.docker.compose.project=" + project,
        "--label",
        "com.docker.compose.service=postgres",
        "--restart=no",
        "--tmpfs=/var/lib/postgresql/data:rw,size=1073741824",
        "-e",
        "POSTGRES_USER=coire",
        "-e",
        "POSTGRES_DB=coire",
        "-e",
        "POSTGRES_HOST_AUTH_METHOD=trust",
        image,
    ).strip()
    volume = project + "_coire-upgrade-prepared"
    published = False
    try:
        command("start", source)
        deadline = time.monotonic() + 30
        while True:
            try:
                command(
                    "exec",
                    source,
                    "pg_isready",
                    "-h",
                    "127.0.0.1",
                    "-U",
                    "coire",
                    "-d",
                    "coire",
                    timeout=2,
                )
                break
            except UpgradeUnavailable:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.2)
        command(
            "exec",
            source,
            "psql",
            "-X",
            "-U",
            "coire",
            "-d",
            "coire",
            "-v",
            "ON_ERROR_STOP=1",
            "-c",
            "CREATE TABLE alembic_version (version_num varchar(64) PRIMARY KEY);"
            "INSERT INTO alembic_version VALUES ('0034_upgrades');"
            "CREATE TABLE preservation (id integer PRIMARY KEY, value text NOT NULL);"
            "INSERT INTO preservation VALUES (1, 'retained');",
        )
        before = schema_identity(source)
        evidence = prepare_backup(tmp_path / "backups", project)
        publisher = json.loads(
            command(
                "image",
                "inspect",
                os.environ.get(
                    "COIRE_UPGRADE_EXECUTOR_TEST_IMAGE", "coire-019-upgrade-executor:dev"
                ),
            )
        )[0]["Id"]
        command("volume", "create", "--label", "com.docker.compose.project=" + project, volume)
        published = True
        script = runpy.run_path(
            str(Path(__file__).resolve().parents[2] / "scripts/coire-deploy.py")
        )
        assert (
            script["publish_backup_catalog"](tmp_path / "backups", evidence, image_id=publisher)
            == evidence
        )
        assert (
            script["publish_backup_catalog"](tmp_path / "backups", evidence, image_id=publisher)
            == evidence
        )
        probe = (
            "import sys; from pathlib import Path; "
            "from coire_core.upgrade_backups import select_prepared_backup; "
            "print(select_prepared_backup(Path('/opt/coire/upgrades/prepared/backups'), "
            "project=sys.argv[1], schema_head=sys.argv[2], schema_sha256=sys.argv[3]).model_dump_json())"
        )
        probe_command = (
            "run",
            "--rm",
            "--pull=never",
            "--network=none",
            "--read-only",
            "--user=65532:65532",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges:true",
            "--mount",
            f"type=volume,source={volume},target=/opt/coire/upgrades/prepared,readonly",
            "--entrypoint=/app/.venv/bin/python3",
            publisher,
            "-c",
        )
        observed = command(*probe_command, probe, project, before[0], before[1])
        assert json.loads(observed) == evidence.model_dump(mode="json")
        # A real published catalog mounted into the executor must not grant a
        # different Compose project access, even when its schema is identical.
        foreign_probe = "\n".join(
            (
                "import sys",
                "from pathlib import Path",
                "from coire_core.errors import UpgradeConflict",
                "from coire_core.upgrade_backups import select_prepared_backup",
                "try:",
                "    select_prepared_backup(Path('/opt/coire/upgrades/prepared/backups'), "
                "project=sys.argv[1], schema_head=sys.argv[2], schema_sha256=sys.argv[3])",
                "except UpgradeConflict:",
                "    print('foreign-project-rejected')",
                "else:",
                "    raise AssertionError('Foreign project acquired a prepared backup')",
            )
        )
        assert (
            command(
                *probe_command, foreign_probe, project + "-foreign", before[0], before[1]
            ).strip()
            == "foreign-project-rejected"
        )
        # Rejection must leave the original receipt and dump usable unchanged.
        assert json.loads(command(*probe_command, probe, project, before[0], before[1])) == (
            evidence.model_dump(mode="json")
        )
        assert (
            select_prepared_backup(
                tmp_path / "backups",
                project=project,
                schema_head=before[0],
                schema_sha256=before[1],
            )
            == evidence
        )
        assert schema_identity(source) == before
        assert (
            command(
                "exec",
                source,
                "psql",
                "-X",
                "-A",
                "-t",
                "-U",
                "coire",
                "-d",
                "coire",
                "-c",
                "SELECT value FROM preservation WHERE id = 1",
            ).strip()
            == "retained"
        )
        assert (
            command(
                "ps", "-a", "-q", "--filter", "label=coire.upgrade.backup.id=" + evidence.backup_id
            ).strip()
            == ""
        )
    finally:
        command("stop", "--time=5", source)
        command("rm", source)
        if published:
            command("volume", "rm", volume)
