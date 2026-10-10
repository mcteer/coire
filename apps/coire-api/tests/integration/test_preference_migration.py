"""Frozen additive migration protects legacy history and retained feedback."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine
from test_evaluation_migration import assert_legacy, seed_legacy

pytestmark = pytest.mark.integration
ALEMBIC_CONFIG = Path(__file__).resolve().parents[2] / "alembic.ini"
OWNER = "00000000-0000-4000-8000-000000000001"


async def test_preference_upgrade_constraints_and_drained_downgrade(
    training_postgres_url: str, monkeypatch: pytest.MonkeyPatch
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
    config = Config(str(ALEMBIC_CONFIG))
    engine = create_async_engine(training_postgres_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(text("DROP SCHEMA public CASCADE"))
            await connection.execute(text("CREATE SCHEMA public"))
        await asyncio.to_thread(command.upgrade, config, "0032_evaluation_verbs")
        async with engine.begin() as connection:
            await connection.execute(
                text("""INSERT INTO users
                (id,email,display_name,role,active) VALUES
                (:owner,'feedback@test.local','Owner','user',true)"""),
                {"owner": OWNER},
            )
            await seed_legacy(connection)
            await connection.execute(
                text(
                    "INSERT INTO chat_conversations (id,owner_user_id,title,mode) VALUES (gen_random_uuid(),:owner,'preserved chat','chat')"
                ),
                {"owner": OWNER},
            )
            before = await connection.scalar(text("SELECT count(*) FROM evaluation_suites"))
        await asyncio.to_thread(command.upgrade, config, "0033_preference_feedback")
        async with engine.begin() as connection:
            await assert_legacy(connection)
            assert (
                await connection.scalar(text("SELECT title FROM chat_conversations"))
                == "preserved chat"
            )
            assert await connection.scalar(text("SELECT count(*) FROM evaluation_suites")) == before
            await connection.execute(
                text("INSERT INTO feedback_preferences (owner_user_id) VALUES (:owner)"),
                {"owner": OWNER},
            )
        async with engine.connect() as connection:
            with pytest.raises(IntegrityError):
                await connection.execute(
                    text("INSERT INTO feedback_preferences (owner_user_id) VALUES (:owner)"),
                    {"owner": OWNER},
                )
            await connection.rollback()
            with pytest.raises(IntegrityError):
                await connection.execute(
                    text("UPDATE feedback_preferences SET capture_generation=0")
                )
            await connection.rollback()
            await connection.execute(
                text("""INSERT INTO comparison_pairs
                (id,owner_user_id,conversation_id,source_message_id,source_turn_id,
                 capture_generation,context_revision,client_request_id,request_sha256,expires_at)
                VALUES ('01ARZ3NDEKTSV4RRFFQ69G5FAV',:owner,gen_random_uuid(),
                 gen_random_uuid(),gen_random_uuid(),1,1,gen_random_uuid(),repeat('a',64),now()+interval '1 day')"""),
                {"owner": OWNER},
            )
            await connection.commit()
            with pytest.raises(IntegrityError, match="uq_comparison_pending"):
                await connection.execute(
                    text("""INSERT INTO comparison_pairs
                    (id,owner_user_id,conversation_id,source_message_id,source_turn_id,
                     capture_generation,context_revision,client_request_id,request_sha256,expires_at)
                    SELECT '01ARZ3NDEKTSV4RRFFQ69G5FAW',owner_user_id,conversation_id,source_message_id,
                    source_turn_id,1,1,gen_random_uuid(),request_sha256,expires_at FROM comparison_pairs""")
                )
            await connection.rollback()
        with pytest.raises(RuntimeError, match="retained"):
            await asyncio.to_thread(command.downgrade, config, "0032_evaluation_verbs")
        async with engine.begin() as connection:
            await connection.execute(text("DELETE FROM comparison_pairs"))
            await connection.execute(text("DELETE FROM feedback_preferences"))
        await asyncio.to_thread(command.downgrade, config, "0032_evaluation_verbs")
        async with engine.connect() as connection:
            await assert_legacy(connection)
            assert (
                await connection.scalar(text("SELECT title FROM chat_conversations"))
                == "preserved chat"
            )
            assert await connection.scalar(text("SELECT count(*) FROM evaluation_suites")) == before
            assert (
                await connection.scalar(
                    text("SELECT display_name FROM users WHERE id=:owner"), {"owner": OWNER}
                )
                == "Owner"
            )
    finally:
        await engine.dispose()
