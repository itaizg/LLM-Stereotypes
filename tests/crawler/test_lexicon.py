"""The keyword lexicon (FR-006, FR-014)."""

from __future__ import annotations

from pathlib import Path

import pytest

from crawler.lexicon import (
    CATEGORIES,
    REQUIRED_LANGUAGES,
    LexiconError,
    load_lexicon,
)


def test_the_committed_lexicon_covers_the_six_languages() -> None:
    lexicon = load_lexicon()
    for language in REQUIRED_LANGUAGES:
        for category in CATEGORIES:
            assert lexicon.phrases(language, category), (language, category)


def test_counting_respects_words_and_unspaced_scripts() -> None:
    lexicon = load_lexicon()
    counts = lexicon.keyword_counts(
        "Practice Test 3. Choose the best answer. A contest is not a TEST? Answer key.",
        "en",
    )
    assert counts["instruction"] == 1
    assert counts["exam"] >= 2  # "practice test" and "test" (not inside "contest")
    assert counts["key"] == 1
    assert (
        lexicon.keyword_counts("次の中から最も適当なものを一つ選べ。", "ja")[
            "instruction"
        ]
        == 3
    )
    assert lexicon.keyword_counts("anything", "xx") == dict.fromkeys(CATEGORIES, 0)
    assert lexicon.has("pt") and not lexicon.has("xx")
    assert lexicon.any_language_counts("gabarito da prova")["key"] == 1


@pytest.mark.parametrize(
    "text",
    [
        "languages: []\n",
        "version: one\nlanguages: {}\n",
        "version: 1\nlanguages: "
        "{en: {exam: [], item: [a], instruction: [b], key: [c]}}\n",
        "version: 1\nlanguages: "
        "{en: {exam: [a], item: [a], instruction: [b], key: [c]}}\n",
        "version: 1\nlanguages: {en: [a]}\n",
        "{{not yaml",
    ],
)
def test_a_malformed_lexicon_is_a_usage_error(tmp_path: Path, text: str) -> None:
    path = tmp_path / "lexicon.yaml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(LexiconError):
        load_lexicon(path)


def test_unknown_categories_are_refused(tmp_path: Path) -> None:
    good = load_lexicon()
    lines = ["version: 1", "languages:"]
    for language in REQUIRED_LANGUAGES:
        lines.append(f"  {language}:")
        for category in CATEGORIES:
            lines.append(f"    {category}: [{good.phrases(language, category)[0]!r}]")
    lines.append("    extra: [x]")
    path = tmp_path / "lexicon.yaml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(LexiconError, match="unknown"):
        load_lexicon(path)
