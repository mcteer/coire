"""The live harness probe must test each capability without chat-template noise."""

import uuid

from coire_api import cli
from coire_core.models.harness import EvaluationVerdict, HarnessEvaluationTarget
from coire_core.models.registry import CapabilityProfile


def test_harness_probe_keeps_all_four_strict_assertions(monkeypatch: object) -> None:
    requests: list[dict[str, object]] = []
    contents = [
        '{"name":"read_file","arguments":{"path":"README.md"}}',
        '{"answer":"ok"}',
        "--- a/note.txt\n+++ b/note.txt\n+coire-eval",
        "coire-context-sentinel-7419",
    ]

    class Response:
        def __init__(self, content: str) -> None:
            self.content = content

        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict[str, object]:
            return {"choices": [{"message": {"content": self.content}}]}

    class Client:
        def __init__(self, *, base_url: str, timeout: int) -> None:
            assert base_url.endswith("/v1") and timeout == 180

        def __enter__(self) -> "Client":
            return self

        def __exit__(self, *_: object) -> None:
            pass

        def post(self, path: str, *, headers: dict[str, str], json: dict[str, object]) -> Response:
            assert path == "/chat/completions" and headers["Authorization"] == "Bearer key"
            requests.append(json)
            return Response(contents[len(requests) - 1])

    monkeypatch.setattr(cli.httpx, "Client", Client)  # type: ignore[attr-defined]
    target = HarnessEvaluationTarget(
        variant_id=uuid.uuid4(), model_id=uuid.uuid4(), capability_profile=CapabilityProfile()
    )
    scores, verdict, diagnostics = cli._run_suite(
        "http://core:8180", {"Authorization": "Bearer key"}, target
    )
    assert verdict is EvaluationVerdict.PASSED and diagnostics == []
    assert list(scores.model_dump().values()) == [1.0] * 4
    assert all(request["stop"] == ["<|im_end|>"] for request in requests)
    long_prompt = requests[-1]["messages"][0]["content"]  # type: ignore[index]
    assert "item0000" in long_prompt and "item1199" in long_prompt
