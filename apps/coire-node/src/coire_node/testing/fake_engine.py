"""A stand-in for `mlx_lm.server`.

MLX runs only on Apple Silicon, so the Linux jobs cannot load a model — but everything around
the load (spawning, readiness, health, adoption, unload, budget) is ordinary process and HTTP
work that Linux runs faithfully. This double reproduces the three behaviours the agent depends
on, including the one that shapes the whole design:

  * `GET /health` answers **immediately**, before the "model" is loaded, exactly as the real
    server does (research R1). An agent that treated this as readiness would report a model
    ready before it could serve, so the double must keep the trap in place.
  * `POST /v1/chat/completions` returns 503 until the simulated load has finished, then a
    one-token completion. That transition is what "ready" actually means (spec FR-012).
  * `--fail-on-start` exits non-zero with a traceback on stderr, so the failure path can be
    tested without breaking a real model.

Usage mirrors the real server's flags closely enough that `EngineManager` builds one command
line for both, selected by `COIRE_ENGINE_COMMAND`.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

_state: dict[str, object] = {"ready_at": 0.0, "model": "", "ballast": None}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args: object) -> None:
        pass

    def _send(self, status: int, payload: object) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/health":
            # Deliberately unconditional: the real server answers here from its HTTP thread
            # while the model is still loading on another.
            self._send(200, {"status": "ok"})
        elif self.path.startswith("/v1/models"):
            self._send(
                200,
                {
                    "object": "list",
                    "data": [{"id": str(_state["model"]), "object": "model", "created": 0}],
                },
            )
        else:
            self._send(404, {"detail": "Not Found"})

    def do_POST(self) -> None:
        if not self.path.endswith("/chat/completions"):
            self._send(404, {"detail": "Not Found"})
            return
        length = int(self.headers.get("Content-Length", "0") or 0)
        request: dict[str, object] = {}
        if length:
            try:
                parsed = json.loads(self.rfile.read(length))
                request = parsed if isinstance(parsed, dict) else {}
            except json.JSONDecodeError:
                self._send(400, {"detail": "invalid JSON"})
                return
        ready_at = _state["ready_at"]
        if time.monotonic() < (ready_at if isinstance(ready_at, float) else 0.0):
            self._send(503, {"detail": "model is still loading"})
            return
        if "slow-completion" in str(request.get("messages", "")):
            time.sleep(5.0)
        raw_tools = request.get("tools", [])
        tools = raw_tools if isinstance(raw_tools, list) else []
        tool_names = {
            str(tool.get("function", {}).get("name", ""))
            for tool in tools
            if isinstance(tool, dict) and isinstance(tool.get("function"), dict)
        }
        if {"read_snapshot", "propose_reversible_action", "final_result"} <= tool_names:
            self._send(200, self._ops_completion(request))
            return
        content = "ok"
        if "coire-harness-json" in str(request.get("messages", "")):
            content = '{"answer":"bounded"}'
        completion = {
            "id": "fake-1",
            "object": "chat.completion",
            "model": str(_state["model"]),
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "length",
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }
        if request.get("stream") is True:
            events = [
                {
                    "id": "fake-1",
                    "object": "chat.completion.chunk",
                    "model": str(_state["model"]),
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"role": "assistant", "content": "ok"},
                            "finish_reason": None,
                        }
                    ],
                },
                {
                    "id": "fake-1",
                    "object": "chat.completion.chunk",
                    "model": str(_state["model"]),
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                },
            ]
            slow = "slow-stream" in str(request.get("messages", ""))
            fail = "fail-stream" in str(request.get("messages", ""))
            if not slow and not fail:
                body = (
                    "".join(f"data: {json.dumps(event)}\n\n" for event in events)
                    + "data: [DONE]\n\n"
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()

            def write_chunk(payload: bytes) -> None:
                self.wfile.write(f"{len(payload):x}\r\n".encode() + payload + b"\r\n")
                self.wfile.flush()

            for index, event in enumerate(events):
                write_chunk(f"data: {json.dumps(event)}\n\n".encode())
                if fail and index == 0:
                    self.close_connection = True
                    return
                if slow:
                    time.sleep(1.0)
            write_chunk(b"data: [DONE]\n\n")
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
            return
        self._send(
            200,
            completion,
        )

    def _ops_completion(self, request: dict[str, object]) -> dict[str, object]:
        """Deterministically exercise the ops agent's real read/propose tool loop in CI."""

        messages = request.get("messages", [])
        message_list = messages if isinstance(messages, list) else []
        tool_results = [
            message
            for message in message_list
            if isinstance(message, dict) and message.get("role") == "tool"
        ]
        if not tool_results:
            name = "read_snapshot"
            arguments: dict[str, object] = {}
        elif "Proposal staged" in str(tool_results[-1].get("content", "")):
            name = "final_result"
            arguments = {"answer": "Prepared one exact reversible proposal for review."}
        else:
            snapshot: dict[str, object] = {}
            try:
                parsed = json.loads(str(tool_results[-1].get("content", "{}")))
                snapshot = parsed if isinstance(parsed, dict) else {}
            except json.JSONDecodeError:
                pass
            cluster = snapshot.get("cluster", {})
            instances = cluster.get("instances", []) if isinstance(cluster, dict) else []
            ready = next(
                (
                    item
                    for item in instances
                    if isinstance(item, dict) and item.get("state") == "ready"
                ),
                None,
            )
            if ready is None:
                name = "final_result"
                arguments = {"answer": "No ready instance is available to unload."}
            else:
                name = "propose_reversible_action"
                arguments = {
                    "action": {
                        "operation": "instance.unload",
                        "target_type": "instance",
                        "target_id": ready["id"],
                        "parameters": {},
                        "precondition": {
                            "resource_version": ready["updated_at"],
                            "expected_state": "ready",
                        },
                    },
                    "rationale": "The selected integration instance is ready and idle.",
                }
        return {
            "id": f"fake-ops-{len(tool_results) + 1}",
            "object": "chat.completion",
            "model": str(_state["model"]),
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": f"fake-ops-call-{len(tool_results) + 1}",
                                "type": "function",
                                "function": {
                                    "name": name,
                                    "arguments": json.dumps(arguments),
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="fake mlx_lm.server")
    parser.add_argument("--model", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--chat-template", default="")
    parser.add_argument("--log-level", default="INFO")
    parser.add_argument("--load-delay", type=float, default=0.0)
    parser.add_argument("--fail-on-start", action="store_true")
    parser.add_argument("--allocate-mb", type=int, default=0)
    # Accepted and ignored, so one command line serves both engines.
    parser.add_argument("--adapter-path", default=None)
    parser.add_argument("--trust-remote-code", action="store_true")
    args, _unknown = parser.parse_known_args(argv)

    # The real server refuses a model path that is not there; so does this, because
    # "engine started against a deleted copy" is a case the agent has to handle.
    if not Path(args.model).exists():
        print(f"model path does not exist: {args.model}", file=sys.stderr)
        return 2

    if args.fail_on_start:
        time.sleep(1.0)
        print("Traceback (most recent call last):", file=sys.stderr)
        print("  ValueError: simulated engine start failure", file=sys.stderr)
        sys.stderr.flush()
        return 3

    _state["model"] = args.model
    _state["ready_at"] = time.monotonic() + args.load_delay
    if args.allocate_mb:
        # Touched, so the pages are dirty and show up in RSS and phys_footprint alike.
        ballast = bytearray(args.allocate_mb * 1024 * 1024)
        for offset in range(0, len(ballast), 4096):
            ballast[offset] = 1
        _state["ballast"] = ballast

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.daemon_threads = True
    print(f"Starting httpd at {args.host} on port {args.port}...", file=sys.stderr)
    sys.stderr.flush()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
