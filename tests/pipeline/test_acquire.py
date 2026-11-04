"""Tests for acquisition: blob storage, the store, catalog, discover, review, fetch."""

from __future__ import annotations

import hashlib
import io
import os
import uuid
from collections.abc import Callable
from pathlib import Path

import psycopg
import pytest
from botocore.exceptions import ClientError
from botocore.stub import Stubber
from psycopg import sql
from psycopg.conninfo import make_conninfo

from pipeline import __main__ as cli
from pipeline.acquire import (
    CatalogError,
    LedgerError,
    append_event,
    ledger_is_prefix_extension,
    read_ledger,
    source_id_for,
)
from pipeline.blobs import (
    BlobCorruptionError,
    BlobExistsError,
    LocalBlobStore,
    S3BlobStore,
)
from pipeline.store import (
    ConnectionFailed,
    Counts,
    LockHeld,
    StageSummary,
    apply_schema,
    connect,
    insert_if_new,
    reconcile,
    redact,
    write_lock,
)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class TestBlobStore:
    def test_put_returns_the_hash_and_stores_under_raw(
        self, blob_store: LocalBlobStore, tmp_path: Path
    ) -> None:
        digest = blob_store.put(b"synthetic paper bytes")
        assert digest == sha(b"synthetic paper bytes")
        assert (
            tmp_path / "blobs" / "raw" / digest
        ).read_bytes() == b"synthetic paper bytes"
        assert blob_store.get(digest) == b"synthetic paper bytes"
        assert blob_store.exists(digest)

    def test_second_put_of_the_same_bytes_is_a_no_op(
        self, blob_store: LocalBlobStore, tmp_path: Path
    ) -> None:
        digest = blob_store.put(b"same")
        stat_before = (tmp_path / "blobs" / "raw" / digest).stat()
        assert blob_store.put(b"same") == digest
        stat_after = (tmp_path / "blobs" / "raw" / digest).stat()
        assert stat_before.st_ino == stat_after.st_ino
        assert stat_before.st_mtime_ns == stat_after.st_mtime_ns

    def test_existing_file_with_the_wrong_bytes_is_corruption(
        self, blob_store: LocalBlobStore, tmp_path: Path
    ) -> None:
        digest = sha(b"original")
        target = tmp_path / "blobs" / "raw" / digest
        target.parent.mkdir(parents=True)
        target.write_bytes(b"tampered")
        with pytest.raises(BlobCorruptionError):
            blob_store.put(b"original")
        with pytest.raises(BlobCorruptionError):
            blob_store.get(digest)

    def test_a_failed_publish_leaves_no_file_at_the_final_name(
        self,
        blob_store: LocalBlobStore,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def crash(src: object, dst: object) -> None:
            raise OSError("simulated crash before the file is published")

        monkeypatch.setattr(os, "link", crash)
        with pytest.raises(OSError, match="simulated crash"):
            blob_store.put(b"never published")
        assert not blob_store.exists(sha(b"never published"))
        raw = tmp_path / "blobs" / "raw"
        assert [p.name for p in raw.iterdir() if not p.name.startswith(".tmp-")] == []

    def test_put_named_refuses_to_overwrite(self, blob_store: LocalBlobStore) -> None:
        blob_store.put_named("snapshots/snap-0001/manifest.json", b"first")
        with pytest.raises(BlobExistsError):
            blob_store.put_named("snapshots/snap-0001/manifest.json", b"second")
        assert blob_store.get_named("snapshots/snap-0001/manifest.json") == b"first"

    def test_put_named_can_overwrite_when_asked(
        self, blob_store: LocalBlobStore
    ) -> None:
        blob_store.put_named("snapshots/snap-0001/part.parquet", b"partial")
        blob_store.put_named(
            "snapshots/snap-0001/part.parquet", b"complete", overwrite=True
        )
        assert blob_store.get_named("snapshots/snap-0001/part.parquet") == b"complete"
        assert blob_store.exists_named("snapshots/snap-0001/part.parquet")
        assert not blob_store.exists_named("snapshots/snap-0002/part.parquet")

    @pytest.mark.parametrize("name", ["/etc/passwd", "../outside", "a/../../b", ""])
    def test_unsafe_names_are_refused(
        self, blob_store: LocalBlobStore, name: str
    ) -> None:
        with pytest.raises(ValueError, match="unsafe"):
            blob_store.put_named(name, b"x")

    def test_non_hash_keys_are_refused(self, blob_store: LocalBlobStore) -> None:
        with pytest.raises(ValueError, match="SHA-256"):
            blob_store.exists("../../etc/passwd")


class TestS3BlobStore:
    """Driven by botocore's Stubber: offline, no AWS account (FR-027)."""

    @staticmethod
    def make() -> tuple[S3BlobStore, Stubber]:
        import boto3

        client = boto3.client(
            "s3",
            region_name="us-east-1",
            aws_access_key_id="test",
            aws_secret_access_key="test",
        )
        return S3BlobStore(client, "bucket", "prefix"), Stubber(client)

    def test_put_sends_a_conditional_write(self) -> None:
        store, stubber = self.make()
        data = b"synthetic"
        stubber.add_response(
            "put_object",
            {},
            {
                "Bucket": "bucket",
                "Key": f"prefix/raw/{sha(data)}",
                "Body": data,
                "IfNoneMatch": "*",
            },
        )
        with stubber:
            assert store.put(data) == sha(data)
        stubber.assert_no_pending_responses()

    def test_a_412_means_it_exists_and_the_hash_is_rechecked(self) -> None:
        store, stubber = self.make()
        data = b"synthetic"
        key = f"prefix/raw/{sha(data)}"
        stubber.add_client_error(
            "put_object", service_error_code="PreconditionFailed", http_status_code=412
        )
        stubber.add_response(
            "get_object",
            {"Body": _body(data)},
            {"Bucket": "bucket", "Key": key},
        )
        with stubber:
            assert store.put(data) == sha(data)
        stubber.assert_no_pending_responses()

    def test_a_412_over_wrong_bytes_is_corruption(self) -> None:
        store, stubber = self.make()
        data = b"synthetic"
        stubber.add_client_error(
            "put_object", service_error_code="PreconditionFailed", http_status_code=412
        )
        stubber.add_response(
            "get_object",
            {"Body": _body(b"tampered")},
            {"Bucket": "bucket", "Key": f"prefix/raw/{sha(data)}"},
        )
        with stubber, pytest.raises(BlobCorruptionError):
            store.put(data)

    def test_put_named_412_raises_exists(self) -> None:
        store, stubber = self.make()
        stubber.add_client_error(
            "put_object", service_error_code="PreconditionFailed", http_status_code=412
        )
        with stubber, pytest.raises(BlobExistsError):
            store.put_named("snapshots/snap-0001/manifest.json", b"x")

    def test_other_errors_propagate(self) -> None:
        store, stubber = self.make()
        stubber.add_client_error(
            "put_object", service_error_code="AccessDenied", http_status_code=403
        )
        with stubber, pytest.raises(ClientError):
            store.put(b"x")


def _body(data: bytes) -> object:
    from botocore.response import StreamingBody

    return StreamingBody(io.BytesIO(data), len(data))


# --- the store ---------------------------------------------------------------

EXAM_ROW = {
    "exam_id": "sat",
    "name": "Synthetic SAT",
    "body": "Synthetic Board",
    "language": "en",
    "domains": ["sat.exams.test"],
    "publishes_keys": True,
}


def _count(conn: psycopg.Connection, table: str) -> int:
    row = conn.execute(
        sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table))
    ).fetchone()
    assert row is not None
    return int(row[0])


class TestStore:
    def test_apply_schema_is_idempotent(self, test_dsn: str) -> None:
        schema = f"t_{uuid.uuid4().hex}"
        with psycopg.connect(test_dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
            try:
                dsn = make_conninfo(test_dsn, options=f"-c search_path={schema}")
                with psycopg.connect(dsn, autocommit=True) as conn:
                    assert apply_schema(conn) is True
                    assert apply_schema(conn) is False
                    assert _count(conn, "exam_editions") == 0
            finally:
                admin.execute(
                    sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema))
                )

    def test_second_writer_fails_fast_while_the_lock_is_held(
        self, schema_dsn: str
    ) -> None:
        with (
            psycopg.connect(schema_dsn, autocommit=True) as first,
            psycopg.connect(schema_dsn, autocommit=True) as second,
        ):
            with (
                write_lock(first),
                pytest.raises(LockHeld, match="another pipeline command"),
                write_lock(second),
            ):
                pytest.fail("the second writer must not run")
            with write_lock(second):  # released when the first command ends
                pass

    def test_reconcile_reports_counts_and_is_idempotent(
        self, db: psycopg.Connection
    ) -> None:
        rows = [
            EXAM_ROW,
            {**EXAM_ROW, "exam_id": "enem", "domains": ["enem.exams.test"]},
        ]
        first = reconcile(db, "exams", key=["exam_id"], scope={}, rows=rows)
        assert first == Counts(inserted=2, updated=0, deleted=0)
        assert reconcile(db, "exams", key=["exam_id"], scope={}, rows=rows) == Counts()

        changed = [{**EXAM_ROW, "name": "Renamed"}, rows[1]]
        assert reconcile(
            db, "exams", key=["exam_id"], scope={}, rows=changed
        ) == Counts(updated=1)
        assert reconcile(
            db, "exams", key=["exam_id"], scope={}, rows=changed[:1]
        ) == Counts(deleted=1)
        assert _count(db, "exams") == 1

    def test_a_failure_midway_leaves_the_scope_unchanged(
        self, db: psycopg.Connection
    ) -> None:
        reconcile(db, "exams", key=["exam_id"], scope={}, rows=[EXAM_ROW])
        bad = [{**EXAM_ROW, "name": "Changed"}, {**EXAM_ROW, "exam_id": "bogus"}]
        with pytest.raises(psycopg.errors.CheckViolation):
            reconcile(db, "exams", key=["exam_id"], scope={}, rows=bad)
        row = db.execute("SELECT name FROM exams WHERE exam_id = 'sat'").fetchone()
        assert row == ("Synthetic SAT",)
        assert _count(db, "exams") == 1

    def test_retire_marks_instead_of_deleting_and_unretires(
        self, db: psycopg.Connection, seed_edition: Callable[..., str]
    ) -> None:
        edition = seed_edition(db)

        def question(item: str, order: int) -> dict[str, object]:
            return {
                "question_id": f"{edition}:s:{item}",
                "canonical_question_id": f"cq_{item}",
                "section": "s",
                "item": item,
                "print_order": order,
                "stem": f"Synthetic stem {item}",
                "choices": [{"label": "A", "text": "a"}, {"label": "B", "text": "b"}],
                "has_figure": False,
                "has_table": False,
                "has_audio": False,
                "is_free_response": False,
                "stimulus_id": None,
                "source_url": "https://sat.exams.test/paper.pdf",
                "artifact_sha256": "ab" * 32,
                "page": 1,
                "pipeline_version": "abc123",
                "content_hash": f"hash-{item}",
            }

        scope = {"edition_id": edition}
        both = [question("1", 1), question("2", 2)]
        args = {"key": ["question_id"], "scope": scope, "retire": True}
        assert reconcile(db, "questions", rows=both, **args) == Counts(inserted=2)
        assert reconcile(db, "questions", rows=both, **args) == Counts()

        assert reconcile(db, "questions", rows=both[:1], **args) == Counts(deleted=1)
        assert _count(db, "questions") == 2  # retired, not deleted
        retired = db.execute(
            "SELECT retired FROM questions WHERE item = '2'"
        ).fetchone()
        assert retired == (True,)
        assert reconcile(db, "questions", rows=both[:1], **args) == Counts()

        assert reconcile(db, "questions", rows=both, **args) == Counts(updated=1)
        assert db.execute(
            "SELECT retired FROM questions WHERE item = '2'"
        ).fetchone() == (False,)

    def test_insert_if_new_adds_nothing_the_second_time(
        self, db: psycopg.Connection, seed_edition: Callable[..., str]
    ) -> None:
        reconcile(db, "exams", key=["exam_id"], scope={}, rows=[EXAM_ROW])
        edition = seed_edition(db, year=2021)
        source = {
            "source_id": "src_0000000000000001",
            "url": "https://sat.exams.test/a.pdf",
            "domain": "sat.exams.test",
            "discovery_method": "manual",
            "edition_id": edition,
            "decision": "approved",
        }
        assert insert_if_new(db, "sources", [source]) == 1
        assert insert_if_new(db, "sources", [source]) == 0

    def test_sql_injection_payloads_are_stored_and_scoped_as_data(
        self, db: psycopg.Connection, seed_edition: Callable[..., str]
    ) -> None:
        edition = seed_edition(db)
        payloads = ["x' OR 1=1 --", "x'); DROP TABLE questions; --"]
        rows = [
            {
                "question_id": payload,
                "canonical_question_id": "cq_1",
                "section": "s",
                "item": "1",
                "print_order": index + 1,
                "stem": "Synthetic stem",
                "choices": [],
                "has_figure": False,
                "has_table": False,
                "has_audio": False,
                "is_free_response": True,
                "stimulus_id": None,
                "source_url": "https://sat.exams.test/paper.pdf",
                "artifact_sha256": "ab" * 32,
                "page": 1,
                "pipeline_version": "abc123",
                "content_hash": f"h{index}",
            }
            for index, payload in enumerate(payloads)
        ]
        reconcile(
            db,
            "questions",
            key=["question_id"],
            scope={"edition_id": edition},
            rows=rows,
        )
        stored = {
            r[0] for r in db.execute("SELECT question_id FROM questions").fetchall()
        }
        assert stored == set(payloads)

        # A hostile scope value matches nothing and deletes nothing elsewhere.
        counts = reconcile(
            db,
            "questions",
            key=["question_id"],
            scope={"edition_id": "x' OR 1=1 --"},
            rows=[],
        )
        assert counts == Counts()
        assert _count(db, "questions") == 2

    def test_unknown_tables_and_columns_are_refused(
        self, db: psycopg.Connection
    ) -> None:
        with pytest.raises(ValueError, match="unknown table"):
            reconcile(db, "exams; DROP TABLE exams", key=["exam_id"], scope={}, rows=[])
        with pytest.raises(ValueError, match="unknown columns"):
            reconcile(
                db, "exams", key=["exam_id"], scope={}, rows=[{**EXAM_ROW, "evil": "1"}]
            )

    def test_connection_errors_never_contain_the_dsn(self) -> None:
        dsn = "postgresql://someone:s3cretpw@127.0.0.1:1/db?connect_timeout=2"
        with pytest.raises(ConnectionFailed) as caught:
            connect(dsn)
        assert "s3cretpw" not in str(caught.value)
        assert dsn not in str(caught.value)
        assert caught.value.__cause__ is None

    def test_redact_removes_passwords_in_both_dsn_styles(self) -> None:
        assert "pw1" not in redact("failed pw1", "postgresql://u:pw1@h/db")
        assert "pw2" not in redact("failed pw2", "host=h user=u password=pw2")

    def test_summary_line_format(self) -> None:
        summary = StageSummary(
            "extract",
            editions=12,
            counts=Counts(inserted=840),
            failures_added=3,
        )
        assert summary.line() == (
            "extract: editions=12 inserted=840 updated=0 deleted=0 failures=+3/-0"
        )


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


# --- the command line --------------------------------------------------------


class TestCli:
    def test_unknown_command_is_a_usage_error(self) -> None:
        assert cli.main(["mystery"]) == cli.EXIT_USAGE

    def test_help_exits_zero(self) -> None:
        assert cli.main(["--help"]) == cli.EXIT_OK

    def test_export_requires_a_snapshot(self) -> None:
        assert cli.main(["export", "--exam", "enem", "--eligible"]) == cli.EXIT_USAGE

    def test_flags_work_after_the_command_name(self) -> None:
        args = cli.build_parser().parse_args(
            ["fetch", "--exam", "enem", "--ledger", "x.jsonl"]
        )
        assert (args.exam, args.ledger) == ("enem", "x.jsonl")

    def test_unimplemented_commands_are_usage_errors(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(cli, "HANDLERS", {})
        assert cli.main(["rebuild", "--schema", "s"]) == cli.EXIT_USAGE

    @pytest.mark.parametrize(
        ("error", "code"),
        [
            (CatalogError("bad catalog"), cli.EXIT_INTEGRITY),
            (LedgerError("bad ledger"), cli.EXIT_INTEGRITY),
            (LockHeld("another pipeline command is running"), cli.EXIT_LOCKED),
            (ConnectionFailed("cannot connect"), cli.EXIT_ERROR),
            (RuntimeError("boom"), cli.EXIT_ERROR),
            (cli.UsageError("PIPELINE_DSN is not set"), cli.EXIT_USAGE),
        ],
    )
    def test_errors_map_to_the_documented_exit_codes(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        error: Exception,
        code: int,
    ) -> None:
        def raiser(args: object) -> int:
            raise error

        monkeypatch.setattr(cli, "HANDLERS", {"catalog": raiser})
        assert cli.main(["catalog"]) == code
        assert str(error) in capsys.readouterr().err

    def test_unexpected_errors_never_print_the_dsn(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        dsn = "postgresql://someone:s3cretpw@db.test/x"
        monkeypatch.setenv("PIPELINE_DSN", dsn)

        def raiser(args: object) -> int:
            raise RuntimeError(f"failed against {dsn}")

        monkeypatch.setattr(cli, "HANDLERS", {"catalog": raiser})
        assert cli.main(["catalog"]) == cli.EXIT_ERROR
        err = capsys.readouterr().err
        assert "s3cretpw" not in err and dsn not in err
