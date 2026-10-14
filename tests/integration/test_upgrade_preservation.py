"""Actual additive migrations preserve populated history and mounted credentials."""

import asyncio
import hashlib
import json
import os
import runpy
import secrets
import subprocess
import sys
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

pytestmark = pytest.mark.integration
REPO = Path(__file__).resolve().parents[2]
OWNER = "00000000-0000-4000-8000-000000000001"
MODEL = "10000000-0000-4000-8000-000000000001"
VARIANT = "10000000-0000-4000-8000-000000000002"
DATASET = "10000000-0000-4000-8000-000000000003"
TABLES = (
    "users",
    "training_jobs",
    "training_attempts",
    "training_checkpoints",
    "training_adapters",
    "training_dataset_revisions",
    "training_dataset_analyses",
    "feedback_preferences",
    "comparison_pairs",
    "harness_evaluations",
)


async def snapshot(connection: AsyncConnection) -> dict[str, object]:
    result = {}
    for table in TABLES:
        projection = "to_jsonb(t)"
        if table == "harness_evaluations":
            projection += " - 'runtime_sha256' - 'harness_image'"
        result[table] = await connection.scalar(
            text(
                "SELECT coalesce(jsonb_agg("
                + projection
                + " ORDER BY "
                + projection
                + "::text), '[]'::jsonb) FROM "
                + table
                + " t"
            )
        )
        assert result[table], "preservation requires populated " + table
    return result


async def test_populated_018_history_and_live_secret_mount_survive_additive_upgrade(
    training_postgres_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = make_url(training_postgres_url)
    assert url.database == "training_test" and url.host == "127.0.0.1" and url.password
    for key, value in {
        "POSTGRES_HOST": url.host,
        "POSTGRES_PORT": str(url.port),
        "POSTGRES_USER": url.username or "postgres",
        "POSTGRES_DB": url.database,
        "POSTGRES_PASSWORD": url.password,
    }.items():
        monkeypatch.setenv(key, value)
    config = Config(str(REPO / "apps/coire-api/alembic.ini"))
    engine = create_async_engine(training_postgres_url)
    secret = tmp_path / "retained-token"
    secret.write_text(secrets.token_urlsafe(32))
    secret.chmod(0o600)
    artifacts = [tmp_path / "published-dataset.jsonl", tmp_path / "published-adapter.blob"]
    for path in artifacts:
        path.write_bytes(b"synthetic immutable preservation fixture: " + path.name.encode())
        path.chmod(0o600)
    before_files = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in artifacts}
    secret_digest = hashlib.sha256(secret.read_bytes()).hexdigest()
    image = os.environ.get("COIRE_UPGRADE_LOCK_TEST_IMAGE", "coire-api:ci")
    pinned_bytes = await asyncio.to_thread(
        subprocess.check_output,
        ["docker", "image", "inspect", image, "--format", "{{.Id}}"],
        timeout=10,
    )
    pinned = pinned_bytes.decode().strip()
    user = "65532:65532" if sys.platform == "darwin" else f"{os.getuid()}:{os.getgid()}"
    assert user.split(":")[0] != "0"
    container_bytes = await asyncio.to_thread(
        subprocess.check_output,
        [
            "docker",
            "run",
            "--detach",
            "--pull=never",
            "--network=none",
            "--read-only",
            "--user=" + user,
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges:true",
            "--pids-limit=32",
            "--memory=128m",
            "--cpus=0.5",
            "--no-healthcheck",
            "--mount",
            "type=bind,source=" + str(secret) + ",target=/retained-token,readonly",
            pinned,
            "-B",
            "-c",
            "import time; time.sleep(300)",
        ],
        timeout=15,
    )
    container = container_bytes.decode().strip()

    def mounted() -> tuple[object, str]:
        info = json.loads(subprocess.check_output(["docker", "inspect", container], timeout=10))[0]
        assert info["State"]["Running"] and info["Image"] == pinned
        digest = (
            subprocess.check_output(
                [
                    "docker",
                    "exec",
                    container,
                    "/app/.venv/bin/python3",
                    "-B",
                    "-c",
                    "import hashlib; from pathlib import Path; print(hashlib.sha256(Path('/retained-token').read_bytes()).hexdigest())",
                ],
                timeout=10,
            )
            .decode()
            .strip()
        )
        assert digest == secret_digest
        return info["Mounts"], digest

    try:
        before_mount = await asyncio.to_thread(mounted)
        async with engine.begin() as connection:
            await connection.execute(text("DROP SCHEMA public CASCADE"))
            await connection.execute(text("CREATE SCHEMA public"))
        await asyncio.to_thread(command.upgrade, config, "0033_preference_feedback")
        legacy = runpy.run_path(
            str(REPO / "apps/coire-api/tests/integration/test_evaluation_migration.py")
        )
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO users (id,email,display_name,role,active) VALUES (:owner,'preservation@isolated.test','Retained owner','user',true)"
                ),
                {"owner": OWNER},
            )
            await legacy["seed_legacy"](connection)
            await connection.execute(
                text(
                    "INSERT INTO training_dataset_revisions (id,owner_user_id,name,format,state,source_sha256,source_bytes,storage_key,provenance,row_count,split_seed,validation_fraction) VALUES (:id,:owner,'Published fixture','preference_pairs','ready',repeat('a',64),100,'preserved-dataset',jsonb_build_object('synthetic',true),2,0,0.5)"
                ),
                {"id": DATASET, "owner": OWNER},
            )
            await connection.execute(
                text(
                    "INSERT INTO training_dataset_analyses (id,dataset_id,model_id,variant_id,command_id,identity_sha256,runtime_sha256,state,result) VALUES (gen_random_uuid(),:dataset,:model,:variant,gen_random_uuid(),repeat('a',64),repeat('b',64),'succeeded',jsonb_build_object('synthetic_history',true))"
                ),
                {"dataset": DATASET, "model": MODEL, "variant": VARIANT},
            )
            await connection.execute(
                text(
                    "INSERT INTO training_adapters (id,model_id,base_variant_id,source_job_id,slug,selector,base_manifest_sha256,manifest_sha256,resolved_spec_sha256,parameterization,state,visibility,verified) VALUES (gen_random_uuid(),:model,:variant,'01ARZ3NDEKTSV4RRFFQ69G5FAV','preserved','fixture@preserved',repeat('a',64),repeat('b',64),repeat('c',64),'lora','ready','published',false)"
                ),
                {"model": MODEL, "variant": VARIANT},
            )
            await connection.execute(
                text(
                    "INSERT INTO feedback_preferences (owner_user_id,enabled,capture_generation,version,withdrawn_at) VALUES (:owner,false,2,2,now())"
                ),
                {"owner": OWNER},
            )
            await connection.execute(
                text(
                    "INSERT INTO comparison_pairs (id,owner_user_id,conversation_id,source_turn_id,source_message_id,capture_generation,context_revision,client_request_id,request_sha256,expires_at,generation_state,selection_state,original,candidate,withdrawn_at,purge_after) VALUES ('01ARZ3NDEKTSV4RRFFQ69G5FAW',:owner,gen_random_uuid(),gen_random_uuid(),gen_random_uuid(),1,1,gen_random_uuid(),repeat('a',64),now(),'withdrawn','withdrawn','private source fixture','private candidate fixture',now(),now())"
                ),
                {"owner": OWNER},
            )
            await connection.execute(
                text(
                    "INSERT INTO harness_evaluations (id,variant_id,scores,overall_score,verdict,harness_version,engine_version,diagnostics) VALUES (gen_random_uuid(),:variant,'{}',0,'failed','legacy','legacy','[]')"
                ),
                {"variant": VARIANT},
            )
            before = await snapshot(connection)
        for revision in ("0034_upgrades", "0033_preference_feedback"):
            await asyncio.to_thread(
                command.upgrade if revision == "0034_upgrades" else command.downgrade,
                config,
                revision,
            )
            async with engine.connect() as connection:
                assert await snapshot(connection) == before
                await legacy["assert_legacy"](connection)
            assert await asyncio.to_thread(mounted) == before_mount
            assert {
                str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in artifacts
            } == before_files
        with pytest.raises(RuntimeError, match="retained"):
            await asyncio.to_thread(command.downgrade, config, "0032_evaluation_verbs")
        async with engine.connect() as connection:
            assert await snapshot(connection) == before
        assert await asyncio.to_thread(mounted) == before_mount
    finally:
        await engine.dispose()
        await asyncio.to_thread(
            subprocess.run,
            ["docker", "rm", "--force", container],
            capture_output=True,
            check=True,
            timeout=15,
        )
        secret.unlink()
