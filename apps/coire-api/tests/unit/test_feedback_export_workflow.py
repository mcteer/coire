"""Durable recovery errors cannot serialize copied feedback into DBOS history."""

import pytest

from coire_scheduler import feedback_exports


async def test_durable_export_exception_is_content_free(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def execute(_identity: str, _settings: object) -> str:
        raise ValueError("private prompt and candidate")

    monkeypatch.setattr(feedback_exports, "execute_export", execute)
    with pytest.raises(RuntimeError, match=r"^Feedback export recovery unavailable$"):
        await feedback_exports.export_tick.__wrapped__.__wrapped__("01AAAAAAAAAAAAAAAAAAAAAAAA")  # type: ignore[attr-defined]
    assert "private prompt" not in caplog.text and "candidate" not in caplog.text
    assert caplog.records[-1].__dict__["safe_reason"] == "ValueError"
