"""Conservative text screening; never a substitute for semantic or image moderation.

No identity/role is accepted by this policy: administrators cannot bypass it.
Negative prompts are not treated as positive requests (a safe exclusion is not intent).
"""

from __future__ import annotations

import re
import unicodedata

from coire_core.errors import ImageContentBlocked

POLICY_VERSION = "child-safety-text-v1"
_YOUTH = re.compile(
    r"\b(?:minors?|underage|under age|children|child|kids?|toddlers?|bab(?:y|ies)|infants?|"
    r"preteen|pre teen|teen(?:ager)?s?|adolescents?|pubescen\w*|prepubescen\w*|"
    r"schoolgirls?|schoolboys?|schoolchildren|little girls?|little boys?|young girls?|young boys?|"
    r"very young|loli(?:ta|con)?|shota(?:con)?|juveniles?|niñ[oa]s?|enfants?|kinder)\b"
)
_AGE = re.compile(
    r"\b(?:(\d{1,2})\s*(?:years? old|yrs? old|year olds?|yo|y o)|(?:age[d]?|under)\s*(\d{1,2}))\b"
)
_SEXUAL = re.compile(
    r"\b(?:nude|nudes|nudity|naked|topless|bottomless|unclothed|undressed|bare breasts?|"
    r"genitals?|penis|vagina|vulva|nipples?|porn\w*|sex(?:ual\w*|y)?|erotic\w*|"
    r"seduct\w*|fetish\w*|lingerie|masturb\w*|intercourse|orgasm\w*|"
    r"provocative|sensual|boudoir|nsfw|hentai|ecchi)\b"
)
_NUMBER_WORDS = {
    "zero": "0",
    "one": "1",
    "two": "2",
    "three": "3",
    "four": "4",
    "five": "5",
    "six": "6",
    "seven": "7",
    "eight": "8",
    "nine": "9",
    "ten": "10",
    "eleven": "11",
    "twelve": "12",
    "thirteen": "13",
    "fourteen": "14",
    "fifteen": "15",
    "sixteen": "16",
    "seventeen": "17",
    "eighteen": "18",
}


def _normalize(prompt: str) -> str:
    value = unicodedata.normalize("NFKC", prompt).casefold()
    value = "".join(char for char in value if unicodedata.category(char) != "Cf")
    value = re.sub(r"[^\w]+", " ", value)
    value = re.sub(
        r"\b(?:" + "|".join(_NUMBER_WORDS) + r")\b", lambda match: _NUMBER_WORDS[match[0]], value
    )
    return value


def ensure_image_content_allowed(prompt: str | None, *, explicit: bool = False) -> None:
    """Reject recognizable sexualized youth intent regardless of caller authority."""
    text = _normalize(prompt or "")
    youth = bool(_YOUTH.search(text))
    for match in _AGE.finditer(text):
        age = int(match[1] or match[2])
        if age < 18 or (match[0].startswith("under ") and age <= 18):
            youth = True
    if youth and (explicit or _SEXUAL.search(text)):
        raise ImageContentBlocked()
