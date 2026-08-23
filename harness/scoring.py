"""Answer extraction and scoring.

An extraction failure (the model didn't produce a recognizable A-D letter)
is a tracked category, not silently scored as wrong-but-unremarkable — see
CLAUDE.md's "unsexy 80% of every real eval system" point. Callers that want
raw accuracy still get one via ScoredResponse.is_correct (a failure counts
as incorrect there), but harness.stats reports the failure rate alongside
so a spike in it is visible rather than hidden inside a dropped accuracy
number.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Order matters: earlier patterns are stricter and are tried first so a
# clean "B" is never mis-extracted by a looser fallback pattern.
_STRICT_LINE = re.compile(r"^\s*\(?([A-D])\)?[.:]?\s*$", re.MULTILINE | re.IGNORECASE)
_BOLD = re.compile(r"\*\*\s*([A-D])\s*\*\*", re.IGNORECASE)
_PHRASE = re.compile(
    r"\b(?:answer(?:\s+is)?|choose)\s*:?\s*\**\s*\(?([A-D])\)?\b", re.IGNORECASE
)
_LEADING = re.compile(r"^\s*\(?([A-D])\)?\b", re.IGNORECASE)

_PATTERNS = (_STRICT_LINE, _BOLD, _PHRASE, _LEADING)


def extract_answer(raw_response: str) -> str | None:
    text = raw_response.strip()
    if not text:
        return None
    for pattern in _PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group(1).upper()
    return None


@dataclass(frozen=True)
class ScoredResponse:
    extracted_answer: str | None
    is_extraction_failure: bool
    is_correct: bool


def score_response(raw_response: str, gold_answer: str) -> ScoredResponse:
    extracted = extract_answer(raw_response)
    if extracted is None:
        return ScoredResponse(
            extracted_answer=None, is_extraction_failure=True, is_correct=False
        )
    return ScoredResponse(
        extracted_answer=extracted,
        is_extraction_failure=False,
        is_correct=extracted == gold_answer.strip().upper(),
    )
