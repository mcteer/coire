"""Durable evaluation CLI transport; generation and scoring stay on Studios."""

from __future__ import annotations

import argparse
import sys
import time
import uuid
from pathlib import Path

from pydantic import BaseModel

from coire_core.models.evaluation import (
    TERMINAL_EVALUATION_STATES,
    EvaluationComparison,
    EvaluationControl,
    EvaluationGroupDetail,
    EvaluationMeasurement,
    EvaluationMeasurementDetail,
    EvaluationMeasurementRequest,
    EvaluationReceipt,
    EvaluationRunDetail,
    EvaluationRunPage,
    EvaluationState,
    EvaluationSubject,
    EvaluationSubmission,
    EvaluationSuite,
    EvaluationSuitePage,
    EvaluationSuiteRegistration,
    EvaluationTemplatePage,
)
from coire_core.models.harness import HarnessEvaluationTarget

ROOT = "/api/v1/admin"


def parsers(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    from coire_api.cli import _job_id, _mutation_options, _page_options, _positive_seconds, _version

    kinds = commands.add_parser("eval").add_subparsers(dest="evaluation", required=True)
    for kind in ("harness", "task", "judge"):
        command = kinds.add_parser(kind, help="submit a durable Studio evaluation")
        if kind == "harness":
            command.add_argument("variant_id", type=uuid.UUID)
            command.add_argument("--engine-version")
        else:
            command.add_argument("--suite", required=True)
            command.add_argument("--suite-version", type=_version, required=True)
            command.add_argument("--model", type=uuid.UUID, required=True)
            command.add_argument("--variant", type=uuid.UUID, required=True)
            targets = command.add_mutually_exclusive_group()
            targets.add_argument("--against-base", action="store_true")
            targets.add_argument("--against-model", type=uuid.UUID)
            command.add_argument("--against-variant", type=uuid.UUID)
            command.add_argument("--against-adapter", type=uuid.UUID)
        command.add_argument("--adapter", type=uuid.UUID)
        command.add_argument("--training-job", type=_job_id)
        command.add_argument("--no-wait", action="store_true")
        command.add_argument("--wait-timeout", type=_positive_seconds, default=1800.0)
        _mutation_options(command)
    suites = kinds.add_parser("suites").add_subparsers(dest="suite_command", required=True)
    suites.add_parser("templates")
    listing = suites.add_parser("list")
    _page_options(listing)
    listing.add_argument("--include-retired", action="store_true")
    register = suites.add_parser("register")
    register.add_argument("--file", type=Path, required=True)
    _mutation_options(register)
    show_suite = suites.add_parser("show")
    show_suite.add_argument("suite_id")
    show_suite.add_argument("suite_version", type=_version)
    retire = suites.add_parser("retire")
    retire.add_argument("suite_id")
    retire.add_argument("suite_version", type=_version)
    _mutation_options(retire, version=True)
    listing = kinds.add_parser("list")
    _page_options(listing)
    listing.add_argument("--job", type=_job_id)
    listing.add_argument("--adapter", type=uuid.UUID)
    listing.add_argument("--state", choices=[item.value for item in EvaluationState])
    listing.add_argument("--model", type=uuid.UUID)
    listing.add_argument("--variant", type=uuid.UUID)
    for verb in ("show", "wait", "cancel", "rerun", "group"):
        command = kinds.add_parser(verb)
        command.add_argument("id", type=_job_id)
        if verb == "wait":
            command.add_argument("--wait-timeout", type=_positive_seconds, default=1800.0)
        if verb in ("cancel", "rerun"):
            _mutation_options(command, version=True)
    compare = kinds.add_parser("compare")
    for side in ("left", "right"):
        compare.add_argument(f"--{side}", required=True)
        compare.add_argument(f"--{side}-subject", type=int, choices=(0, 1), default=0)
    measure = kinds.add_parser("measure")
    measure.add_argument("--file", type=Path, required=True)
    _mutation_options(measure)
    measurement = kinds.add_parser("measurement")
    measurement.add_argument("id", type=uuid.UUID)
    cancel = kinds.add_parser("measurement-cancel")
    cancel.add_argument("id", type=uuid.UUID)
    _mutation_options(cancel, version=True)


def file_model[Model: BaseModel](path: Path, model: type[Model]) -> Model:
    with path.open("rb") as source:
        data = source.read(65537)
    if not data or len(data) > 65536:
        raise ValueError("Evaluation document exceeds bound")
    return model.model_validate_json(data)


def wait(args: argparse.Namespace, headers: dict[str, str], run_id: str) -> int:
    from coire_api.cli import _api, _print_model

    deadline = time.monotonic() + args.wait_timeout
    try:
        while True:
            run = _api(args, headers, "GET", f"{ROOT}/evaluations/{run_id}", EvaluationRunDetail)
            if run.state in TERMINAL_EVALUATION_STATES:
                _print_model(run)
                return (
                    0
                    if run.state is EvaluationState.SUCCEEDED
                    and run.result is not None
                    and run.result.harness_verdict != "failed"
                    else 1
                )
            if time.monotonic() >= deadline:
                print(
                    f"Wait expired; evaluation {run_id} continues. Inspect its persisted status.",
                    file=sys.stderr,
                )
                return 2
            print(
                f"{run.id}: {run.state.value}; phase={run.phase}; cleanup={run.cleanup_state}",
                file=sys.stderr,
            )
            time.sleep(min(1.0, max(0.0, deadline - time.monotonic())))
    except KeyboardInterrupt:
        print(f"Stopped waiting; evaluation {run_id} continues.", file=sys.stderr)
        return 2


def command(args: argparse.Namespace, headers: dict[str, str]) -> int:
    from coire_api.cli import _api, _mutation_headers, _page_params, _print_model

    verb = args.evaluation
    value: BaseModel
    if verb in ("harness", "task", "judge"):
        if verb == "harness":
            target = _api(
                args,
                headers,
                "GET",
                f"{ROOT}/harness-evaluations/target/{args.variant_id}",
                HarnessEvaluationTarget,
                params={"adapter_id": str(args.adapter)} if args.adapter else None,
            )
            if (
                target.model_id != (target.target.model_id if target.target else target.model_id)
                or target.variant_id != args.variant_id
                or (
                    args.adapter
                    and (
                        target.target is None
                        or target.target.adapter_id != args.adapter
                        or target.target.variant_id != args.variant_id
                        or target.public_selector is None
                        or "@" not in str(target.public_selector)
                    )
                )
            ):
                raise ValueError("Evaluation target differs")
            subjects = [
                EvaluationSubject(
                    model_id=target.model_id, variant_id=args.variant_id, adapter_id=args.adapter
                )
            ]
            suite, version = "harness-capability", 1
        else:
            subject = EvaluationSubject(
                model_id=args.model, variant_id=args.variant, adapter_id=args.adapter
            )
            subjects = [subject]
            suite, version = args.suite, args.suite_version
            if args.against_base:
                if not args.adapter:
                    raise ValueError("Against base requires an adapter")
                subjects.insert(0, subject.model_copy(update={"adapter_id": None}))
            elif args.against_model:
                if not args.against_variant:
                    raise ValueError("Comparison variant required")
                subjects.insert(
                    0,
                    EvaluationSubject(
                        model_id=args.against_model,
                        variant_id=args.against_variant,
                        adapter_id=args.against_adapter,
                    ),
                )
            elif args.against_variant or args.against_adapter:
                raise ValueError("Comparison model required")
        assertion = (
            args.engine_version if verb == "harness" and args.engine_version != "unknown" else None
        )
        body = EvaluationSubmission(
            suite_id=suite,
            suite_version=version,
            subjects=subjects,
            training_job_id=args.training_job,
            expected_engine_version=assertion,
        )
        accepted = _api(
            args,
            _mutation_headers(args, headers),
            "POST",
            f"{ROOT}/evaluations",
            EvaluationReceipt,
            body,
        )
        _print_model(accepted)
        if args.no_wait:
            return 0
        return wait(args, headers, accepted.id)
    if verb == "suites":
        path = f"{ROOT}/evaluation-suites"
        if args.suite_command == "templates":
            value = _api(
                args, headers, "GET", f"{ROOT}/evaluation-suite-templates", EvaluationTemplatePage
            )
        elif args.suite_command == "list":
            value = _api(
                args,
                headers,
                "GET",
                path,
                EvaluationSuitePage,
                params={**_page_params(args), "include_retired": str(args.include_retired).lower()},
            )
        elif args.suite_command == "register":
            value = _api(
                args,
                _mutation_headers(args, headers),
                "POST",
                path,
                EvaluationSuite,
                file_model(args.file, EvaluationSuiteRegistration),
            )
        else:
            path += f"/{args.suite_id}/versions/{args.suite_version}"
            registered_suite = _api(args, headers, "GET", path, EvaluationSuite)
            value = (
                registered_suite
                if args.suite_command == "show"
                else _api(
                    args,
                    _mutation_headers(args, headers),
                    "POST",
                    path + "/retire",
                    EvaluationSuite,
                    EvaluationControl(
                        expected_version=args.expected_version or registered_suite.registry_version
                    ),
                )
            )
    elif verb == "list":
        params = _page_params(args)
        for flag, query in (
            ("job", "training_job_id"),
            ("adapter", "adapter_id"),
            ("state", "state"),
            ("model", "model_id"),
            ("variant", "variant_id"),
        ):
            item = getattr(args, flag)
            if item is not None:
                params[query] = str(item)
        value = _api(args, headers, "GET", f"{ROOT}/evaluations", EvaluationRunPage, params=params)
    elif verb == "wait":
        return wait(args, headers, args.id)
    elif verb == "show":
        value = _api(args, headers, "GET", f"{ROOT}/evaluations/{args.id}", EvaluationRunDetail)
    elif verb == "group":
        value = _api(
            args, headers, "GET", f"{ROOT}/evaluation-groups/{args.id}", EvaluationGroupDetail
        )
    elif verb in ("cancel", "rerun"):
        version = (
            args.expected_version
            or _api(
                args, headers, "GET", f"{ROOT}/evaluations/{args.id}", EvaluationRunDetail
            ).version
        )
        value = _api(
            args,
            _mutation_headers(args, headers),
            "POST",
            f"{ROOT}/evaluations/{args.id}/{verb}",
            EvaluationReceipt,
            EvaluationControl(expected_version=version),
        )
    elif verb == "compare":
        value = _api(
            args,
            headers,
            "GET",
            f"{ROOT}/evaluation-comparisons",
            EvaluationComparison,
            params={
                "left_result_id": args.left,
                "left_subject": args.left_subject,
                "right_result_id": args.right,
                "right_subject": args.right_subject,
            },
        )
    elif verb == "measure":
        value = _api(
            args,
            _mutation_headers(args, headers),
            "POST",
            f"{ROOT}/evaluation-measurements",
            EvaluationMeasurement,
            file_model(args.file, EvaluationMeasurementRequest),
        )
    elif verb == "measurement":
        value = _api(
            args,
            headers,
            "GET",
            f"{ROOT}/evaluation-measurements/{args.id}",
            EvaluationMeasurementDetail,
        )
    else:
        version = (
            args.expected_version
            or _api(
                args,
                headers,
                "GET",
                f"{ROOT}/evaluation-measurements/{args.id}",
                EvaluationMeasurementDetail,
            ).version
        )
        value = _api(
            args,
            _mutation_headers(args, headers),
            "POST",
            f"{ROOT}/evaluation-measurements/{args.id}/cancel",
            EvaluationMeasurement,
            EvaluationControl(expected_version=version),
        )
    _print_model(value)
    return 0
