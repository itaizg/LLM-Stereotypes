"""The decisions file and its import (spec 003 FR-026, contracts/decisions-file.md)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from pipeline.decisions import (
    MAX_FILE_BYTES,
    DecisionsError,
    decision_id_for,
    load_decisions,
    parse_decisions,
)

if TYPE_CHECKING:
    from conftest import DatasetBuilder, SeedEdition

RUN_ID = "cr_20261123T101500_a3f9"
P1 = "prop_1f3c9a0b2d4e5f60"
P2 = "prop_77ab000000000000"


def approve(**changes: object) -> dict[str, object]:
    decision: dict[str, object] = {
        "decision_id": decision_id_for(RUN_ID, "sha256:abc", [1, 2]),
        "content_hash": "sha256:abc",
        "members": [
            {"proposal_id": P1, "revision": 1, "primary": True, "origin": "official"},
            {
                "proposal_id": P2,
                "revision": 2,
                "primary": False,
                "origin": "third_party",
            },
        ],
        "decision": "approve",
        "exam": {"key": "enem", "new": False},
        "new_exam": None,
        "edition_id": "enem-2024-d1",
        "role": "paper",
        "published": None,
        "reason": None,
    }
    decision.update(changes)
    return decision


def document(*decisions: dict[str, object], **changes: object) -> bytes:
    body: dict[str, object] = {
        "format": "crawler-decisions/1",
        "run_id": RUN_ID,
        "decided_at": "2026-11-24T09:12:00Z",
        "decisions": list(decisions),
    }
    body.update(changes)
    return json.dumps(body).encode()


class TestModel:
    def test_model_accepts_the_contract_example(self) -> None:
        parsed = parse_decisions(document(approve()))
        assert parsed.decisions[0].members[1].origin == "third_party"

    def test_model_accepts_a_reject_with_a_reason(self) -> None:
        reject = approve(
            decision="reject", exam=None, edition_id=None, role=None, reason="mirror"
        )
        assert parse_decisions(document(reject)).decisions[0].reason == "mirror"

    @pytest.mark.parametrize(
        "bad",
        [
            approve(extra="x"),
            approve(edition_id=None),
            approve(role=None),
            approve(role="lunch"),
            approve(exam=None),
            approve(role="key_erratum"),
            approve(decision="reject", reason=None),
            approve(exam={"key": "Bad-Key", "new": False}),
            approve(exam={"key": "newexam", "new": True}),
            approve(new_exam={"name": "X", "body": "Y", "language": "pt"}),
            approve(decision_id="dec_x"),
            approve(
                members=[
                    {
                        "proposal_id": P1,
                        "revision": 1,
                        "primary": False,
                        "origin": "official",
                    }
                ]
            ),
            approve(
                members=[
                    {
                        "proposal_id": P1,
                        "revision": 1,
                        "primary": True,
                        "origin": "official",
                    },
                    {
                        "proposal_id": P1,
                        "revision": 1,
                        "primary": False,
                        "origin": "official",
                    },
                ]
            ),
        ],
    )
    def test_model_refuses_a_bad_decision(self, bad: dict[str, object]) -> None:
        with pytest.raises(DecisionsError):
            parse_decisions(document(bad))

    def test_model_refuses_a_bad_file(self) -> None:
        with pytest.raises(DecisionsError, match="format"):
            parse_decisions(document(approve(), format="crawler-decisions/2"))
        with pytest.raises(DecisionsError, match="JSON"):
            parse_decisions(b"{not json")
        with pytest.raises(DecisionsError, match="once"):
            parse_decisions(document(approve(), approve()))
        with pytest.raises(DecisionsError, match="bytes"):
            parse_decisions(b" " * (MAX_FILE_BYTES + 1))

    def test_model_new_exam_needs_its_details(self) -> None:
        new = approve(
            exam={"key": "vestibular_x", "new": True},
            new_exam={
                "name": "Vestibular X",
                "body": "Universidade X",
                "language": "pt",
            },
        )
        assert parse_decisions(document(new)).decisions[0].new_exam is not None

    def test_model_file_size_is_checked_before_reading(self, tmp_path: Path) -> None:
        path = tmp_path / "decisions.json"
        path.write_bytes(b" " * (MAX_FILE_BYTES + 1))
        with pytest.raises(DecisionsError, match="bytes"):
            load_decisions(path)
        with pytest.raises(DecisionsError, match="cannot read"):
            load_decisions(tmp_path / "missing.json")

    def test_model_decision_ids_name_the_group_shown(self) -> None:
        first = decision_id_for(RUN_ID, "sha256:abc", [1, 2])
        assert first == decision_id_for(RUN_ID, "sha256:abc", [1, 2])
        assert first != decision_id_for(RUN_ID, "sha256:abc", [1, 3])
        assert first.startswith("dec_") and len(first) == 20


# --- the import (T054-T056) -----------------------------------------------------------

import threading  # noqa: E402

import psycopg  # noqa: E402
from psycopg.types.json import Jsonb  # noqa: E402

from pipeline import __main__ as pipeline_cli  # noqa: E402
from pipeline.decisions import (  # noqa: E402
    ImportSummary,
    import_decisions,
    origin_counts,
    suspended_sources,
)
from pipeline.manifests import (  # noqa: E402
    LedgerEvent,
    catalog_view,
    load_catalog,
    read_ledger,
    replay_ledger,
    source_id_for,
)

OFFICIAL_URL = "https://sat.exams.test/practice.pdf"
MIRROR_URL = "https://mirror.example/practice.pdf"


def pid(url: str) -> str:
    return "prop_" + hashlib.sha256(url.encode()).hexdigest()[:16]


def seed(
    conn: psycopg.Connection,
    url: str,
    *,
    revision: int = 1,
    digest: str = "sha256:abc",
    status: str = "passed",
    key: str = "linked_file",
    review_state: str = "unreviewed",
    flags: tuple[str, ...] = (),
) -> str:
    conn.execute(
        "INSERT INTO crawler_runs (run_id, config, approved_by, approved_at, status, "
        "started_at) VALUES (%s, '{}', 't', now(), 'completed', now()) "
        "ON CONFLICT DO NOTHING",
        (RUN_ID,),
    )
    if revision > 1:
        conn.execute(
            "UPDATE crawler_proposals SET review_state = 'stale_superseded' "
            "WHERE proposal_id = %s AND revision < %s AND review_state = 'unreviewed'",
            (pid(url), revision),
        )
    conn.execute(
        "INSERT INTO crawler_proposals (proposal_id, revision, normalised_url, url, "
        "domain, "
        "run_id, content_hash, labels, checks, validation_status, reason, "
        "reason_class, "
        "material_fingerprint, review_state, observed_at) VALUES (%s, %s, %s, %s, %s, "
        "%s, "
        "%s, %s, %s, %s, %s, %s, 'f', %s, now())",
        (
            pid(url),
            revision,
            url,
            url,
            url.split("/")[2],
            RUN_ID,
            digest,
            Jsonb({"origin": "official", "key_status": key, "flags": list(flags)}),
            Jsonb({"key_located": {"result": "passed", "evidence": url + ".key"}}),
            status,
            None if status == "passed" else "not_mcq_structure",
            None if status == "passed" else "permanent",
            review_state,
        ),
    )
    return pid(url)


def members(*items: tuple[str, int, str]) -> list[dict[str, object]]:
    return [
        {"proposal_id": pid(url), "revision": rev, "primary": n == 0, "origin": origin}
        for n, (url, rev, origin) in enumerate(items)
    ]


def group(
    *items: tuple[str, int, str], digest: str = "sha256:abc", **changes: object
) -> dict[str, object]:
    revisions = [rev for _, rev, _ in items]
    fields: dict[str, object] = {
        "decision_id": decision_id_for(RUN_ID, digest, revisions),
        "content_hash": digest,
        "members": members(*items),
        "exam": {"key": "sat", "new": False},
        "edition_id": "sat-2024-main",
    }
    fields.update(changes)
    return approve(**fields)


def do_import(
    conn: psycopg.Connection, ds: DatasetBuilder, *decisions: dict[str, object]
) -> ImportSummary:
    file = parse_decisions(document(*decisions)) if decisions else None
    return import_decisions(
        conn,
        ds.ledger_path,
        load_catalog(ds.catalog_path),
        file,
        "2026-11-24T00:00:00Z",
    )


def events(ds: DatasetBuilder, kind: str | None = None) -> list[LedgerEvent]:
    found = read_ledger(ds.ledger_path)
    return [e for e in found if kind is None or e.kind == kind]


class TestImport:
    def test_import_approves_and_allowlists_third_party_mirrors(
        self, db: psycopg.Connection, dataset: DatasetBuilder
    ) -> None:
        seed(db, OFFICIAL_URL)
        seed(db, MIRROR_URL)
        summary = do_import(
            db,
            dataset,
            group((OFFICIAL_URL, 1, "official"), (MIRROR_URL, 1, "third_party")),
        )
        assert summary.approved == 1
        state = replay_ledger(read_ledger(dataset.ledger_path))
        mirror = state.sources[source_id_for(MIRROR_URL)]
        assert (mirror.decision, mirror.origin, mirror.method) == (
            "approved",
            "third_party",
            "crawler",
        )
        assert state.allowlisted == {"sat": frozenset({"mirror.example"})}
        candidate = next(
            e for e in events(dataset, "candidate") if e.fields["url"] == MIRROR_URL
        )
        assert candidate.fields["evidence"] == [MIRROR_URL, MIRROR_URL + ".key"]
        states = db.execute("SELECT review_state FROM crawler_proposals").fetchall()
        assert states == [("approved",), ("approved",)]

    def test_import_twice_changes_nothing(
        self, db: psycopg.Connection, dataset: DatasetBuilder
    ) -> None:
        seed(db, OFFICIAL_URL)
        decision = group((OFFICIAL_URL, 1, "official"))
        do_import(db, dataset, decision)
        before = dataset.ledger_path.read_bytes()
        summary = do_import(db, dataset, decision)
        assert summary.unchanged == 1
        assert dataset.ledger_path.read_bytes() == before

    def test_import_a_changed_decision_adds_one_review(
        self, db: psycopg.Connection, dataset: DatasetBuilder
    ) -> None:
        seed(db, OFFICIAL_URL)
        do_import(db, dataset, group((OFFICIAL_URL, 1, "official")))
        count = len(events(dataset))
        reject = group(
            (OFFICIAL_URL, 1, "official"),
            decision="reject",
            exam=None,
            edition_id=None,
            role=None,
            reason="wrong paper",
        )
        assert do_import(db, dataset, reject).rejected == 1
        assert [e.kind for e in events(dataset)[count:]] == ["review"]
        source = replay_ledger(read_ledger(dataset.ledger_path)).sources[
            source_id_for(OFFICIAL_URL)
        ]
        assert (source.decision, source.decision_reason) == ("rejected", "wrong paper")

    def test_import_a_superseded_revision_is_stale(
        self, db: psycopg.Connection, dataset: DatasetBuilder
    ) -> None:
        """Story 3, scenario 1: decided on revision 2 while 3 is current."""
        seed(db, OFFICIAL_URL)
        seed(db, OFFICIAL_URL, revision=2, digest="sha256:abc")
        seed(db, OFFICIAL_URL, revision=3, digest="sha256:abc")
        before = len(events(dataset))
        summary = do_import(db, dataset, group((OFFICIAL_URL, 2, "official")))
        assert summary.stale == 1
        assert len(events(dataset)) == before

    def test_import_adds_a_new_exam_once(
        self, db: psycopg.Connection, dataset: DatasetBuilder
    ) -> None:
        url = "https://vest.example/prova.pdf"
        other = "https://vest.example/prova2.pdf"
        seed(db, url)
        seed(db, other, digest="sha256:def")
        new_exam = {"name": "Vestibular X", "body": "Universidade X", "language": "pt"}
        first = group(
            (url, 1, "official"),
            exam={"key": "vestibular_x", "new": True},
            new_exam=new_exam,
            edition_id="vestibular_x-2024-main",
        )
        do_import(db, dataset, first)
        second = group(
            (other, 1, "official"),
            digest="sha256:def",
            exam={"key": "vestibular_x", "new": False},
            edition_id="vestibular_x-2024-main",
        )
        do_import(db, dataset, second)
        assert len(events(dataset, "exam_added")) == 1
        view = catalog_view(
            load_catalog(dataset.catalog_path),
            replay_ledger(read_ledger(dataset.ledger_path)),
        )
        exam = view.exams["vestibular_x"]
        assert (exam.name, exam.domains, exam.added_by) == (
            "Vestibular X",
            ("vest.example",),
            first["decision_id"],
        )

    @pytest.mark.parametrize(
        "problem", ["failed", "superseded_hash", "no_key", "existing_new", "unknown"]
    )
    def test_import_refuses_rule_breaks_before_writing(
        self, db: psycopg.Connection, dataset: DatasetBuilder, problem: str
    ) -> None:
        url = "https://vest.example/prova.pdf"
        seed(
            db,
            url,
            status="failed" if problem == "failed" else "passed",
            key="not_found" if problem == "no_key" else "linked_file",
        )
        new_exam = {"name": "V", "body": "B", "language": "pt"}
        decision = {
            "failed": group((url, 1, "official")),
            "superseded_hash": group((url, 1, "official"), digest="sha256:other"),
            "no_key": group(
                (url, 1, "official"), exam={"key": "vx", "new": True}, new_exam=new_exam
            ),
            "existing_new": group(
                (url, 1, "official"),
                exam={"key": "sat", "new": True},
                new_exam=new_exam,
            ),
            "unknown": group((url, 1, "official"), exam={"key": "vx", "new": False}),
        }[problem]
        before = len(events(dataset))
        with pytest.raises(DecisionsError):
            do_import(db, dataset, decision)
        assert len(events(dataset)) == before

    def test_import_concurrent_crawler_write_serialises_with_the_import(
        self, db: psycopg.Connection, dataset: DatasetBuilder, schema_dsn: str
    ) -> None:
        """Either order is safe: a lock held by a crawler write makes the import wait,
        and once the new revision commits, the old decision is stale."""
        seed(db, OFFICIAL_URL)
        with psycopg.connect(schema_dsn) as crawler:
            crawler.execute(
                "SELECT 1 FROM crawler_proposals WHERE proposal_id = %s FOR UPDATE",
                (pid(OFFICIAL_URL),),
            )
            crawler.execute(
                "UPDATE crawler_proposals SET review_state = 'stale_superseded' "
                "WHERE proposal_id = %s",
                (pid(OFFICIAL_URL),),
            )
            crawler.execute(
                "INSERT INTO crawler_proposals SELECT proposal_id, 2, normalised_url, "
                "url, "
                "domain, run_id, content_hash, labels, checks, validation_status, "
                "reason, "
                "reason_class, 'g', 'unreviewed', observed_at, created_at "
                "FROM crawler_proposals WHERE proposal_id = %s AND revision = 1",
                (pid(OFFICIAL_URL),),
            )
            outcome: list[ImportSummary] = []
            worker = threading.Thread(
                target=lambda: outcome.append(
                    do_import(db, dataset, group((OFFICIAL_URL, 1, "official")))
                )
            )
            worker.start()
            worker.join(timeout=0.5)
            assert worker.is_alive()  # waiting on the crawler's row lock
            crawler.commit()
        worker.join(timeout=10)
        assert outcome and outcome[0].stale == 1

    def test_import_a_crash_before_the_review_is_completed_by_a_rerun(
        self,
        db: psycopg.Connection,
        dataset: DatasetBuilder,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        seed(db, MIRROR_URL)
        decision = group((MIRROR_URL, 1, "third_party"))
        import pipeline.decisions as module

        real = module.append_events

        def torn(
            path: Path, block: list[tuple[str, dict[str, object]]], *, at: str
        ) -> None:
            real(path, block[:-1], at=at)  # everything but the closing review
            raise OSError("disk full")

        monkeypatch.setattr(module, "append_events", torn)
        with pytest.raises(OSError):
            do_import(db, dataset, decision)
        state = replay_ledger(read_ledger(dataset.ledger_path))
        assert (
            source_id_for(MIRROR_URL) not in state.sources and state.allowlisted == {}
        )
        monkeypatch.setattr(module, "append_events", real)
        assert do_import(db, dataset, decision).approved == 1
        state = replay_ledger(read_ledger(dataset.ledger_path))
        assert state.sources[source_id_for(MIRROR_URL)].decision == "approved"


class TestReopen:
    def approved_then_changed(
        self, db: psycopg.Connection, dataset: DatasetBuilder
    ) -> None:
        seed(db, MIRROR_URL)
        do_import(db, dataset, group((MIRROR_URL, 1, "third_party")))
        seed(
            db, MIRROR_URL, revision=2, key="inline", flags=("reopened_after_approval",)
        )

    def test_reopen_sweep_runs_once_per_revision(
        self, db: psycopg.Connection, dataset: DatasetBuilder
    ) -> None:
        self.approved_then_changed(db, dataset)
        assert do_import(db, dataset).reopened == 1
        assert do_import(db, dataset).reopened == 0
        [event] = events(dataset, "review_reopened")
        assert event.fields["changed"] == ["key"]
        source = replay_ledger(read_ledger(dataset.ledger_path)).sources[
            source_id_for(MIRROR_URL)
        ]
        assert source.decision == "pending"

    def test_reopen_suspends_fetch_until_reviewed(
        self, db: psycopg.Connection, dataset: DatasetBuilder
    ) -> None:
        self.approved_then_changed(db, dataset)
        state = replay_ledger(read_ledger(dataset.ledger_path))
        assert suspended_sources(db, state) == {source_id_for(MIRROR_URL)}
        do_import(db, dataset, group((MIRROR_URL, 2, "third_party")))
        state = replay_ledger(read_ledger(dataset.ledger_path))
        assert suspended_sources(db, state) == frozenset()

    def test_reopen_only_cli(
        self,
        db: psycopg.Connection,
        dataset: DatasetBuilder,
        schema_dsn: str,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        self.approved_then_changed(db, dataset)
        monkeypatch.setenv("PIPELINE_DSN", schema_dsn)
        args = [
            "--catalog",
            str(dataset.catalog_path),
            "--ledger",
            str(dataset.ledger_path),
        ]
        assert pipeline_cli.main(["import-decisions", "--reopen-only", *args]) == 0
        assert "reopened=1" in capsys.readouterr().out
        assert pipeline_cli.main(["import-decisions", *args]) == 2


class TestImportCli:
    def test_cli_exit_codes(
        self,
        db: psycopg.Connection,
        dataset: DatasetBuilder,
        schema_dsn: str,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        seed(db, OFFICIAL_URL)
        monkeypatch.setenv("PIPELINE_DSN", schema_dsn)
        args = [
            "--catalog",
            str(dataset.catalog_path),
            "--ledger",
            str(dataset.ledger_path),
        ]
        good = tmp_path / "decisions.json"
        good.write_bytes(document(group((OFFICIAL_URL, 1, "official"))))
        assert pipeline_cli.main(["import-decisions", str(good), *args]) == 0
        out = capsys.readouterr().out
        assert out.splitlines()[0].endswith(" approved") and "approved=1" in out
        bad = tmp_path / "bad.json"
        bad.write_text("{}", encoding="utf-8")
        assert pipeline_cli.main(["import-decisions", str(bad), *args]) == 2
        with psycopg.connect(schema_dsn, autocommit=True) as other:
            from pipeline.store import write_lock

            with write_lock(other):
                assert pipeline_cli.main(["import-decisions", str(good), *args]) == 4


class TestOrigins:
    def test_origin_counts_follow_the_source(
        self, db: psycopg.Connection, dataset: DatasetBuilder, seed_edition: SeedEdition
    ) -> None:
        seed(db, MIRROR_URL)
        do_import(db, dataset, group((MIRROR_URL, 1, "third_party")))
        state = replay_ledger(read_ledger(dataset.ledger_path))
        catalog = load_catalog(dataset.catalog_path)
        edition = seed_edition(db)
        sid = source_id_for(MIRROR_URL)
        db.execute(
            "INSERT INTO sources (source_id, url, domain, discovery_method, "
            "edition_id, "
            "decision) VALUES (%s, %s, 'mirror.example', 'crawler', %s, 'approved')",
            (sid, MIRROR_URL, edition),
        )
        db.execute(
            "INSERT INTO artifacts VALUES ('h1', %s, %s, 'paper', 'application/pdf', "
            "NULL, 1)",
            (edition, sid),
        )
        db.execute(
            "INSERT INTO questions (question_id, edition_id, canonical_question_id, "
            "section, "
            "item, print_order, stem, choices, has_figure, has_table, has_audio, "
            "is_free_response, source_url, artifact_sha256, page, pipeline_version, "
            "content_hash) VALUES ('q1', %s, 'c1', 's', '1', 1, 'synthetic', '[]', "
            "false, "
            "false, false, false, %s, 'h1', 1, 'v', 'x')",
            (edition, MIRROR_URL),
        )
        counts = origin_counts(db, state, catalog)
        exam = edition.split("-", 1)[0]
        assert counts[exam]["third_party"] == {"questions": 1, "keys": 0}
