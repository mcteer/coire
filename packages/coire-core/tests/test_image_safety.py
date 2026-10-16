"""Synthetic text-only policy cases; no images or engines are invoked."""

import pytest

from coire_core.errors import ImageContentBlocked
from coire_core.image_safety import ensure_image_content_allowed


@pytest.mark.parametrize(
    "subject",
    [
        "a minor",
        "a child",
        "a teenager",
        "an adolescent",
        "a 12-year-old person",
        "a twelve year old person",
        "a person aged 17",
        "a person under 18",
        "a \uff11\uff17-year-old person",
        "a ch\u200bild",
        "a schoolgirl",
        "a pre-teen",
    ],
)
@pytest.mark.parametrize("explicit", [False, True])
def test_youth_sexualization_is_blocked(subject: str, explicit: bool) -> None:
    with pytest.raises(ImageContentBlocked):
        ensure_image_content_allowed(f"sexualized depiction of {subject}", explicit=explicit)


@pytest.mark.parametrize("role", ["user", "admin", "api_key"])
def test_authority_is_not_a_policy_input(role: str) -> None:
    # Identity cannot supply an exemption because the policy accepts no principal.
    assert role
    with pytest.raises(ImageContentBlocked):
        ensure_image_content_allowed("a minor", explicit=True)


@pytest.mark.parametrize(
    "prompt",
    [
        "A ceramic teapot on a table",
        "A family wearing winter coats",
        "A fully clothed child reading a book",
        "A nonsexual portrait of a person aged 12",
        "An adult aged 30, nude figure study",
        "An adult aged 18, nude figure study",
        "A nude figure study of an adult under 30",
    ],
)
def test_safe_control_prompts_are_not_blocked(prompt: str) -> None:
    ensure_image_content_allowed(prompt)


def test_explicit_youth_is_conservatively_refused_even_without_sexual_keyword() -> None:
    with pytest.raises(ImageContentBlocked):
        ensure_image_content_allowed("a teenager", explicit=True)
