"""The keyword lexicon: per-language phrases that seed queries and support validation.

Phrases are counted, never interpreted. Matching is case-insensitive after NFKC
normalisation. A phrase in a script written without spaces (Chinese, Japanese, Korean)
matches as a substring; any other phrase must stand on word boundaries, so "test" does
not count inside "contest".
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

import yaml

CATEGORIES = ("exam", "item", "instruction", "key")
REQUIRED_LANGUAGES = ("en", "he", "ko", "zh", "ja", "pt")
LEXICON_PATH = Path(__file__).parent / "lexicon.yaml"


class LexiconError(ValueError):
    """The lexicon file is malformed (a usage error for ``crawler run``)."""


def normalise(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold()


def _unspaced(phrase: str) -> bool:
    """True for CJK, kana and Hangul, which are written without word spaces."""
    return any(
        "⺀" <= ch <= "鿿" or "가" <= ch <= "힯" or "豈" <= ch <= "﫿" for ch in phrase
    )


def _pattern(phrase: str) -> re.Pattern[str]:
    escaped = re.escape(normalise(phrase))
    if _unspaced(phrase):
        return re.compile(escaped)
    return re.compile(rf"(?<!\w){escaped}(?!\w)")


@dataclass(frozen=True)
class Lexicon:
    version: int
    languages: dict[str, dict[str, tuple[str, ...]]]

    def has(self, language: str) -> bool:
        return language in self.languages

    def phrases(self, language: str, category: str) -> tuple[str, ...]:
        return self.languages.get(language, {}).get(category, ())

    def keyword_counts(self, text: str, language: str) -> dict[str, int]:
        """Hits per category for ``language``; all zero when it has no lexicon."""
        folded = normalise(text)
        return {
            category: sum(
                len(_pattern(p).findall(folded))
                for p in self.phrases(language, category)
            )
            for category in CATEGORIES
        }

    def any_language_counts(self, text: str) -> dict[str, int]:
        """Hits per category over every language (for link and anchor text)."""
        folded = normalise(text)
        totals = dict.fromkeys(CATEGORIES, 0)
        for language in self.languages:
            for category in CATEGORIES:
                totals[category] += sum(
                    len(_pattern(p).findall(folded))
                    for p in self.phrases(language, category)
                )
        return totals


def load_lexicon(path: Path = LEXICON_PATH) -> Lexicon:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise LexiconError(f"{path.name}: cannot read the lexicon ({error})") from None
    if not isinstance(raw, dict) or not isinstance(raw.get("languages"), dict):
        raise LexiconError(f"{path.name}: needs a 'languages' mapping")
    version = raw.get("version")
    if not isinstance(version, int):
        raise LexiconError(f"{path.name}: 'version' must be an integer")
    languages: dict[str, dict[str, tuple[str, ...]]] = {}
    for language, categories in raw["languages"].items():
        if not isinstance(categories, dict):
            raise LexiconError(f"{path.name}: {language} must map categories to lists")
        entry: dict[str, tuple[str, ...]] = {}
        for category in CATEGORIES:
            phrases = categories.get(category)
            if (
                not isinstance(phrases, list)
                or not phrases
                or not all(isinstance(p, str) and p.strip() for p in phrases)
            ):
                raise LexiconError(
                    f"{path.name}: {language}.{category} must be a non-empty list"
                )
            entry[category] = tuple(p.strip() for p in phrases)
        unknown = set(categories) - set(CATEGORIES)
        if unknown:
            raise LexiconError(f"{path.name}: {language} has unknown {sorted(unknown)}")
        languages[str(language)] = entry
    missing = [code for code in REQUIRED_LANGUAGES if code not in languages]
    if missing:
        raise LexiconError(f"{path.name}: missing required languages {missing}")
    return Lexicon(version=version, languages=languages)
