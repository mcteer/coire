"""Project-authored immutable case data; execution/scoring lives on the Studios."""

from __future__ import annotations

import hashlib
import json

from coire_core.models.evaluation import (
    EvaluationAssertion,
    EvaluationCase,
    EvaluationSuiteTemplate,
    SuiteKind,
    SuiteMode,
    TemplateId,
)

SCORER_VERSION = "coire-evaluation-v1"
LICENSE_NOTE = "Project-authored fixtures; no third-party benchmark data"


def _harness() -> list[EvaluationCase]:
    sentinel = "coire-context-sentinel-7419"
    filler = " ".join(f"item{i:04d}" for i in range(1200))
    return [
        EvaluationCase(
            id="tool-call",
            category="tool_calling",
            prompt='Return only {"name":"read_file","arguments":{"path":"README.md"}}',
            assertions=[EvaluationAssertion(kind="tool_call", value="read_file")],
        ),
        EvaluationCase(
            id="structured-output",
            category="structured_output",
            prompt='Return only {"answer":"ok"}',
            assertions=[EvaluationAssertion(kind="json_object")],
        ),
        EvaluationCase(
            id="edit-application",
            category="edit_application",
            prompt="Return only a unified diff for note.txt, containing 'hello\\n', adding the line coire-eval after hello. Use --- a/note.txt and +++ b/note.txt.",
            assertions=[
                EvaluationAssertion(kind="patch", fixture="hello\n", value="hello\ncoire-eval\n")
            ],
        ),
        EvaluationCase(
            id="long-context",
            category="long_context",
            prompt=f"Read the entire following list and reply with only the value after 'Final token:'.\n{filler}\nFinal token: {sentinel}\nWhat is the final token?",
            assertions=[EvaluationAssertion(kind="retrieval", value=sentinel)],
        ),
    ]


def _tasks() -> list[EvaluationCase]:
    cases = []
    for index, (name, expression) in enumerate(
        [
            ("add", "a + b"),
            ("subtract", "a - b"),
            ("multiply", "a * b"),
            ("maximum", "max(a, b)"),
            ("minimum", "min(a, b)"),
            ("equal", "a == b"),
            ("both", "a and b"),
            ("either", "a or b"),
        ]
    ):
        fixture = f"def {name}(a, b):\n    return 0\n"
        expected = f"def {name}(a, b):\n    return {expression}\n"
        cases.append(
            EvaluationCase(
                id=f"coding-{index + 1}",
                category="coding",
                prompt=f"Fix fixture.py using only a unified diff (--- a/fixture.py and +++ b/fixture.py). Replace return 0 with return {expression}. Preserve every other character.\n\n{fixture}",
                reference=expected,
                assertions=[EvaluationAssertion(kind="patch", fixture=fixture, value=expected)],
            )
        )
    for index, (prompt, answer) in enumerate(
        [
            ("Reply with only the word amber, in lowercase.", "amber"),
            (
                "Sort these numbers in ascending order and return only comma-separated digits without spaces: 3, 1, 2.",
                "1,2,3",
            ),
            ("Return only the uppercase form of coire.", "COIRE"),
            ("Return only the last word of: blue green red.", "red"),
            ("Reply with exactly three repetitions of ha, separated by single spaces.", "ha ha ha"),
            (
                "Return only JSON with one field answer whose value is ok, no spaces or extra fields.",
                '{"answer":"ok"}',
            ),
            ("Count the letters in the word studio and return only the digit.", "6"),
            ("Return the first two letters of TRAIN in lowercase, nothing else.", "tr"),
        ]
    ):
        cases.append(
            EvaluationCase(
                id=f"instruction-{index + 1}",
                category="instruction",
                prompt=prompt,
                reference=answer,
                assertions=[EvaluationAssertion(kind="exact", value=answer)],
            )
        )
    return cases


def cases(template_id: TemplateId) -> list[EvaluationCase]:
    """Return fresh typed fixtures so callers cannot mutate the installed catalog."""
    return _harness() if template_id == "harness-capability" else _tasks()


def template(template_id: TemplateId) -> EvaluationSuiteTemplate:
    definitions = {
        "harness-capability": (SuiteKind.HARNESS, SuiteMode.CAPABILITY),
        "task-coding-instructions": (SuiteKind.TASK, SuiteMode.DETERMINISTIC),
        "judge-rubric": (SuiteKind.JUDGE, SuiteMode.RUBRIC),
        "judge-pairwise": (SuiteKind.JUDGE, SuiteMode.PAIRWISE),
    }
    kind, mode = definitions[template_id]
    items = cases(template_id)
    payload = json.dumps(
        [item.model_dump(mode="json") for item in items],
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )
    case_digest = hashlib.sha256(payload.encode()).hexdigest()
    content_digest = hashlib.sha256(
        json.dumps(
            {
                "template_id": template_id,
                "version": 1,
                "kind": kind,
                "mode": mode,
                "cases_sha256": case_digest,
                "scorer": SCORER_VERSION,
                "rubric": "correctness/instruction_adherence/clarity:0-4",
                "order": "counterbalanced-v1",
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    return EvaluationSuiteTemplate(
        template_id=template_id,
        template_version=1,
        kind=kind,
        mode=mode,
        content_sha256=content_digest,
        cases_sha256=case_digest,
        case_count=len(items),
        license=LICENSE_NOTE,
    )


def templates() -> list[EvaluationSuiteTemplate]:
    return [
        template(name)
        for name in (
            "harness-capability",
            "task-coding-instructions",
            "judge-rubric",
            "judge-pairwise",
        )
    ]
