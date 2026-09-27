"""Small administrative CLI; all mutations go through the authenticated API."""

from __future__ import annotations

import argparse
import json
import os
import sys

import httpx

from coire_core.models.harness import (
    CategoryScores,
    EvaluationVerdict,
    HarnessEvaluationSubmission,
    HarnessEvaluationTarget,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="coire")
    parser.add_argument("--api-url", default=os.getenv("COIRE_API_URL", "http://localhost:8180"))
    parser.add_argument("--token", default=os.getenv("COIRE_API_TOKEN", ""))
    commands = parser.add_subparsers(dest="command", required=True)
    evaluate = commands.add_parser("eval")
    kinds = evaluate.add_subparsers(dest="evaluation", required=True)
    harness = kinds.add_parser("harness", help="run and persist the deterministic harness suite")
    harness.add_argument("variant_id")
    harness.add_argument("--engine-version", default="unknown")
    run = commands.add_parser("run")
    run_commands = run.add_subparsers(dest="run_command", required=True)
    submit = run_commands.add_parser("submit")
    submit.add_argument("--profile", choices=("coding", "general", "image"), required=True)
    submit.add_argument("--model", required=True)
    submit.add_argument("--workspace", required=True)
    submit.add_argument("--permit-model", action="append", default=[])
    submit.add_argument("--permit-tool", action="append", default=[])
    submit.add_argument("--spend-limit-tokens", type=int, default=100_000)
    run_commands.add_parser("list")
    show = run_commands.add_parser("show")
    show.add_argument("run_id")
    kill = run_commands.add_parser("kill")
    kill.add_argument("run_id")
    kill.add_argument("--reason", default="killed by administrator")
    events = run_commands.add_parser("events")
    events.add_argument("run_id")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not args.token:
        _parser().error("--token or COIRE_API_TOKEN is required")
    headers = {"Authorization": f"Bearer {args.token}"}
    if args.command == "run":
        return _run_command(args, headers)
    return _evaluation_command(args, headers)


def _evaluation_command(args: argparse.Namespace, headers: dict[str, str]) -> int:
    target_response = httpx.get(
        f"{args.api_url.rstrip('/')}/api/v1/admin/harness-evaluations/target/{args.variant_id}",
        headers=headers,
        timeout=30,
    )
    if target_response.is_error:
        print(target_response.text, file=sys.stderr)
        return 1
    target = HarnessEvaluationTarget.model_validate(target_response.json())
    scores, verdict, diagnostics = _run_suite(args.api_url, headers, target)
    submission = HarnessEvaluationSubmission(
        variant_id=target.variant_id,
        scores=scores,
        verdict=verdict,
        harness_version="0.1.0",
        engine_version=args.engine_version,
        diagnostics=diagnostics,
    )
    response = httpx.post(
        f"{args.api_url.rstrip('/')}/api/v1/admin/harness-evaluations",
        headers=headers,
        json=submission.model_dump(mode="json"),
        timeout=30,
    )
    if response.is_error:
        print(response.text, file=sys.stderr)
        return 1
    print(json.dumps(response.json(), indent=2, sort_keys=True))
    return 0


def _run_command(args: argparse.Namespace, headers: dict[str, str]) -> int:
    base = args.api_url.rstrip("/")
    if args.run_command == "submit":
        permitted = set(args.permit_model) | {args.model}
        response = httpx.post(
            f"{base}/api/v1/runs",
            headers=headers,
            json={
                "profile": args.profile,
                "primary_model_id": args.model,
                "workspace_ref": args.workspace,
                "permitted_model_ids": sorted(permitted),
                "permitted_tools": sorted(set(args.permit_tool)),
                "spend_limit_tokens": args.spend_limit_tokens,
                "limits": {},
            },
            timeout=30,
        )
    elif args.run_command == "list":
        response = httpx.get(f"{base}/api/v1/runs", headers=headers, timeout=30)
    elif args.run_command == "show":
        response = httpx.get(f"{base}/api/v1/runs/{args.run_id}", headers=headers, timeout=30)
    elif args.run_command == "kill":
        response = httpx.request(
            "DELETE",
            f"{base}/api/v1/admin/runs/{args.run_id}",
            headers=headers,
            json={"reason": args.reason},
            timeout=30,
        )
    else:
        with httpx.stream(
            "GET",
            f"{base}/api/v1/runs/{args.run_id}/events",
            headers=headers,
            timeout=None,
        ) as stream:
            if stream.is_error:
                print(stream.read().decode(errors="replace"), file=sys.stderr)
                return 1
            for line in stream.iter_lines():
                if line.startswith("data: "):
                    print(line[6:])
        return 0
    if response.is_error:
        print(response.text, file=sys.stderr)
        return 1
    print(json.dumps(response.json(), indent=2, sort_keys=True))
    return 0


def _run_suite(
    api_url: str, headers: dict[str, str], target: HarnessEvaluationTarget
) -> tuple[CategoryScores, EvaluationVerdict, list[str]]:
    sentinel = "coire-context-sentinel-7419"
    cases = {
        "tool_calling": 'Return only {"name":"read_file","arguments":{"path":"README.md"}}',
        "structured_output": 'Return only {"answer":"ok"}',
        "edit_application": "Return a unified diff that adds the line coire-eval to note.txt",
        "long_context": f"Remember the final token. {'bounded ' * 1200} Final token: {sentinel}",
    }
    passed: dict[str, float] = {}
    diagnostics: list[str] = []
    try:
        with httpx.Client(base_url=f"{api_url.rstrip('/')}/v1", timeout=180) as client:
            for category, prompt in cases.items():
                response = client.post(
                    "/chat/completions",
                    headers=headers,
                    json={
                        "model": str(target.model_id),
                        "messages": [{"role": "user", "content": prompt}],
                        "temperature": 0,
                        "max_tokens": 256,
                    },
                )
                response.raise_for_status()
                content = str(response.json()["choices"][0]["message"]["content"])
                if category == "tool_calling":
                    ok = '"name"' in content and '"arguments"' in content
                elif category == "structured_output":
                    try:
                        ok = isinstance(json.loads(content), dict)
                    except json.JSONDecodeError:
                        ok = False
                elif category == "edit_application":
                    ok = "---" in content and "+++" in content and "+coire-eval" in content
                else:
                    ok = sentinel in content
                passed[category] = float(ok)
                if not ok:
                    diagnostics.append(f"{category}: deterministic assertion failed")
    except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
        diagnostics.append(f"infrastructure: {type(exc).__name__}: {str(exc)[:200]}")
        return (
            CategoryScores(tool_calling=0, structured_output=0, edit_application=0, long_context=0),
            EvaluationVerdict.INFRASTRUCTURE_ERROR,
            diagnostics,
        )
    scores = CategoryScores.model_validate(passed)
    verdict = (
        EvaluationVerdict.PASSED
        if min(scores.model_dump().values()) >= 0.8
        else EvaluationVerdict.FAILED
    )
    return scores, verdict, diagnostics
