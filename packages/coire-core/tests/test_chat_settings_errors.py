"""Bounded chat settings and safe domain failures."""

import pytest
from pydantic import ValidationError

from coire_core.errors import ChatConflict, ChatNotFound, ChatQuotaExceeded
from coire_core.settings import Settings


def test_chat_limits_and_worker_secret_defaults() -> None:
    settings = Settings(_secrets_dir="/nonexistent")  # type: ignore[call-arg]
    assert settings.chat_upload_max_bytes == 10 * 1024 * 1024
    assert settings.chat_pdf_max_pages == 50
    assert settings.file_worker_service_token.get_secret_value() == ""
    with pytest.raises(ValidationError):
        Settings(_secrets_dir="/nonexistent", chat_upload_max_bytes=0)  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        Settings(_secrets_dir="/nonexistent", chat_pdf_max_pages=100)  # type: ignore[call-arg]
    assert settings.chat_derived_job_max_bytes == 32 * 1024 * 1024
    assert settings.chat_normalized_max_pixels == 4_000_000
    assert settings.file_worker_process_timeout_s == 30
    for field, value in (
        ("chat_derived_job_max_bytes", 32 * 1024 * 1024 + 1),
        ("chat_normalized_max_pixels", 4_000_001),
        ("chat_purge_deadline_hours", 25),
        ("file_worker_process_timeout_s", 31),
        ("file_worker_max_active", 2),
    ):
        with pytest.raises(ValidationError):
            Settings(_secrets_dir="/nonexistent", **{field: value})  # type: ignore[arg-type,call-arg]


def test_chat_browser_origin_is_exact() -> None:
    for valid in ("https://chat.example.test", "http://localhost:8080"):
        created = Settings(_secrets_dir="/nonexistent", chat_browser_origin=valid)  # type: ignore[call-arg]
        assert created.chat_browser_origin == valid
    for invalid in (
        "http://chat.example.test",
        "https://chat.example.test/",
        "https://chat.example.test/path",
        "https://user@chat.example.test",
    ):
        with pytest.raises(ValidationError):
            Settings(_secrets_dir="/nonexistent", chat_browser_origin=invalid)  # type: ignore[call-arg]


def test_chat_errors_expose_safe_problem_details() -> None:
    assert ChatNotFound().to_problem().status == 404
    assert ChatConflict("active turn").to_problem().status == 409
    quota = ChatQuotaExceeded()
    assert quota.to_problem().status == 413
    assert "traceback" not in quota.to_problem().model_dump_json()
