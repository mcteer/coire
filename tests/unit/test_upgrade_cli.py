"""CLI retries retain explicit identity and watches reconnect without resubmission."""

import json
from datetime import UTC, datetime
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from upgrade_fixtures import plan

from coire_api.cli import _parser
from coire_api.upgrades import cli
from coire_core.client import UpgradeClient
from coire_core.errors import UpgradeUnavailable
from coire_core.models.upgrade_recovery import UpgradeExpiredRecoveryRequest
from coire_core.models.upgrades import UpgradeEventPage, UpgradePhase


def test_start_requires_maintenance_acknowledgment_and_reusable_request_identity() -> None:
    for arguments in (
        ["upgrades", "node", "coire-edge-a"],
        ["upgrades", "rollback", "../foreign"],
        ["upgrades", "status", "invalid"],
        ["upgrades", "status", "01ARZ3NDEKTSV4RRFFQ69G5FAV", "--timeout", "nan"],
    ):
        with pytest.raises(SystemExit):
            _parser().parse_args(arguments)


@pytest.mark.parametrize("payload", [b"{}", b'{"deadline_at":"caller"}', b"x" * (512 * 1024 + 1)])
def test_recovery_json_input_is_strict_and_bounded(monkeypatch, payload):
    monkeypatch.setattr(cli.sys, "stdin", SimpleNamespace(buffer=BytesIO(payload)))
    with pytest.raises(UpgradeUnavailable):
        cli.input_model(UpgradeExpiredRecoveryRequest)


async def test_lost_start_reply_does_not_retry_and_explicit_retry_uses_same_key(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    value = plan()
    arguments = _parser().parse_args(
        [
            "--api-url",
            "http://gateway.test",
            "--token",
            "human-test-key",
            "upgrades",
            "node",
            "coire-edge-a",
            "--release",
            str(value.target.release_id),
            "--expected-source",
            value.operation.source_sha256,
            "--idempotency-key",
            str(value.operation.idempotency_key),
            "--acknowledge-maintenance",
            "--json",
        ]
    )
    bodies: list[bytes] = []

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST" and request.url.path.endswith("/nodes/coire-edge-a")
        bodies.append(request.content)
        if len(bodies) == 1:
            raise httpx.ReadError("lost response", request=request)
        return httpx.Response(200, json=value.operation.model_dump(mode="json"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle), trust_env=False) as http:
        monkeypatch.setattr(
            cli, "UpgradeClient", lambda url, token: UpgradeClient(url, token, client=http)
        )
        with pytest.raises(UpgradeUnavailable):
            await cli.execute(arguments)
        assert len(bodies) == 1
        assert await cli.execute(arguments) == 0
    assert len(bodies) == 2 and bodies[0] == bodies[1]
    output = capsys.readouterr()
    assert json.loads(output.out)["job_id"] == value.operation.job_id
    assert str(value.operation.idempotency_key) in output.err


@pytest.mark.parametrize("terminal", [UpgradePhase.SUCCEEDED, UpgradePhase.ROLLED_BACK])
async def test_watch_reconnects_by_job_id_and_retains_event_cursor(
    monkeypatch: pytest.MonkeyPatch,
    terminal: UpgradePhase,
) -> None:
    value = plan()
    arguments = _parser().parse_args(
        [
            "--api-url",
            "http://gateway.test",
            "--token",
            "human-test-key",
            "upgrades",
            "status",
            value.operation.job_id,
            "--watch",
            "--json",
        ]
    )
    queries: list[int] = []

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET" and request.url.path.endswith(
            f"/{value.operation.job_id}/events"
        )
        queries.append(int(request.url.params["after"]))
        if len(queries) == 2:
            return httpx.Response(503)
        operation = (
            value.operation
            if len(queries) == 1
            else value.operation.model_copy(
                update={
                    "phase": terminal,
                    "receipt_sequence": 2,
                    "updated_at": datetime.now(UTC),
                    "finished_at": datetime.now(UTC),
                }
            )
        )
        page = UpgradeEventPage(
            operation=operation, events=(), cursor=1 if len(queries) == 1 else 2
        )
        return httpx.Response(200, json=page.model_dump(mode="json"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle), trust_env=False) as http:
        monkeypatch.setattr(
            cli, "UpgradeClient", lambda url, token: UpgradeClient(url, token, client=http)
        )
        monkeypatch.setattr(cli.asyncio, "sleep", AsyncMock())
        assert await cli.execute(arguments) == (0 if terminal is UpgradePhase.SUCCEEDED else 1)
    assert queries == [0, 1, 1]


@pytest.mark.parametrize("mismatch", [False, True])
async def test_executor_handover_retry_and_status_keep_exact_identity(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mismatch: bool,
) -> None:
    from datetime import timedelta
    from uuid import uuid4

    from coire_core.errors import UpgradeConflict
    from coire_core.models.upgrades import CoreHandoverSummary

    value = plan()
    now = datetime.now(UTC)
    result = CoreHandoverSummary(
        handover_id=uuid4(),
        project="coire",
        generation=2,
        phase="accepted",
        preparation_sha256="a" * 64,
        candidate_release_id=value.target.release_id,
        incumbent_image_id="sha256:" + "b" * 64,
        candidate_image_id="sha256:" + "c" * 64,
        prepared_at=now,
        deadline_at=now + timedelta(seconds=120),
    )
    arguments = _parser().parse_args(
        [
            "--api-url",
            "http://gateway.test",
            "--token",
            "human-test-key",
            "upgrades",
            "executor-handover",
            "--release",
            str(value.target.release_id),
            "--expected-incumbent-seed",
            "d" * 64,
            "--idempotency-key",
            str(value.operation.idempotency_key),
            "--json",
        ]
    )
    bodies: list[bytes] = []

    def handle(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            assert request.url.path.endswith("/executor-handover")
            bodies.append(request.content)
            if len(bodies) == 1:
                raise httpx.ReadError("lost response", request=request)
            returned = (
                result.model_copy(update={"candidate_release_id": uuid4()}) if mismatch else result
            )
        else:
            assert request.url.path.endswith("/executor-handover/" + str(result.handover_id))
            returned = result.model_copy(update={"handover_id": uuid4()}) if mismatch else result
        return httpx.Response(200, json=returned.model_dump(mode="json"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle), trust_env=False) as http:
        monkeypatch.setattr(
            cli, "UpgradeClient", lambda url, token: UpgradeClient(url, token, client=http)
        )
        with pytest.raises(UpgradeUnavailable):
            await cli.execute(arguments)
        assert len(bodies) == 1
        if mismatch:
            with pytest.raises(UpgradeConflict):
                await cli.execute(arguments)
        else:
            assert await cli.execute(arguments) == 0
        status = _parser().parse_args(
            [
                "--api-url",
                "http://gateway.test",
                "--token",
                "human-test-key",
                "upgrades",
                "executor-status",
                str(result.handover_id),
                "--json",
            ]
        )
        if mismatch:
            with pytest.raises(UpgradeConflict):
                await cli.execute(status)
        else:
            assert await cli.execute(status) == 0
    assert bodies[0] == bodies[1]
    assert json.loads(bodies[0])["expected_incumbent_seed_sha256"] == "d" * 64
    assert str(value.operation.idempotency_key) in capsys.readouterr().err
