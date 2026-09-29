"""Reasoning cannot leak into answer text across engine stream boundaries."""

from coire_api.chat.reasoning import ReasoningParser
from coire_core.models.registry import Reasoning


def test_split_markers_keep_reasoning_separate() -> None:
    parser = ReasoningParser(Reasoning.THINKING)
    output = []
    for chunk in ["Preface <thi", "nk>private", " thought</thi", "nk>Answer"]:
        output.extend(parser.feed(chunk))
    output.extend(parser.finish())
    assert output == [
        ("answer", "Preface "),
        ("reasoning", "private"),
        ("reasoning", " thought"),
        ("answer", "Answer"),
    ]


def test_unclosed_thinking_and_partial_open_marker() -> None:
    parser = ReasoningParser(Reasoning.HYBRID)
    assert parser.feed("<think>hidden") == [("reasoning", "hidden")]
    assert parser.feed("</thi") == []
    assert parser.finish() == [("reasoning", "</thi")]

    parser = ReasoningParser(Reasoning.THINKING)
    assert parser.feed("safe <thi") == [("answer", "safe ")]
    assert parser.finish() == []


def test_non_reasoning_profile_does_not_parse_tags() -> None:
    parser = ReasoningParser(Reasoning.NONE)
    assert parser.feed("ordinary text") == [("answer", "ordinary text")]
    assert parser.finish() == []


def test_mixed_answer_and_reasoning_in_one_frame() -> None:
    parser = ReasoningParser(Reasoning.HYBRID)
    assert parser.feed("Start<think>private</think>Finish") == [
        ("answer", "Start"),
        ("reasoning", "private"),
        ("answer", "Finish"),
    ]
    assert parser.finish() == []
