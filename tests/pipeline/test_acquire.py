"""Tests for the catalog and ledger, the catalog stage, and candidate review."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

import psycopg
import pytest
from psycopg import sql

from pipeline.acquire import review, run_catalog
from pipeline.manifests import (
    EXAM_IDS,
    CatalogError,
    LedgerError,
    append_event,
    ledger_is_prefix_extension,
    load_catalog,
    read_ledger,
    replay_ledger,
    source_id_for,
    validate_catalog,
)
from pipeline.store import Counts

if TYPE_CHECKING:
    from conftest import DatasetBuilder

REPO_ROOT = Path(__file__).resolve().parents[2]
SAT = "sat-2020-main"


# --- the ledger --------------------------------------------------------------


def _write(path: Path, *lines: str) -> Path:
    path.write_text("".join(line + "\n" for line in lines), encoding="utf-8")
    return path


class TestLedger:
    def test_append_assigns_increasing_seq_and_uses_canonical_json(
        self, tmp_path: Path
    ) -> None:
        ledger = tmp_path / "ledger.jsonl"
        first = append_event(
            ledger,
            "candidate",
            {"url": "u", "source_id": "src_1"},
            at="2026-01-01T00:00:00Z",
        )
        second = append_event(
            ledger,
            "review",
            {"source_id": "src_1", "decision": "approved"},
            at="2026-01-01T00:00:01Z",
        )
        assert (first.seq, second.seq) == (1, 2)
        lines = ledger.read_text(encoding="utf-8").splitlines()
        assert lines[0] == (
            '{"at":"2026-01-01T00:00:00Z","kind":"candidate",'
            '"seq":1,"source_id":"src_1","url":"u"}'
        )
        assert [e.seq for e in read_ledger(ledger)] == [1, 2]

    def test_a_missing_ledger_is_empty(self, tmp_path: Path) -> None:
        assert read_ledger(tmp_path / "absent.jsonl") == []

    @pytest.mark.parametrize(
        "lines",
        [
            ['{"seq":1,"kind":"fetch","at":"t"}', '{"seq":1,"kind":"fetch","at":"t"}'],
            ['{"seq":2,"kind":"fetch","at":"t"}', '{"seq":1,"kind":"fetch","at":"t"}'],
            ['{"seq":"1","kind":"fetch","at":"t"}'],
            ['{"seq":1,"kind":"mystery","at":"t"}'],
            ["not json"],
            ["[1, 2]"],
        ],
    )
    def test_malformed_or_non_increasing_ledgers_are_refused(
        self, tmp_path: Path, lines: list[str]
    ) -> None:
        with pytest.raises(LedgerError):
            read_ledger(_write(tmp_path / "ledger.jsonl", *lines))

    def test_unknown_kinds_cannot_be_appended(self, tmp_path: Path) -> None:
        with pytest.raises(LedgerError):
            append_event(tmp_path / "l.jsonl", "mystery", {}, at="t")

    @pytest.mark.parametrize(
        ("old", "new", "expected"),
        [
            (b"", b"anything\n", True),
            (b"a\nb\n", b"a\nb\nc\n", True),
            (b"a\nb\n", b"a\nb\n", True),
            (b"a\nb\n", b"a\nX\nc\n", False),
            (b"a\nb\n", b"a\n", False),
            (b"a\nb", b"a\nbc\n", False),  # an unterminated last line was rewritten
        ],
    )
    def test_prefix_extension(self, old: bytes, new: bytes, expected: bool) -> None:
        assert ledger_is_prefix_extension(old, new) is expected

    def test_source_ids_are_stable_hashes_of_the_url(self) -> None:
        one = source_id_for("https://sat.exams.test/a.pdf")
        assert one == source_id_for("https://sat.exams.test/a.pdf")
        assert one != source_id_for("https://sat.exams.test/b.pdf")
        assert one.startswith("src_") and len(one) == 4 + 16


# --- catalog validation (US1, FR-001..FR-006) --------------------------------


def problems(ds: DatasetBuilder) -> list[str]:
    ds.save()
    catalog = load_catalog(ds.catalog_path)
    return validate_catalog(catalog, replay_ledger(read_ledger(ds.ledger_path)))


def pending_source(ds: DatasetBuilder) -> str:
    return ds.add_source(
        "https://sat.exams.test/unreviewed", SAT, "statement", approve=False
    )


def drop_cell(ds: DatasetBuilder) -> None:
    ds.catalog["editions"] = [
        e for e in ds.catalog["editions"] if e["edition_id"] != SAT
    ]


def empty_evidence(ds: DatasetBuilder) -> None:
    ds.edition(SAT)["evidence"] = []


def cite_pending(ds: DatasetBuilder) -> None:
    ds.edition(SAT)["evidence"] = [
        {"type": "statement", "source_id": pending_source(ds)}
    ]


def cite_rejected(ds: DatasetBuilder) -> None:
    source = ds.add_source("https://sat.exams.test/bad", SAT, "statement")
    ds.review(source, SAT, "statement", approve=False, reason="not official")
    ds.edition(SAT)["evidence"] = [{"type": "statement", "source_id": source}]


def ingested_without_paper(ds: DatasetBuilder) -> None:
    ds.make_ingested(SAT, paper=None)


def ingested_without_key(ds: DatasetBuilder) -> None:
    ds.make_ingested(SAT, key=None)


def keys_not_published(ds: DatasetBuilder) -> None:
    ds.catalog["exams"]["sat"]["publishes_keys"] = False


def no_keys_evidence(ds: DatasetBuilder) -> None:
    ds.catalog["exams"]["sat"]["keys_evidence"] = {}


def index_absence_from_incomplete_seed(ds: DatasetBuilder) -> None:
    page = ds.add_source("https://sat.exams.test/news", SAT, "index_page")
    ds.edition(SAT)["evidence"] = [{"type": "index_absence", "source_id": page}]


def blocked_without_reason(ds: DatasetBuilder) -> None:
    ds.edition(SAT)["status"] = "blocked"


def unknown_status(ds: DatasetBuilder) -> None:
    ds.edition(SAT)["status"] = "unknown"


def malformed_edition_id(ds: DatasetBuilder) -> None:
    ds.edition(SAT)["edition_id"] = "sat_2020_main"


def seventh_exam(ds: DatasetBuilder) -> None:
    ds.catalog["exams"]["gre"] = dict(ds.catalog["exams"]["sat"])


def evidence_of_unknown_type(ds: DatasetBuilder) -> None:
    ds.edition(SAT)["evidence"][0]["type"] = "rumour"


BREAKAGES: list[tuple[Callable[[DatasetBuilder], None], str]] = [
    (drop_cell, "cell sat/2020 has no edition"),
    (empty_evidence, "sat-2020-main: no evidence"),
    (cite_pending, "not approved"),
    (cite_rejected, "not approved"),
    (ingested_without_paper, "no paper artifact"),
    (ingested_without_key, "no key artifact"),
    (keys_not_published, "exam sat: publishes_keys must be true"),
    (no_keys_evidence, "exam sat: keys_evidence must cite an approved source"),
    (index_absence_from_incomplete_seed, "complete_listing"),
    (blocked_without_reason, "blocked edition needs status_reason"),
    (unknown_status, "status must be one of"),
    (malformed_edition_id, "edition_id must be"),
    (seventh_exam, "unknown exam: gre"),
    (evidence_of_unknown_type, "unknown evidence type"),
]


class TestCatalogValidation:
    def test_a_complete_synthetic_catalog_is_valid(
        self, dataset: DatasetBuilder
    ) -> None:
        assert problems(dataset) == []

    @pytest.mark.parametrize(("breakage", "expected"), BREAKAGES)
    def test_each_broken_rule_is_reported(
        self,
        dataset: DatasetBuilder,
        breakage: Callable[[DatasetBuilder], None],
        expected: str,
    ) -> None:
        breakage(dataset)
        found = problems(dataset)
        assert any(expected in line for line in found), found

    def test_all_problems_are_reported_at_once(self, dataset: DatasetBuilder) -> None:
        drop_cell(dataset)
        keys_not_published(dataset)
        assert len(problems(dataset)) >= 2

    @pytest.mark.parametrize(
        ("breakage", "expected"),
        [(drop_cell, "sat/2020"), (keys_not_published, "publishes_keys")],
    )
    def test_a_failed_validation_writes_nothing(
        self,
        db: psycopg.Connection,
        dataset: DatasetBuilder,
        breakage: Callable[[DatasetBuilder], None],
        expected: str,
    ) -> None:
        breakage(dataset)
        dataset.save()
        with pytest.raises(CatalogError, match=expected):
            run_catalog(db, dataset.catalog_path, dataset.ledger_path)
        for table in ("exams", "exam_editions", "sources", "artifacts"):
            assert count(db, table) == 0

    def test_index_absence_from_a_complete_listing_is_valid_evidence(
        self, dataset: DatasetBuilder
    ) -> None:
        page = dataset.add_source("https://sat.exams.test/archive", SAT, "index_page")
        dataset.edition(SAT)["evidence"] = [
            {"type": "index_absence", "source_id": page}
        ]
        assert problems(dataset) == []

    def test_blocked_and_practice_editions_are_valid(
        self, dataset: DatasetBuilder
    ) -> None:
        entry = dataset.edition(SAT)
        entry.update(
            {"status": "blocked", "status_reason": "robots", "kind": "practice"}
        )
        assert problems(dataset) == []

    def test_a_provincial_gaokao_paper_needs_no_schema_change(
        self, dataset: DatasetBuilder
    ) -> None:
        extra = dict(dataset.edition("gaokao-2020-main"))
        extra.update({"edition_id": "gaokao-2020-beijing", "sitting": "beijing"})
        dataset.catalog["editions"].append(extra)
        assert problems(dataset) == []

    def test_an_official_english_version_does_not_satisfy_the_paper_role(
        self, dataset: DatasetBuilder
    ) -> None:
        dataset.make_ingested(SAT, paper=None)
        english = dataset.add_source(
            "https://sat.exams.test/en.pdf", SAT, "english", fetched=b"%PDF english"
        )
        assert english
        assert any("no paper artifact" in line for line in problems(dataset))


def count(conn: psycopg.Connection, table: str) -> int:
    row = conn.execute(
        sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table))
    ).fetchone()
    assert row is not None
    return int(row[0])


# --- the catalog stage (US1, FR-002, FR-009, FR-019) -------------------------


class TestCatalogStage:
    def test_it_writes_the_exams_editions_and_sources(
        self, db: psycopg.Connection, dataset: DatasetBuilder
    ) -> None:
        summary = run_catalog(db, dataset.catalog_path, dataset.ledger_path)
        assert count(db, "exams") == 6
        assert count(db, "exam_editions") == 66
        assert count(db, "sources") == 6 + 66
        assert count(db, "artifacts") == 0
        assert summary.counts.inserted == 6 + 66 + 72

    def test_a_second_run_changes_nothing(
        self, db: psycopg.Connection, dataset: DatasetBuilder
    ) -> None:
        run_catalog(db, dataset.catalog_path, dataset.ledger_path)
        again = run_catalog(db, dataset.catalog_path, dataset.ledger_path)
        assert again.counts == Counts()
        assert "inserted=0 updated=0 deleted=0" in again.line()

    def test_ingesting_one_edition_writes_only_that_edition(
        self, db: psycopg.Connection, dataset: DatasetBuilder
    ) -> None:
        run_catalog(db, dataset.catalog_path, dataset.ledger_path)
        dataset.make_ingested(SAT)
        dataset.save()
        summary = run_catalog(db, dataset.catalog_path, dataset.ledger_path)
        assert summary.counts.updated == 1  # only the edition row
        assert summary.counts.inserted == 3 + 2  # three sources, two artifacts
        roles = db.execute(
            "SELECT role FROM artifacts WHERE edition_id = %s ORDER BY role", (SAT,)
        ).fetchall()
        assert roles == [("key",), ("paper",)]

    def test_blocking_an_edition_updates_one_row(
        self, db: psycopg.Connection, dataset: DatasetBuilder
    ) -> None:
        run_catalog(db, dataset.catalog_path, dataset.ledger_path)
        dataset.edition("csat-2018-main").update(
            {"status": "blocked", "status_reason": "login_required"}
        )
        dataset.save()
        assert run_catalog(
            db, dataset.catalog_path, dataset.ledger_path
        ).counts == Counts(updated=1)

    def test_the_latest_review_and_fetch_decide_the_source_state(
        self, db: psycopg.Connection, dataset: DatasetBuilder
    ) -> None:
        source = dataset.add_source(
            "https://sat.exams.test/x.pdf", SAT, "paper", fetched=b"%PDF one"
        )
        dataset.review(source, SAT, "paper", approve=False, reason="wrong file")
        dataset.save()
        run_catalog(db, dataset.catalog_path, dataset.ledger_path)
        row = db.execute(
            "SELECT decision, decision_reason, fetch_outcome "
            "FROM sources WHERE source_id = %s",
            (source,),
        ).fetchone()
        assert row == ("rejected", "wrong file", "fetched")
        assert count(db, "artifacts") == 0  # a rejected source yields no artifact

    def test_one_key_file_can_serve_two_editions(
        self, db: psycopg.Connection, dataset: DatasetBuilder
    ) -> None:
        key = dataset.add_source(
            "https://sat.exams.test/keys.pdf", SAT, "key", fetched=b"%PDF shared key"
        )
        dataset.review(key, "sat-2021-main", "key")
        dataset.save()
        run_catalog(db, dataset.catalog_path, dataset.ledger_path)
        rows = db.execute(
            "SELECT edition_id FROM artifacts WHERE role = 'key' ORDER BY edition_id"
        ).fetchall()
        assert rows == [(SAT,), ("sat-2021-main",)]
        assert db.execute(
            "SELECT count(DISTINCT sha256) FROM artifacts"
        ).fetchone() == (1,)

    def test_changed_content_at_the_same_url_keeps_both_files(
        self, db: psycopg.Connection, dataset: DatasetBuilder
    ) -> None:
        source = dataset.add_source(
            "https://sat.exams.test/p.pdf", SAT, "paper", fetched=b"%PDF version one"
        )
        dataset.fetch_event(source, b"%PDF version two", "application/pdf")
        dataset.save()
        run_catalog(db, dataset.catalog_path, dataset.ledger_path)
        assert count(db, "artifacts") == 2

    def test_an_official_english_version_adds_an_artifact_not_an_edition(
        self, db: psycopg.Connection, dataset: DatasetBuilder
    ) -> None:
        run_catalog(db, dataset.catalog_path, dataset.ledger_path)
        dataset.add_source(
            "https://sat.exams.test/en.pdf", SAT, "english", fetched=b"%PDF english"
        )
        dataset.save()
        run_catalog(db, dataset.catalog_path, dataset.ledger_path)
        assert count(db, "exam_editions") == 66
        assert db.execute("SELECT role FROM artifacts").fetchall() == [("english",)]

    def test_a_provincial_paper_is_just_another_edition(
        self, db: psycopg.Connection, dataset: DatasetBuilder
    ) -> None:
        extra = dict(dataset.edition("gaokao-2020-main"))
        extra.update({"edition_id": "gaokao-2020-beijing", "sitting": "beijing"})
        dataset.catalog["editions"].append(extra)
        dataset.save()
        run_catalog(db, dataset.catalog_path, dataset.ledger_path)
        assert count(db, "exam_editions") == 67
        assert db.execute(
            "SELECT count(DISTINCT (exam_id, year)) FROM exam_editions"
        ).fetchone() == (66,)

    def test_a_duplicate_ledger_seq_is_refused_before_anything_is_written(
        self, db: psycopg.Connection, dataset: DatasetBuilder
    ) -> None:
        lines = dataset.ledger_path.read_text(encoding="utf-8").splitlines()
        dataset.ledger_path.write_text(
            "\n".join([*lines, lines[-1]]) + "\n", encoding="utf-8"
        )
        with pytest.raises(LedgerError):
            run_catalog(db, dataset.catalog_path, dataset.ledger_path)
        assert count(db, "exams") == 0


# --- review (US1, FR-007) -----------------------------------------------------


def state_of(ds: DatasetBuilder, source_id: str):
    return replay_ledger(read_ledger(ds.ledger_path)).sources[source_id]


class TestReview:
    def candidate(self, ds: DatasetBuilder) -> str:
        return ds.add_source(
            "https://sat.exams.test/c.pdf", SAT, "paper", approve=False
        )

    def test_approve_records_edition_and_role(self, dataset: DatasetBuilder) -> None:
        source = self.candidate(dataset)
        assert review(
            dataset.ledger_path,
            source,
            approve=True,
            edition_id=SAT,
            role="key",
            published=None,
            reason=None,
            at=dataset.at(),
        )
        state = state_of(dataset, source)
        assert state.decision == "approved"
        assert state.approvals[SAT].role == "key"

    @pytest.mark.parametrize(
        ("approve", "edition_id", "role", "published", "reason", "message"),
        [
            (True, None, "paper", None, None, "edition"),
            (True, SAT, None, None, None, "role"),
            (True, SAT, "mystery", None, None, "role"),
            (True, SAT, "key_erratum", None, None, "published"),
            (True, SAT, "key", "01/02/2020", None, "ISO"),
            (False, None, None, None, None, "reason"),
        ],
    )
    def test_invalid_reviews_are_refused_and_write_nothing(
        self,
        dataset: DatasetBuilder,
        approve: bool,
        edition_id: str | None,
        role: str | None,
        published: str | None,
        reason: str | None,
        message: str,
    ) -> None:
        source = self.candidate(dataset)
        before = dataset.ledger_path.read_bytes()
        with pytest.raises(ValueError, match=message):
            review(
                dataset.ledger_path,
                source,
                approve=approve,
                edition_id=edition_id,
                role=role,
                published=published,
                reason=reason,
                at=dataset.at(),
            )
        assert dataset.ledger_path.read_bytes() == before

    def test_an_unknown_candidate_is_refused(self, dataset: DatasetBuilder) -> None:
        with pytest.raises(ValueError, match="unknown candidate"):
            review(
                dataset.ledger_path,
                "src_nope",
                approve=False,
                edition_id=None,
                role=None,
                published=None,
                reason="x",
                at=dataset.at(),
            )

    def test_the_latest_review_wins(self, dataset: DatasetBuilder) -> None:
        source = self.candidate(dataset)
        args = {"edition_id": SAT, "role": "paper", "published": None, "reason": None}
        review(dataset.ledger_path, source, approve=True, at=dataset.at(), **args)
        review(
            dataset.ledger_path,
            source,
            approve=False,
            edition_id=None,
            role=None,
            published=None,
            reason="changed my mind",
            at=dataset.at(),
        )
        assert state_of(dataset, source).decision == "rejected"
        review(dataset.ledger_path, source, approve=True, at=dataset.at(), **args)
        assert state_of(dataset, source).decision == "approved"

    def test_repeating_an_identical_review_appends_nothing(
        self, dataset: DatasetBuilder
    ) -> None:
        source = self.candidate(dataset)
        args = {"edition_id": SAT, "role": "paper", "published": None, "reason": None}
        assert review(
            dataset.ledger_path, source, approve=True, at=dataset.at(), **args
        )
        size = dataset.ledger_path.stat().st_size
        assert not review(
            dataset.ledger_path, source, approve=True, at=dataset.at(), **args
        )
        assert dataset.ledger_path.stat().st_size == size

    def test_approving_for_a_second_edition_is_a_new_review(
        self, dataset: DatasetBuilder
    ) -> None:
        source = self.candidate(dataset)
        for edition in (SAT, "sat-2021-main"):
            assert review(
                dataset.ledger_path,
                source,
                approve=True,
                edition_id=edition,
                role="key",
                published=None,
                reason=None,
                at=dataset.at(),
            )
        assert set(state_of(dataset, source).approvals) == {SAT, "sat-2021-main"}


# --- the committed ledger only grows (FR-009, contracts/manifests.md) --------


class TestCommittedLedger:
    def test_only_grows_since_the_merge_base(self) -> None:
        base = subprocess.run(
            ["git", "merge-base", "HEAD", "origin/main"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        if base.returncode != 0:
            if os.environ.get("CI") == "true":
                pytest.fail(
                    "origin/main is unreachable; the CI checkout must be full depth"
                )
            pytest.skip("origin/main is not available locally")
        shown = subprocess.run(
            ["git", "show", f"{base.stdout.strip()}:pipeline/ledger.jsonl"],
            cwd=REPO_ROOT,
            capture_output=True,
            check=False,
        )
        old = shown.stdout if shown.returncode == 0 else b""
        new = (REPO_ROOT / "pipeline" / "ledger.jsonl").read_bytes()
        assert ledger_is_prefix_extension(old, new)


# --- the committed registry ----------------------------------------------------


class TestCommittedCatalog:
    def test_it_loads_and_names_exactly_the_six_exams(self) -> None:
        catalog = load_catalog(REPO_ROOT / "pipeline" / "catalog.yaml")
        assert sorted(catalog.exams) == sorted(EXAM_IDS)
        assert catalog.translation.model

    def test_it_is_not_valid_yet_and_says_why(self) -> None:
        # The skeleton must stay refused until the maintainer authors real evidence:
        # a catalog that passed on invented data would be a fabricated result.
        catalog = load_catalog(REPO_ROOT / "pipeline" / "catalog.yaml")
        found = validate_catalog(
            catalog, replay_ledger(read_ledger(REPO_ROOT / "pipeline" / "ledger.jsonl"))
        )
        assert any("has no edition" in line for line in found)
        assert any("keys_evidence" in line for line in found)
