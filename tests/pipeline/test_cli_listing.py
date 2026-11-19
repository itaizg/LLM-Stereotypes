"""The read-only listing commands ``exams`` and ``candidates``.

They exist so a workflow can ask the pipeline which exams there are and which
candidates still wait for a review, instead of re-deriving either (spec 002,
contracts/pipeline-cli-additions.md P1 and P2). Both must stay read-only: no
database connection, no write lock.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

import pytest

import pipeline.__main__ as cli
from pipeline import acquire
from pipeline.manifests import LedgerError, LedgerEvent, load_catalog, source_id_for

if TYPE_CHECKING:
    from conftest import DatasetBuilder

Run = Callable[..., tuple[int, str, str]]


def _no_connection(*_args: object, **_kwargs: object) -> NoReturn:
    raise AssertionError("a listing command must not open a database connection")


@pytest.fixture
def run(
    dataset: DatasetBuilder,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> Run:
    """Run one command against the synthetic dataset, with the database unreachable."""
    monkeypatch.delenv("PIPELINE_DSN", raising=False)
    monkeypatch.setattr(cli, "connect", _no_connection)

    def go(*argv: str) -> tuple[int, str, str]:
        code = cli.main(
            [
                *argv,
                "--catalog",
                str(dataset.catalog_path),
                "--ledger",
                str(dataset.ledger_path),
            ]
        )
        out = capsys.readouterr()
        return code, out.out, out.err

    return go


class TestExams:
    def test_prints_sorted_exam_ids_then_a_count(
        self, run: Run, dataset: DatasetBuilder
    ) -> None:
        code, out, _ = run("exams")

        expected = sorted(dataset.catalog["exams"])
        assert code == 0
        assert out.splitlines() == [*expected, f"exams: count={len(expected)}"]

    def test_takes_no_lock_and_opens_no_connection(self, run: Run) -> None:
        # The fixture makes connect() raise and unsets PIPELINE_DSN; reaching the
        # end with exit 0 shows neither was used.
        assert run("exams")[0] == 0

    @pytest.mark.parametrize(
        "flag", [["--exam", "csat"], ["--edition", "csat-2020-main"]]
    )
    def test_exam_and_edition_filters_are_a_usage_error(
        self, run: Run, flag: list[str]
    ) -> None:
        code, out, err = run("exams", *flag)

        assert code == 2
        assert out == ""
        assert err.startswith("usage:")
        assert "not implemented" not in err

    def test_an_unloadable_catalog_exits_3(
        self, dataset: DatasetBuilder, run: Run
    ) -> None:
        dataset.catalog_path.write_text("version: [not, an, int\n", encoding="utf-8")

        assert run("exams")[0] == 3


def _pending(dataset: DatasetBuilder, url: str, edition: str = "sat-2020-main") -> str:
    """Add a candidate that nobody has reviewed yet; return its source id."""
    return dataset.add_source(url, edition, "paper", approve=False)


class TestCandidates:
    def test_lists_every_pending_source_sorted_by_exam_then_id(
        self, run: Run, dataset: DatasetBuilder
    ) -> None:
        sat = [_pending(dataset, f"https://sat.exams.test/p/{n}") for n in range(3)]
        csat = _pending(dataset, "https://csat.exams.test/p/1", "csat-2020-main")

        code, out, _ = run("candidates")

        assert code == 0
        assert out.splitlines() == [
            *(f"candidate {sid} exam=csat domain=csat.exams.test" for sid in [csat]),
            *(f"candidate {sid} exam=sat domain=sat.exams.test" for sid in sorted(sat)),
            "candidates: pending=4",
        ]

    def test_reviewed_sources_are_not_listed(
        self, run: Run, dataset: DatasetBuilder
    ) -> None:
        waiting = _pending(dataset, "https://sat.exams.test/p/waiting")
        approved = dataset.add_source(
            "https://sat.exams.test/p/approved", "sat-2020-main", "paper"
        )
        rejected = dataset.add_source(
            "https://sat.exams.test/p/rejected", "sat-2020-main", "paper", approve=False
        )
        dataset.review(rejected, "sat-2020-main", "paper", approve=False, reason="dup")

        _, out, _ = run("candidates")

        assert waiting in out
        assert approved not in out
        assert rejected not in out
        assert out.splitlines()[-1] == "candidates: pending=1"

    def test_a_domain_listed_by_two_exams_appears_under_both(
        self, run: Run, dataset: DatasetBuilder
    ) -> None:
        dataset.catalog["exams"]["csat"]["domains"].append("sat.exams.test")
        dataset.save()
        shared = _pending(dataset, "https://sat.exams.test/p/shared")

        _, out, _ = run("candidates")

        assert out.splitlines() == [
            f"candidate {shared} exam=csat domain=sat.exams.test",
            f"candidate {shared} exam=sat domain=sat.exams.test",
            "candidates: pending=2",
        ]

    def test_a_pending_candidate_whose_domain_left_the_catalog_is_not_dropped(
        self, run: Run, dataset: DatasetBuilder
    ) -> None:
        orphan = _pending(dataset, "https://sat.exams.test/p/orphan")
        kept = _pending(dataset, "https://csat.exams.test/p/1", "csat-2020-main")
        dataset.catalog["exams"]["sat"]["domains"].remove("sat.exams.test")
        dataset.save()

        _, out, _ = run("candidates")
        _, filtered, _ = run("candidates", "--exam", "csat")

        assert out.splitlines() == [
            f"candidate {orphan} exam=- domain=sat.exams.test",
            f"candidate {kept} exam=csat domain=csat.exams.test",
            "candidates: pending=2",
        ]
        assert orphan not in filtered

    def test_a_ledger_caught_mid_append_is_read_again(
        self, dataset: DatasetBuilder, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sid = _pending(dataset, "https://sat.exams.test/p/1")
        real = acquire.read_ledger
        calls: list[int] = []

        def torn_once(path: Path) -> list[LedgerEvent]:
            calls.append(1)
            if len(calls) == 1:
                raise LedgerError("half a line")
            return real(path)

        monkeypatch.setattr(acquire, "read_ledger", torn_once)
        pauses: list[float] = []

        found = acquire.pending_candidates(
            load_catalog(dataset.catalog_path),
            dataset.ledger_path,
            sleep=pauses.append,
        )

        assert found == [("sat", sid, "sat.exams.test")]
        assert len(pauses) == 1

    def test_a_ledger_that_stays_bad_still_raises(
        self, dataset: DatasetBuilder, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def always_bad(path: Path) -> list[LedgerEvent]:
            raise LedgerError("corrupt")

        monkeypatch.setattr(acquire, "read_ledger", always_bad)

        with pytest.raises(LedgerError):
            acquire.pending_candidates(
                load_catalog(dataset.catalog_path),
                dataset.ledger_path,
                sleep=lambda _s: None,
            )

    def test_exam_filter_keeps_only_that_exam(
        self, run: Run, dataset: DatasetBuilder
    ) -> None:
        _pending(dataset, "https://sat.exams.test/p/1")
        kept = _pending(dataset, "https://csat.exams.test/p/1", "csat-2020-main")

        _, out, _ = run("candidates", "--exam", "csat")

        assert out.splitlines() == [
            f"candidate {kept} exam=csat domain=csat.exams.test",
            "candidates: pending=1",
        ]

    def test_an_exam_filter_with_nothing_pending_still_succeeds(self, run: Run) -> None:
        code, out, _ = run("candidates", "--exam", "enem")

        assert code == 0
        assert out == "candidates: pending=0\n"

    def test_an_unknown_exam_is_a_usage_error(self, run: Run) -> None:
        code, out, err = run("candidates", "--exam", "nope")

        assert code == 2
        assert out == ""
        assert "nope" in err

    def test_output_carries_no_url_path_and_no_page_text(
        self, run: Run, dataset: DatasetBuilder
    ) -> None:
        url = "https://sat.exams.test/secret/path/question-text.pdf"
        _pending(dataset, url)

        _, out, _ = run("candidates")

        assert source_id_for(url) in out
        assert "secret" not in out
        assert "question-text" not in out
        assert "https://" not in out
