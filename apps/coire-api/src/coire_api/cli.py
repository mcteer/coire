"""Small administrative CLI; all mutations go through the authenticated API."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path

import httpx
from pydantic import BaseModel, TypeAdapter, ValidationError

from coire_core.models.adapters import (
    AdapterCurationRequest,
    AdapterDetail,
    AdapterPage,
    AdapterReceipt,
    AdapterRetireRequest,
)
from coire_core.models.datasets import (
    DatasetAnalysis,
    DatasetAnalysisReceipt,
    DatasetAnalyzeRequest,
    DatasetDeleteRequest,
    DatasetDeletionReceipt,
    DatasetDetail,
    DatasetFormat,
    DatasetPage,
    DatasetProvenance,
    DatasetReceipt,
    DatasetUploadRequest,
)
from coire_core.models.feedback import (
    AdapterLineage,
    PreferenceExportCancel,
    PreferenceExportCreate,
    PreferenceExportDetail,
    PreferenceExportPage,
    PreferenceExportReceipt,
)
from coire_core.models.registry import Visibility
from coire_core.models.training import (
    CheckpointPage,
    CheckpointPromotionRequest,
    TrainingCommandReceipt,
    TrainingControlRequest,
    TrainingDeleteRequest,
    TrainingDeletionReceipt,
    TrainingEvent,
    TrainingJobDetail,
    TrainingJobPage,
    TrainingJobReceipt,
    TrainingMeasurementReceipt,
    TrainingMeasurementRequest,
    TrainingMeasurementResult,
    TrainingProfilePage,
    TrainingRecipePage,
    TrainingSubmission,
    TrainingValidation,
)
from coire_core.models.training_types import TrainingId


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="coire")
    parser.add_argument("--api-url", default=os.getenv("COIRE_API_URL", "http://localhost:8180"))
    parser.add_argument("--token", default=os.getenv("COIRE_API_TOKEN", ""))
    commands = parser.add_subparsers(dest="command", required=True)
    from coire_api.evaluation.cli import parsers as evaluation_parsers

    evaluation_parsers(commands)
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
    _training_parsers(commands)
    _feedback_parsers(commands)
    return parser


def _positive_seconds(value: str) -> float:
    import math

    seconds = float(value)
    if not math.isfinite(seconds) or seconds <= 0:
        raise argparse.ArgumentTypeError("must be a finite positive number of seconds")
    return seconds


def _feedback_parsers(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    feedback = commands.add_parser("feedback", help="manage private preference exports")
    verbs = feedback.add_subparsers(dest="verb", required=True)
    submit = verbs.add_parser("export", help="submit a shared-contract JSON request file")
    submit.add_argument("file", type=Path)
    _mutation_options(submit)
    history = verbs.add_parser("exports")
    _page_options(history)
    show = verbs.add_parser("export-show")
    show.add_argument("id", type=_job_id)
    cancel = verbs.add_parser("export-cancel")
    cancel.add_argument("id", type=_job_id)
    _mutation_options(cancel, version=True)


def _version(value: str) -> int:
    version = int(value)
    if version < 1:
        raise argparse.ArgumentTypeError("must be a positive version")
    return version


def _job_id(value: str) -> str:
    try:
        return TypeAdapter(TrainingId).validate_python(value)
    except ValidationError:
        raise argparse.ArgumentTypeError("must be a training job ULID") from None


def _key(value: str) -> str:
    if not 1 <= len(value) <= 128 or any(ord(char) < 33 or ord(char) > 126 for char in value):
        raise argparse.ArgumentTypeError("must be 1..128 printable non-space ASCII characters")
    return value


def _mutation_options(parser: argparse.ArgumentParser, *, version: bool = False) -> None:
    parser.add_argument("--idempotency-key", type=_key)
    if version:
        parser.add_argument(
            "--expected-version",
            type=_version,
            help="pin the original version when replaying a command; otherwise read current version",
        )


def _page_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--cursor")
    parser.add_argument("--limit", type=int, choices=range(1, 101), default=25)


def _wait_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--wait", action="store_true")
    parser.add_argument("--wait-timeout", type=_positive_seconds, default=1800.0)
    parser.add_argument("--poll-interval", type=_positive_seconds, default=2.0)


def _training_parsers(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    data = commands.add_parser("data", help="manage private uploaded JSONL datasets")
    verbs = data.add_subparsers(dest="verb", required=True)
    upload = verbs.add_parser("upload")
    upload.add_argument("file", type=Path)
    upload.add_argument("--name", required=True)
    upload.add_argument("--format", choices=[item.value for item in DatasetFormat], required=True)
    upload.add_argument("--source", required=True, help="declared provenance; never fetched")
    upload.add_argument("--license-note", required=True)
    upload.add_argument("--model", type=uuid.UUID, required=True)
    upload.add_argument("--variant", type=uuid.UUID, required=True)
    upload.add_argument("--seed", type=int, default=0)
    upload.add_argument("--validation-fraction", type=float, default=0.05)
    _mutation_options(upload)
    _wait_options(upload)
    _page_options(verbs.add_parser("list"))
    verbs.add_parser("show").add_argument("id", type=uuid.UUID)
    analyze = verbs.add_parser("analyze")
    analyze.add_argument("id", type=uuid.UUID)
    analyze.add_argument("--model", type=uuid.UUID, required=True)
    analyze.add_argument("--variant", type=uuid.UUID, required=True)
    _mutation_options(analyze)
    _wait_options(analyze)
    delete = verbs.add_parser("delete")
    delete.add_argument("id", type=uuid.UUID)
    _mutation_options(delete, version=True)

    train = commands.add_parser("train", help="submit and observe node-owned training")
    verbs = train.add_subparsers(dest="verb", required=True)
    verbs.add_parser("recipes")
    for verb in ("validate", "submit", "measure"):
        command = verbs.add_parser(verb)
        command.add_argument("file", type=Path)
        _mutation_options(command)
    _page_options(verbs.add_parser("list"))
    for verb in ("show", "checkpoints", "pause", "resume", "cancel", "delete", "events"):
        command = verbs.add_parser(verb)
        command.add_argument("id", type=_job_id)
        if verb in ("pause", "resume", "cancel", "delete"):
            _mutation_options(command, version=True)
        if verb == "checkpoints":
            _page_options(command)
        if verb == "events":
            command.add_argument("--last-event-id", type=int, default=0)
            command.add_argument("--timeout", type=_positive_seconds, default=300.0)
    verbs.add_parser("measurement").add_argument("id", type=uuid.UUID)
    _page_options(verbs.add_parser("profiles"))

    adapter = commands.add_parser("adapter", help="curate exact base/adapter targets")
    verbs = adapter.add_subparsers(dest="verb", required=True)
    verbs.add_parser("lineage").add_argument("id", type=uuid.UUID)
    _page_options(verbs.add_parser("list"))
    verbs.add_parser("show").add_argument("id", type=uuid.UUID)
    promote = verbs.add_parser("promote")
    promote.add_argument("id", type=uuid.UUID, help="complete checkpoint UUID")
    promote.add_argument("--name", required=True, help="new adapter slug")
    binding = promote.add_mutually_exclusive_group(required=True)
    binding.add_argument("--job", type=_job_id, help="parent job whose current version is read")
    binding.add_argument("--expected-version", type=_version, help="original parent job version")
    _mutation_options(promote)
    for verb in ("publish", "unpublish", "retire"):
        command = verbs.add_parser(verb)
        command.add_argument("id", type=uuid.UUID)
        _mutation_options(command, version=True)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not args.token:
        _parser().error("--token or COIRE_API_TOKEN is required")
    headers = {"Authorization": f"Bearer {args.token}"}
    try:
        if args.command == "run":
            return _run_command(args, headers)
        if args.command == "feedback":
            return _feedback_command(args, headers)
        if args.command in ("data", "train", "adapter"):
            return _training_command(args, headers)
        return _evaluation_command(args, headers)
    except httpx.HTTPStatusError:
        return 2 if args.command == "eval" else 1
    except httpx.HTTPError as error:
        print(
            f"API transport failed ({type(error).__name__}); no automatic retry.", file=sys.stderr
        )
        return 2 if args.command == "eval" else 1
    except (ValidationError, ValueError):
        print("Invalid request or API response; check shared contract fields.", file=sys.stderr)
        return 2 if args.command == "eval" else 1
    except OSError:
        print("Cannot read input file; check access and file size.", file=sys.stderr)
        return 2 if args.command == "eval" else 1


def _feedback_command(args: argparse.Namespace, headers: dict[str, str]) -> int:
    root = "/api/v1/admin/feedback/exports"
    if args.verb == "export":
        body = PreferenceExportCreate.model_validate_json(_read_yaml(args.file))
        _print_model(
            _api(
                args, _mutation_headers(args, headers), "POST", root, PreferenceExportReceipt, body
            )
        )
    elif args.verb == "exports":
        _print_model(
            _api(args, headers, "GET", root, PreferenceExportPage, params=_page_params(args))
        )
    elif args.verb == "export-show":
        _print_model(_api(args, headers, "GET", root + "/" + args.id, PreferenceExportDetail))
    else:
        version = args.expected_version
        if version is None:
            version = _api(
                args, headers, "GET", root + "/" + args.id, PreferenceExportDetail
            ).version
        _print_model(
            _api(
                args,
                _mutation_headers(args, headers),
                "POST",
                root + "/" + args.id + "/cancel",
                PreferenceExportReceipt,
                PreferenceExportCancel(expected_version=version),
            )
        )
    return 0


def _evaluation_command(args: argparse.Namespace, headers: dict[str, str]) -> int:
    from coire_api.evaluation.cli import command

    return command(args, headers)


def _api[ResponseModel: BaseModel](
    args: argparse.Namespace,
    headers: dict[str, str],
    method: str,
    path: str,
    model: type[ResponseModel],
    body: BaseModel | None = None,
    *,
    params: dict[str, str | int] | None = None,
    timeout: float = 30,
) -> ResponseModel:
    response = httpx.request(
        method,
        args.api_url.rstrip("/") + path,
        headers=headers,
        json=body.model_dump(mode="json") if body is not None else None,
        params=params,
        timeout=timeout,
    )
    _check_response(response)
    return model.model_validate(response.json())


def _check_response(response: httpx.Response) -> None:
    if response.is_error:
        guidance = {
            401: "authentication required",
            403: "administrator scope required",
            404: "resource unavailable or not visible",
            409: "version, state or intent conflict; inspect resource before a new action",
            413: "input exceeds the server bound",
            422: "invalid input; check recipe fields, registry bindings and row diagnostics",
            429: "quota or queue limit; inspect capacity before retrying",
            503: "feature or dependency unavailable",
        }.get(response.status_code, "request refused")
        # Never echo untrusted server bodies (which may contain source content or credentials).
        print(f"HTTP {response.status_code}: {guidance}", file=sys.stderr)
        response.raise_for_status()


def _print_model(value: BaseModel) -> None:
    print(json.dumps(value.model_dump(mode="json"), indent=2, sort_keys=True))


def _mutation_headers(args: argparse.Namespace, headers: dict[str, str]) -> dict[str, str]:
    key = args.idempotency_key or str(uuid.uuid4())
    print(f"Idempotency-Key: {key}", file=sys.stderr)
    return {**headers, "Idempotency-Key": key}


def _page_params(args: argparse.Namespace) -> dict[str, str | int]:
    result: dict[str, str | int] = {"limit": args.limit}
    if args.cursor is not None:
        result["cursor"] = args.cursor
    return result


def _read_yaml(path: Path) -> str:
    with path.open("rb") as source:
        data = source.read(65537)
    if not data or len(data) > 65536:
        raise ValueError("YAML must contain 1..65536 bytes")
    return data.decode("utf-8", errors="strict")


def _measurement(path: Path) -> TrainingMeasurementRequest:
    import yaml

    from coire_api.training.specs import RecipeLoader, RecipeShapeError, _check_structure
    from coire_core.settings import Settings

    source = _read_yaml(path)
    try:
        _check_structure(source, Settings())
        return TrainingMeasurementRequest.model_validate(yaml.load(source, Loader=RecipeLoader))
    except (yaml.YAMLError, RecipeShapeError):
        raise ValueError("invalid measurement YAML") from None


def _current_version(
    args: argparse.Namespace,
    headers: dict[str, str],
    path: str,
    model: type[DatasetDetail] | type[TrainingJobDetail] | type[AdapterDetail],
) -> int:
    if args.expected_version is not None:
        return int(args.expected_version)
    detail = _api(args, headers, "GET", path, model)
    print(f"Expected version: {detail.version}", file=sys.stderr)
    return detail.version


def _wait_dataset(
    args: argparse.Namespace,
    headers: dict[str, str],
    receipt: DatasetReceipt | DatasetAnalysisReceipt,
) -> int:
    try:
        return _poll_dataset(args, headers, receipt)
    except httpx.TimeoutException:
        print(
            "Polling timed out; work continues. Inspect the returned resource ID.", file=sys.stderr
        )
        return 2


def _poll_dataset(
    args: argparse.Namespace,
    headers: dict[str, str],
    receipt: DatasetReceipt | DatasetAnalysisReceipt,
) -> int:
    deadline = time.monotonic() + args.wait_timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            print(
                "Wait timed out; work continues. Inspect the returned resource ID.", file=sys.stderr
            )
            return 2
        if isinstance(receipt, DatasetReceipt):
            detail = _api(
                args,
                headers,
                "GET",
                f"/api/v1/admin/datasets/{receipt.dataset_id}",
                DatasetDetail,
                timeout=min(30, remaining),
            )
            _print_model(detail)
            if detail.state == "ready":
                if detail.analysis_id is None:
                    raise ValueError("ready dataset has no analysis")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    print("Wait timed out; inspect the returned analysis ID.", file=sys.stderr)
                    return 2
                selected = _api(
                    args,
                    headers,
                    "GET",
                    f"/api/v1/admin/dataset-analyses/{detail.analysis_id}",
                    DatasetAnalysis,
                    timeout=min(30, remaining),
                )
                if (
                    selected.id != detail.analysis_id
                    or selected.dataset_id != receipt.dataset_id
                    or selected.model_id != args.model
                    or selected.variant_id != args.variant
                    or selected.state != "succeeded"
                ):
                    raise ValueError("upload analysis identity changed")
                _print_model(selected)
                return 0
            if detail.state in ("failed", "analysis_failed", "retired", "purged"):
                return 1
        else:
            analysis = _api(
                args,
                headers,
                "GET",
                f"/api/v1/admin/dataset-analyses/{receipt.analysis_id}",
                DatasetAnalysis,
                timeout=min(30, remaining),
            )
            if (
                analysis.id != receipt.analysis_id
                or analysis.model_id != args.model
                or analysis.variant_id != args.variant
                or analysis.dataset_id != args.id
            ):
                raise ValueError("analysis identity changed")
            _print_model(analysis)
            if analysis.state == "succeeded":
                return 0
            if analysis.state in ("failed", "cancelled"):
                return 1
        time.sleep(min(args.poll_interval, max(0, deadline - time.monotonic())))


def _training_events(args: argparse.Namespace, headers: dict[str, str]) -> int:
    if args.last_event_id < 0:
        raise ValueError("negative event cursor")
    deadline = time.monotonic() + args.timeout
    event_headers = {
        **headers,
        "Accept": "text/event-stream",
        "Last-Event-ID": str(args.last_event_id),
    }
    cursor = args.last_event_id
    data: list[str] = []
    with httpx.stream(
        "GET",
        f"{args.api_url.rstrip('/')}/api/v1/admin/training/jobs/{args.id}/events",
        headers=event_headers,
        timeout=min(30, args.timeout),
    ) as stream:
        _check_response(stream)
        for line in stream.iter_lines():
            if time.monotonic() >= deadline:
                print("Event observation timed out; job continues.", file=sys.stderr)
                return 2
            if line.startswith("data:"):
                data.append(line[5:].lstrip(" "))
                if sum(map(len, data)) > 65536:
                    raise ValueError("event exceeds bound")
            elif not line and data:
                event = TrainingEvent.model_validate_json("\n".join(data))
                data.clear()
                if event.job_id != args.id:
                    raise ValueError("event job differs")
                if event.id <= cursor:
                    continue
                cursor = event.id
                _print_model(event)
                if event.kind == "terminal":
                    return 0 if getattr(event.payload, "state", None) == "succeeded" else 1
                if event.kind == "reset":
                    snapshot = getattr(event.payload, "snapshot", None)
                    if snapshot is not None and snapshot.state in (
                        "succeeded",
                        "failed",
                        "cancelled",
                    ):
                        return 0 if snapshot.state == "succeeded" else 1
    print(
        f"Event stream closed; reconnect with --last-event-id {cursor}. Job continues.",
        file=sys.stderr,
    )
    return 2


def _training_command(args: argparse.Namespace, headers: dict[str, str]) -> int:
    if args.command == "data":
        root = "/api/v1/admin/datasets"
        if args.verb == "upload":
            metadata = DatasetUploadRequest(
                name=args.name,
                format=DatasetFormat(args.format),
                provenance=DatasetProvenance(source=args.source, license_note=args.license_note),
                analysis_model_id=args.model,
                analysis_variant_id=args.variant,
                split_seed=args.seed,
                validation_fraction=args.validation_fraction,
            )
            with args.file.open("rb") as file:
                if not 0 < os.fstat(file.fileno()).st_size <= 256 * 1024**2:
                    raise ValueError("dataset exceeds bound")
                response = httpx.post(
                    args.api_url.rstrip("/") + root,
                    headers=_mutation_headers(args, headers),
                    data={"metadata": metadata.model_dump_json()},
                    files={"file": ("dataset.jsonl", file, "application/x-ndjson")},
                    timeout=30,
                )
            _check_response(response)
            receipt = DatasetReceipt.model_validate(response.json())
            _print_model(receipt)
            if args.wait:
                return _wait_dataset(args, headers, receipt)
            return 1 if receipt.state in ("failed", "analysis_failed") else 0
        if args.verb == "list":
            result: BaseModel = _api(
                args, headers, "GET", root, DatasetPage, params=_page_params(args)
            )
        elif args.verb == "show":
            result = _api(args, headers, "GET", f"{root}/{args.id}", DatasetDetail)
        elif args.verb == "analyze":
            analysis_receipt = _api(
                args,
                _mutation_headers(args, headers),
                "POST",
                f"{root}/{args.id}/analyze",
                DatasetAnalysisReceipt,
                DatasetAnalyzeRequest(model_id=args.model, variant_id=args.variant),
            )
            _print_model(analysis_receipt)
            if args.wait:
                return _wait_dataset(args, headers, analysis_receipt)
            return 1 if analysis_receipt.state in ("failed", "cancelled") else 0
        else:
            version = _current_version(args, headers, f"{root}/{args.id}", DatasetDetail)
            result = _api(
                args,
                _mutation_headers(args, headers),
                "DELETE",
                f"{root}/{args.id}",
                DatasetDeletionReceipt,
                DatasetDeleteRequest(expected_version=version),
            )
    elif args.command == "train":
        root = "/api/v1/admin/training"
        if args.verb == "events":
            try:
                return _training_events(args, headers)
            except httpx.TimeoutException:
                print(
                    "Event observation timed out; job continues. Reconnect using the last printed event ID.",
                    file=sys.stderr,
                )
                return 2
        if args.verb in ("validate", "submit"):
            submission = TrainingSubmission(source_yaml=_read_yaml(args.file))
            result = _api(
                args,
                _mutation_headers(args, headers),
                "POST",
                f"{root}/validate" if args.verb == "validate" else f"{root}/jobs",
                TrainingValidation if args.verb == "validate" else TrainingJobReceipt,
                submission,
            )
        elif args.verb == "measure":
            result = _api(
                args,
                _mutation_headers(args, headers),
                "POST",
                f"{root}/measurements",
                TrainingMeasurementReceipt,
                _measurement(args.file),
            )
        elif args.verb in ("pause", "resume", "cancel", "delete"):
            path = f"{root}/jobs/{args.id}"
            version = _current_version(args, headers, path, TrainingJobDetail)
            result = _api(
                args,
                _mutation_headers(args, headers),
                "DELETE" if args.verb == "delete" else "POST",
                path if args.verb == "delete" else f"{path}/{args.verb}",
                TrainingDeletionReceipt if args.verb == "delete" else TrainingCommandReceipt,
                TrainingDeleteRequest(expected_version=version)
                if args.verb == "delete"
                else TrainingControlRequest(expected_version=version),
            )
        else:
            routes: dict[str, tuple[str, type[BaseModel]]] = {
                "recipes": (f"{root}/recipes", TrainingRecipePage),
                "list": (f"{root}/jobs", TrainingJobPage),
                "profiles": (f"{root}/profiles", TrainingProfilePage),
            }
            if args.verb in routes:
                path, model = routes[args.verb]
                result = _api(
                    args,
                    headers,
                    "GET",
                    path,
                    model,
                    params=None if args.verb == "recipes" else _page_params(args),
                )
            elif args.verb == "measurement":
                result = _api(
                    args,
                    headers,
                    "GET",
                    f"{root}/measurements/{args.id}",
                    TrainingMeasurementResult,
                )
            elif args.verb == "checkpoints":
                result = _api(
                    args,
                    headers,
                    "GET",
                    f"{root}/jobs/{args.id}/checkpoints",
                    CheckpointPage,
                    params=_page_params(args),
                )
            else:
                result = _api(args, headers, "GET", f"{root}/jobs/{args.id}", TrainingJobDetail)
    else:
        root = "/api/v1/admin/adapters"
        if args.verb == "list":
            result = _api(args, headers, "GET", root, AdapterPage, params=_page_params(args))
        elif args.verb == "show":
            result = _api(args, headers, "GET", f"{root}/{args.id}", AdapterDetail)
        elif args.verb == "lineage":
            result = _api(args, headers, "GET", f"{root}/{args.id}/lineage", AdapterLineage)
        elif args.verb == "promote":
            version = _current_version(
                args, headers, f"/api/v1/admin/training/jobs/{args.job}", TrainingJobDetail
            )
            result = _api(
                args,
                _mutation_headers(args, headers),
                "POST",
                f"/api/v1/admin/training/checkpoints/{args.id}/promote",
                AdapterReceipt,
                CheckpointPromotionRequest(expected_version=version, adapter_slug=args.name),
            )
        else:
            version = _current_version(args, headers, f"{root}/{args.id}", AdapterDetail)
            body: BaseModel = (
                AdapterRetireRequest(expected_version=version)
                if args.verb == "retire"
                else AdapterCurationRequest(
                    expected_version=version,
                    visibility=Visibility.PUBLISHED
                    if args.verb == "publish"
                    else Visibility.ADMIN_ONLY,
                )
            )
            result = _api(
                args,
                _mutation_headers(args, headers),
                "DELETE" if args.verb == "retire" else "PATCH",
                f"{root}/{args.id}",
                AdapterReceipt,
                body,
            )
    _print_model(result)
    if isinstance(result, TrainingValidation) and not result.ready_to_run:
        print("Validation pending: " + ", ".join(result.reasons), file=sys.stderr)
        if any(
            reason in ("invalid_input", "impossible_fit", "unauthorized")
            for reason in result.reasons
        ):
            return 1
        return 2
    if isinstance(
        result,
        (
            TrainingJobDetail,
            TrainingJobReceipt,
            TrainingMeasurementResult,
            TrainingMeasurementReceipt,
            AdapterReceipt,
        ),
    ) and result.state in (
        "failed",
        "cancelled",
        "inconclusive",
    ):
        return 1
    if isinstance(result, (DatasetDetail, AdapterDetail)) and result.state in (
        "failed",
        "analysis_failed",
    ):
        return 1
    if isinstance(result, TrainingDeletionReceipt):
        print(
            "Job retirement retains referenced adapter/checkpoint lineage; byte cleanup may remain pending.",
            file=sys.stderr,
        )
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
