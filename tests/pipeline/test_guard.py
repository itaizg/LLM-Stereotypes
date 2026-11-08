"""Normalization, the corpus guard, and the architecture rules of the pipeline.

Every string here is synthetic (FR-025): the "corpus" is invented text, so these
tests prove the guard's behavior without any real exam question in git.
"""

from __future__ import annotations

import ast
import json
import random
import re
import string
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest
import yaml

from pipeline import corpus
from pipeline.corpus import (
    SAMPLE_CAP_PER_EXAM,
    build_fingerprints,
    canonical_json,
    canonical_question_id,
    canonical_text,
    content_hash,
    fingerprint_text,
    fingerprints_of,
    winnow,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

LONG_EN = (
    "Synthetic corpus stem about the migration of arctic terns across the "
    "southern ocean during the austral summer"
)
LONG_JA = (
    "合成された問題文ですこれは試験のための人工的なテキストで長い文章になっています"
)

OPEN_LICENCE = {"url": "https://example.test/licence", "statement": "CC BY 4.0 text"}


class TestNormalization:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (
                "\uff28\uff45\uff4c\uff4c\uff4f\u3000\uff37\uff4f\uff52\uff4c\uff44\uff01",
                "Hello World",
            ),  # NFKC folds full-width forms
            ("  a\t\n b  ", "a b"),  # whitespace runs collapse to one space
            ("(A) What's this?", "A Whats this"),  # every P* category is dropped
            ("問１。", "問1"),  # CJK punctuation and full-width digits
            ("¿Qué — pasa?", "Qué pasa"),  # em dash is punctuation
            ("2+2=4", "2+2=4"),  # symbols (Sm) are not punctuation
            ("Ice cream", "Ice cream"),  # case is kept
        ],
    )
    def test_canonical_text_table(self, raw: str, expected: str) -> None:
        assert canonical_text(raw) == expected

    def test_identity_is_strict_about_case_and_spaces(self) -> None:
        assert canonical_text("Ice cream") != canonical_text("ice cream")
        assert canonical_text("ice cream") != canonical_text("icecream")

    def test_fingerprint_text_is_looser_than_identity(self) -> None:
        assert fingerprint_text("Ice  Cream!") == "icecream"
        assert fingerprint_text("Ice cream") == fingerprint_text("ice cream")
        assert fingerprint_text("ice cream") == fingerprint_text("icecream")

    def test_case_variants_get_different_ids_but_the_same_fingerprints(self) -> None:
        a = canonical_question_id("Ice cream", ["x", "y"])
        b = canonical_question_id("ice cream", ["x", "y"])
        assert a != b
        assert fingerprints_of(LONG_EN) == fingerprints_of(LONG_EN.upper())

    def test_canonical_question_id_is_stable_under_punctuation_and_spacing(
        self,
    ) -> None:
        one = canonical_question_id("What is 2 + 2?", ["Four.", "Five"])
        two = canonical_question_id("What is 2 + 2", ["Four", "Five"])
        assert one == two
        assert one.startswith("cq_") and len(one) == 3 + 20

    def test_choice_order_and_content_matter(self) -> None:
        base = canonical_question_id("stem", ["a", "b"])
        assert base != canonical_question_id("stem", ["b", "a"])
        assert base != canonical_question_id("stem", ["a", "bb"])
        # A choice boundary must not be ambiguous: ["ab", "c"] is not ["a", "bc"].
        assert canonical_question_id("s", ["ab", "c"]) != canonical_question_id(
            "s", ["a", "bc"]
        )

    def test_canonical_json_is_sorted_compact_and_unescaped(self) -> None:
        assert canonical_json({"b": 1, "a": "é問"}) == '{"a":"é問","b":1}'


def _question_fields() -> dict[str, object]:
    return {
        "edition_id": "sat-2020-main",
        "section": "math",
        "item": "7",
        "print_order": 7,
        "stem": "Synthetic stem 7",
        "choices": [{"label": "A", "text": "one"}, {"label": "B", "text": "two"}],
        "has_figure": False,
        "has_table": False,
        "has_audio": False,
        "is_free_response": False,
        "stimulus_id": None,
        "artifact_sha256": "ab" * 32,
        "page": 3,
    }


class TestContentHash:
    def test_ignores_pipeline_version(self) -> None:
        fields = _question_fields()
        first = content_hash({**fields, "pipeline_version": "abc123"})
        second = content_hash({**fields, "pipeline_version": "def456-dirty"})
        assert first == second

    @pytest.mark.parametrize(
        ("field", "value"),
        [("print_order", 8), ("stem", "Synthetic stem 8"), ("page", 4)],
    )
    def test_changes_with_content(self, field: str, value: object) -> None:
        fields = _question_fields()
        assert content_hash({**fields, field: value}) != content_hash(fields)

    def test_refuses_partial_records(self) -> None:
        fields = _question_fields()
        del fields["page"]
        with pytest.raises(KeyError):
            content_hash(fields)


class TestWinnowing:
    def test_short_text_has_no_fingerprints(self) -> None:
        assert winnow("short") == set()

    def test_every_63_character_run_is_detected(self) -> None:
        # Guarantee from research R13: k + w - 1 = 63 shared characters always
        # share at least one fingerprint, wherever the run starts.
        rng = random.Random(7)
        source = "".join(rng.choices(string.ascii_lowercase, k=300))
        corpus_prints = winnow(source)
        run = corpus.WINNOW_K + corpus.WINNOW_W - 1
        assert run == 63
        for start in range(len(source) - run + 1):
            assert winnow(source[start : start + run]) & corpus_prints, start

    def test_build_fingerprints_is_sorted_unique_16_hex(self) -> None:
        prints = build_fingerprints([LONG_EN, LONG_EN, LONG_JA])
        assert prints == sorted(set(prints))
        assert all(re.fullmatch(r"[0-9a-f]{16}", p) for p in prints)


def make_repo(
    root: Path,
    files: dict[str, str | bytes],
    *,
    fingerprints: list[str] | None = None,
    samples: Sequence[Mapping[str, object]] | None = None,
) -> Path:
    """Create a throwaway git repository with the guard's data files."""
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    data_files: dict[str, str | bytes] = dict(files)
    data_files[corpus.FINGERPRINTS_PATH] = "\n".join(fingerprints or []) + "\n"
    data_files[corpus.ALLOWLIST_PATH] = yaml.safe_dump({"samples": samples or []})
    for name, content in data_files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            target.write_bytes(content)
        else:
            target.write_text(content, encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    return root


def kinds(root: Path) -> set[tuple[str, str]]:
    return {(hit.path, hit.kind) for hit in corpus.scan(root)}


class TestCorpusGuard:
    def test_clean_repo_passes(self, tmp_path: Path) -> None:
        root = make_repo(
            tmp_path,
            {"README.md": "Nothing to see here, just ordinary prose."},
            fingerprints=build_fingerprints([LONG_EN]),
        )
        assert corpus.scan(root) == []

    def test_corpus_text_in_a_tracked_file_fails(self, tmp_path: Path) -> None:
        root = make_repo(
            tmp_path,
            {"notes/leak.md": f"A note.\n\n{LONG_EN.upper()}\n"},
            fingerprints=build_fingerprints([LONG_EN]),
        )
        assert kinds(root) == {("notes/leak.md", "corpus_text")}

    def test_escaped_cjk_in_json_fails(self, tmp_path: Path) -> None:
        escaped = json.dumps({"stem": LONG_JA})  # ensure_ascii=True: \uXXXX
        assert "\\u" in escaped
        root = make_repo(
            tmp_path,
            {"data/bank.json": escaped},
            fingerprints=build_fingerprints([LONG_JA]),
        )
        assert kinds(root) == {("data/bank.json", "corpus_text")}

    def test_escaped_cjk_in_plain_text_fails(self, tmp_path: Path) -> None:
        escaped = json.dumps(LONG_JA)[1:-1]
        root = make_repo(
            tmp_path,
            {"docs/escaped.txt": escaped},
            fingerprints=build_fingerprints([LONG_JA]),
        )
        assert kinds(root) == {("docs/escaped.txt", "corpus_text")}

    def test_text_split_across_notebook_output_lines_fails(
        self, tmp_path: Path
    ) -> None:
        words = LONG_EN.split(" ")
        lines = [" ".join(words[i : i + 3]) + "\n" for i in range(0, len(words), 3)]
        notebook = {
            "cells": [
                {
                    "cell_type": "code",
                    "outputs": [{"output_type": "stream", "text": lines}],
                }
            ]
        }
        root = make_repo(
            tmp_path,
            {"analysis.ipynb": json.dumps(notebook)},
            fingerprints=build_fingerprints([LONG_EN]),
        )
        assert kinds(root) == {("analysis.ipynb", "corpus_text")}

    def test_html_entities_fail(self, tmp_path: Path) -> None:
        entities = "".join(f"&#{ord(ch)};" for ch in LONG_JA)
        root = make_repo(
            tmp_path,
            {"site/page.html": f"<p>{entities}</p>"},
            fingerprints=build_fingerprints([LONG_JA]),
        )
        assert kinds(root) == {("site/page.html", "corpus_text")}

    def test_machine_translated_english_is_covered(self, tmp_path: Path) -> None:
        original = LONG_JA
        english_mt = "Synthetic machine translated text about migrating arctic terns"
        root = make_repo(
            tmp_path,
            {"docs/mt.md": english_mt},
            fingerprints=build_fingerprints([original, english_mt]),
        )
        assert kinds(root) == {("docs/mt.md", "corpus_text")}

    @pytest.mark.parametrize("suffix", ["pdf", "hwp", "parquet", "mp3"])
    def test_tracked_binaries_that_can_carry_papers_fail(
        self, tmp_path: Path, suffix: str
    ) -> None:
        root = make_repo(tmp_path, {f"stash/paper.{suffix}": b"\x00\x01binary"})
        assert kinds(root) == {(f"stash/paper.{suffix}", "refused_binary")}

    def test_other_binaries_are_allowed(self, tmp_path: Path) -> None:
        root = make_repo(
            tmp_path,
            {"results/plot.png": b"\x89PNG\r\n\x1a\n\x00"},
            fingerprints=build_fingerprints([LONG_EN]),
        )
        assert corpus.scan(root) == []

    def test_allowlisted_sample_is_skipped_and_the_fingerprint_file_is_not_scanned(
        self, tmp_path: Path
    ) -> None:
        sample = {
            "path": "pipeline/samples/open.json",
            "exam": "enem",
            "question_count": 3,
            "licence": "CC BY 4.0",
            "licence_evidence": OPEN_LICENCE,
        }
        root = make_repo(
            tmp_path,
            {"pipeline/samples/open.json": json.dumps({"stem": LONG_EN})},
            fingerprints=build_fingerprints([LONG_EN]),
            samples=[sample],
        )
        assert corpus.scan(root) == []

    def test_sample_over_the_per_exam_cap_fails(self, tmp_path: Path) -> None:
        def entry(name: str, count: int) -> dict[str, object]:
            return {
                "path": f"pipeline/samples/{name}.json",
                "exam": "enem",
                "question_count": count,
                "licence": "CC BY 4.0",
                "licence_evidence": OPEN_LICENCE,
            }

        (tmp_path / "ok").mkdir()
        within = make_repo(
            tmp_path / "ok",
            {},
            samples=[entry("a", 30), entry("b", SAMPLE_CAP_PER_EXAM - 30)],
        )
        assert corpus.scan(within) == []

        (tmp_path / "over").mkdir()
        over = make_repo(
            tmp_path / "over",
            {},
            samples=[entry("a", 30), entry("b", SAMPLE_CAP_PER_EXAM - 30 + 1)],
        )
        assert kinds(over) == {("exam:enem", "over_cap")}

    @pytest.mark.parametrize(
        ("licence", "evidence", "expected"),
        [
            ("", OPEN_LICENCE, "missing_licence"),
            ("CC BY 4.0", {}, "missing_licence_evidence"),
            ("CC BY 4.0", {"url": "https://example.test"}, "missing_licence_evidence"),
            ("CC BY 4.0", {"url": " ", "statement": "x"}, "missing_licence_evidence"),
        ],
    )
    def test_allowlist_entry_without_licence_evidence_fails(
        self,
        tmp_path: Path,
        licence: str,
        evidence: dict[str, str],
        expected: str,
    ) -> None:
        sample = {
            "path": "pipeline/samples/s.json",
            "exam": "csat",
            "question_count": 1,
            "licence": licence,
            "licence_evidence": evidence,
        }
        root = make_repo(tmp_path, {"pipeline/samples/s.json": "{}"}, samples=[sample])
        assert kinds(root) == {("pipeline/samples/s.json", expected)}

    def test_main_reports_paths_not_text_and_exits_3(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        root = make_repo(
            tmp_path,
            {"notes/leak.md": LONG_EN},
            fingerprints=build_fingerprints([LONG_EN]),
        )
        monkeypatch.chdir(root)
        assert corpus.main([]) == 3
        out = capsys.readouterr().out
        assert "notes/leak.md" in out and "corpus_text" in out
        assert "arctic" not in out

    def test_main_exits_0_on_the_real_repository(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(REPO_ROOT)
        assert corpus.main([]) == 0


class TestArchitecture:
    def test_harness_never_imports_pipeline(self) -> None:
        offenders: list[str] = []
        for source in sorted((REPO_ROOT / "harness").rglob("*.py")):
            tree = ast.parse(source.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module]
                else:
                    continue
                if any(n == "pipeline" or n.startswith("pipeline.") for n in names):
                    offenders.append(f"{source.relative_to(REPO_ROOT)}:{node.lineno}")
        assert offenders == []

    @pytest.mark.parametrize(
        ("source", "table", "writes"),
        [
            (
                'conn.execute("INSERT INTO answer_keys VALUES (%s)")',
                "answer_keys",
                True,
            ),
            ('"UPDATE  answer_keys SET page = 1"', "answer_keys", True),
            ('"delete from answer_keys"', "answer_keys", True),
            ('insert_if_new(conn, "answer_keys", rows)', "answer_keys", True),
            ('reconcile(conn, "translations", scope, rows)', "translations", True),
            ('"SELECT * FROM answer_keys"', "answer_keys", False),
            ('insert_if_new(conn, "translations", rows)', "answer_keys", False),
        ],
    )
    def test_write_detector_recognises_writes(
        self, source: str, table: str, writes: bool
    ) -> None:
        assert writes_table(source, table) is writes

    def test_single_writers(self) -> None:
        # FR-016: exactly one stage writes each authoritative table.
        owners = {"answer_keys": "keys.py", "translations": "translate.py"}
        offenders: list[str] = []
        for source in sorted((REPO_ROOT / "pipeline").glob("*.py")):
            text = source.read_text(encoding="utf-8")
            offenders.extend(
                f"{source.name} writes {table}"
                for table, owner in owners.items()
                if source.name != owner and writes_table(text, table)
            )
        assert offenders == []


def writes_table(source: str, table: str) -> bool:
    """Return True if the source holds SQL or a helper call writing ``table``."""
    sql = re.search(rf"(?is)\b(insert\s+into|update|delete\s+from)\s+{table}\b", source)
    helper = re.search(
        rf"(?s)\b(insert_if_new|reconcile)\(\s*[^)]*[\"']{table}[\"']", source
    )
    return bool(sql or helper)
