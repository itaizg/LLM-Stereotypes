import pytest

from harness.scoring import extract_answer, score_response

EXTRACTABLE = [
    ("B", "B"),
    ("B)", "B"),
    ("B.", "B"),
    ("(B)", "B"),
    ("**B**", "B"),
    ("The answer is B.", "B"),
    ("The answer is: B", "B"),
    ("Choose B", "B"),
    ("  B  \n", "B"),
    ("b", "B"),  # case-insensitive
    ("A note: let me think...\nB", "B"),  # trailing bare-letter line
]

NOT_EXTRACTABLE = [
    "",
    "   ",
    "I'm not sure, this question is ambiguous.",
    "This is a difficult question with no clear right answer.",
]


@pytest.mark.parametrize("raw,expected", EXTRACTABLE)
def test_extract_answer_recognizes_common_formats(raw, expected):
    assert extract_answer(raw) == expected


@pytest.mark.parametrize("raw", NOT_EXTRACTABLE)
def test_extract_answer_returns_none_for_unrecognizable_text(raw):
    assert extract_answer(raw) is None


def test_score_response_correct():
    scored = score_response("B", "B")
    assert scored.is_correct
    assert not scored.is_extraction_failure
    assert scored.extracted_answer == "B"


def test_score_response_incorrect():
    scored = score_response("C", "B")
    assert not scored.is_correct
    assert not scored.is_extraction_failure
    assert scored.extracted_answer == "C"


def test_score_response_extraction_failure_is_tracked_not_silently_wrong():
    scored = score_response("I refuse to answer.", "B")
    assert scored.is_extraction_failure
    assert scored.extracted_answer is None
    # An extraction failure still counts toward "incorrect" for primary
    # accuracy, but callers can distinguish it from a genuine wrong answer.
    assert not scored.is_correct
