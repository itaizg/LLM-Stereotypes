"""Text normalization, canonical identities, and the repository corpus guard.

Two jobs live here because they share one definition of "the same text":

* *Identity* (FR-010): two questions are the same question exactly when their
  original-language stem and choices are equal after ``canonical_text``. The
  rule is strict on purpose, so the canonical link is deterministic across
  rebuilds and near-matches are never linked.
* *Detection* (FR-025/FR-026): the guard must catch a copy of corpus text in a
  tracked file even if case or line breaks changed, so it uses the looser
  ``fingerprint_text``, defined as ``canonical_text`` plus case-folding and
  whitespace removal. Detection is more forgiving than identity by design.

This module imports only the standard library and PyYAML so that
``python -m pipeline.corpus`` runs in pre-commit without the ``pipeline``
extra (research R13).
"""

from __future__ import annotations

import contextlib
import hashlib
import html
import json
import re
import subprocess
import sys
import unicodedata
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import yaml

# The only definition of the committed-sample cap (spec Assumptions): changing
# it is a deliberate, reviewed commit.
SAMPLE_CAP_PER_EXAM: int = 50

# Winnowing parameters (research R13). Any shared normalized run of at least
# WINNOW_K + WINNOW_W - 1 = 63 characters is guaranteed to share a fingerprint.
WINNOW_K: int = 24
WINNOW_W: int = 40

# Tracked files of these types can carry whole papers and cannot be
# fingerprinted reliably, so they are refused unless allowlisted.
REFUSED_SUFFIXES: frozenset[str] = frozenset(
    {".pdf", ".hwp", ".hwpx", ".parquet", ".mp3", ".wav", ".m4a", ".aac", ".ogg"}
)

# Fields hashed into a question's content_hash (research R7). pipeline_version
# is deliberately absent, so an unrelated commit never changes a hash.
CONTENT_HASH_FIELDS: tuple[str, ...] = (
    "edition_id",
    "section",
    "item",
    "print_order",
    "stem",
    "choices",
    "has_figure",
    "has_table",
    "has_audio",
    "is_free_response",
    "stimulus_id",
    "artifact_sha256",
    "page",
)

FINGERPRINTS_PATH: str = "pipeline/fingerprints.txt"
ALLOWLIST_PATH: str = "pipeline/samples/allowlist.yaml"

_WHITESPACE = re.compile(r"\s+")


def canonical_text(text: str) -> str:
    """Normalize text exactly as FR-010 states.

    Unicode NFKC, drop every punctuation character (category P*), collapse each
    whitespace run to one space, strip both ends. Case is kept and whitespace is
    collapsed rather than removed, so ``Ice cream`` and ``ice cream`` (or
    ``ice cream`` and ``icecream``) stay distinct questions.
    """
    folded = unicodedata.normalize("NFKC", text)
    kept = "".join(ch for ch in folded if not unicodedata.category(ch).startswith("P"))
    return _WHITESPACE.sub(" ", kept).strip()


def fingerprint_text(text: str) -> str:
    """Normalize text for the guard: ``canonical_text``, case-folded, no whitespace."""
    return _WHITESPACE.sub("", canonical_text(text).casefold())


def canonical_json(value: object) -> str:
    """Serialize with sorted keys, no ASCII escaping, and compact separators.

    Removes serializer ambiguity so hashes are reproducible (research R7).
    """
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def sha256_hex(data: bytes | str) -> str:
    """Return the SHA-256 hex digest of bytes or UTF-8 text."""
    raw = data.encode("utf-8") if isinstance(data, str) else data
    return hashlib.sha256(raw).hexdigest()


def canonical_question_id(stem: str, choices: Sequence[str]) -> str:
    """Return the exact-match duplicate key for a stem and its choice texts."""
    parts = [canonical_text(stem), *(canonical_text(c) for c in choices)]
    return "cq_" + sha256_hex("\x1f".join(parts))[:20]


def content_hash(fields: Mapping[str, object]) -> str:
    """Hash the content-defining fields of a question (research R7).

    Raises ``KeyError`` when a required field is missing, so a record cannot be
    hashed from partial data.
    """
    return sha256_hex(
        canonical_json({name: fields[name] for name in CONTENT_HASH_FIELDS})
    )


# --- fingerprinting ---------------------------------------------------------


def winnow(normalized: str, k: int = WINNOW_K, w: int = WINNOW_W) -> set[str]:
    """Return the winnowed k-gram fingerprints of already-normalized text.

    Schleimer, Wilkerson & Aiken (2003): hash every k-gram and keep the minimum
    of each window of ``w`` hashes. Text shorter than ``k`` yields nothing,
    since a handful of characters cannot identify a source. Fingerprints are the
    first 16 hex digits of each selected k-gram's SHA-256.
    """
    if len(normalized) < k:
        return set()
    hashes = [
        int(sha256_hex(normalized[i : i + k])[:16], 16)
        for i in range(len(normalized) - k + 1)
    ]
    window = min(w, len(hashes))
    chosen = {min(hashes[i : i + window]) for i in range(len(hashes) - window + 1)}
    return {f"{value:016x}" for value in chosen}


def fingerprints_of(text: str) -> set[str]:
    """Fingerprint raw text: normalize with ``fingerprint_text``, then winnow."""
    return winnow(fingerprint_text(text))


def build_fingerprints(texts: Sequence[str]) -> list[str]:
    """Return the sorted, unique fingerprints of many texts."""
    found: set[str] = set()
    for text in texts:
        found |= fingerprints_of(text)
    return sorted(found)


# --- the guard --------------------------------------------------------------


@dataclass(frozen=True)
class Sample:
    """One allowlisted, openly licensed sample file."""

    path: str
    exam: str
    question_count: int
    licence: str
    licence_evidence: Mapping[str, str]


@dataclass(frozen=True)
class GuardHit:
    """One guard failure. Never carries the matched text."""

    path: str
    kind: str


def load_allowlist(path: Path) -> list[Sample]:
    """Read ``samples/allowlist.yaml`` and narrow it to ``Sample`` records."""
    if not path.exists():
        return []
    parsed: object = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(parsed, dict):
        return []
    entries = parsed.get("samples") or []
    samples: list[Sample] = []
    if not isinstance(entries, list):
        return samples
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        evidence = entry.get("licence_evidence")
        samples.append(
            Sample(
                path=str(entry.get("path", "")),
                exam=str(entry.get("exam", "")),
                question_count=int(entry.get("question_count") or 0),
                licence=str(entry.get("licence") or ""),
                licence_evidence=(
                    {str(k): str(v) for k, v in evidence.items()}
                    if isinstance(evidence, dict)
                    else {}
                ),
            )
        )
    return samples


def load_fingerprints(path: Path) -> set[str]:
    """Read the committed fingerprint file (one hex digest per line)."""
    if not path.exists():
        return set()
    return {line.strip() for line in path.read_text().splitlines() if line.strip()}


def _json_strings(node: object) -> Iterator[str]:
    """Yield every string value in parsed JSON, however deeply nested."""
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for value in node.values():
            yield from _json_strings(value)
    elif isinstance(node, list):
        if node and all(isinstance(value, str) for value in node):
            # Notebook outputs split text into per-line strings; scan the joined
            # text so a stem broken across lines is still seen.
            yield "".join(str(value) for value in node)
            return
        for value in node:
            yield from _json_strings(value)


_UNICODE_ESCAPE = re.compile(r"\\u([0-9a-fA-F]{4})")


def _decode_escapes(text: str) -> str:
    """Decode ``\\uXXXX`` escapes and HTML entities so encoded text is still seen."""
    decoded = _UNICODE_ESCAPE.sub(lambda m: chr(int(m.group(1), 16)), text)
    with contextlib.suppress(UnicodeError):
        # Rejoin surrogate pairs produced by escaped astral characters.
        decoded = decoded.encode("utf-16", "surrogatepass").decode("utf-16")
    return html.unescape(decoded)


def _strings_of_file(path: Path) -> Iterator[str]:
    """Yield the scannable strings of one tracked text file."""
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return  # Binary or unreadable: not scannable text.
    if path.suffix in {".json", ".ipynb"}:
        try:
            yield from _json_strings(json.loads(text))
            return
        except json.JSONDecodeError:
            pass
    yield text
    decoded = _decode_escapes(text)
    if decoded != text:
        yield decoded


def _tracked_files(repo_root: Path) -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=repo_root,
        capture_output=True,
        check=True,
    )
    return [p for p in result.stdout.decode("utf-8").split("\0") if p]


def scan(repo_root: Path) -> list[GuardHit]:
    """Check every tracked file against the corpus rules (FR-025, FR-026)."""
    samples = load_allowlist(repo_root / ALLOWLIST_PATH)
    corpus = load_fingerprints(repo_root / FINGERPRINTS_PATH)
    allowlisted = {s.path for s in samples}
    hits: list[GuardHit] = []

    per_exam: dict[str, int] = {}
    for sample in samples:
        per_exam[sample.exam] = per_exam.get(sample.exam, 0) + sample.question_count
        if not sample.licence.strip():
            hits.append(GuardHit(sample.path, "missing_licence"))
        if not (
            sample.licence_evidence.get("url", "").strip()
            and sample.licence_evidence.get("statement", "").strip()
        ):
            hits.append(GuardHit(sample.path, "missing_licence_evidence"))
    hits.extend(
        GuardHit(f"exam:{exam}", "over_cap")
        for exam, count in sorted(per_exam.items())
        if count > SAMPLE_CAP_PER_EXAM
    )

    for name in _tracked_files(repo_root):
        if name in allowlisted or name == FINGERPRINTS_PATH:
            continue
        file_path = repo_root / name
        if file_path.suffix.lower() in REFUSED_SUFFIXES:
            hits.append(GuardHit(name, "refused_binary"))
            continue
        if not corpus or not file_path.is_file():
            continue
        if any(fingerprints_of(s) & corpus for s in _strings_of_file(file_path)):
            hits.append(GuardHit(name, "corpus_text"))
    return hits


def main(argv: Sequence[str] | None = None) -> int:
    """Run the guard from the repository root; exit 3 on any hit (integrity refusal)."""
    del argv  # The guard takes no arguments; the signature matches other entry points.
    hits = scan(Path.cwd())
    for hit in hits:
        print(f"corpus-guard: {hit.kind}: {hit.path}")
    return 3 if hits else 0


if __name__ == "__main__":
    sys.exit(main())
