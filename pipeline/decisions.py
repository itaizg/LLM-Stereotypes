"""The review page's decisions file, and its import into the ledger (spec 003 FR-026).

The pipeline owns this input, so its model lives here and the crawler imports it to
build the review page (``crawler → pipeline``, never the reverse). Everything in the
file is validated at the boundary before anything is written: size, shape, unknown
fields, and the rules of each decision.

Format: specs/003-crawler-agent/contracts/decisions-file.md.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal

import psycopg
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from pipeline.manifests import (
    REVIEW_ROLES,
    Catalog,
    LedgerState,
    append_events,
    catalog_view,
    read_ledger,
    replay_ledger,
    source_id_for,
    source_origins,
)

FORMAT = "crawler-decisions/1"
MAX_FILE_BYTES = 5 * 1024 * 1024
EXAM_KEY_PATTERN = r"^[a-z][a-z0-9_]{1,40}$"


class DecisionsError(ValueError):
    """The decisions file is malformed or breaks a rule (exit code 2)."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Member(_Strict):
    """One URL of a content group, with the revision the maintainer saw."""

    proposal_id: str = Field(pattern=r"^prop_[0-9a-f]{16}$")
    revision: int = Field(ge=1)
    primary: bool
    origin: Literal["official", "third_party"]


class ExamRef(_Strict):
    key: str = Field(pattern=EXAM_KEY_PATTERN)
    new: bool = False


class NewExam(_Strict):
    name: str = Field(min_length=1, max_length=120)
    body: str = Field(min_length=1, max_length=200)
    language: str = Field(pattern=r"^[a-z]{2,3}(-[A-Za-z0-9]{2,8})?$")


class Decision(_Strict):
    decision_id: str = Field(pattern=r"^dec_[0-9a-f]{16}$")
    content_hash: str = Field(min_length=1, max_length=100)
    members: tuple[Member, ...] = Field(min_length=1)
    decision: Literal["approve", "reject"]
    exam: ExamRef | None = None
    new_exam: NewExam | None = None
    edition_id: str | None = Field(default=None, max_length=120)
    role: str | None = None
    published: date | None = None
    reason: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def _rules(self) -> Decision:
        if sum(m.primary for m in self.members) != 1:
            raise ValueError("a content group has exactly one primary member")
        if len({m.proposal_id for m in self.members}) != len(self.members):
            raise ValueError("a member appears twice")
        if self.decision == "reject":
            if not self.reason:
                raise ValueError("a reject needs a reason")
            return self
        if self.exam is None or not self.edition_id or self.role is None:
            raise ValueError("an approve needs exam, edition_id and role")
        if self.role not in REVIEW_ROLES:
            raise ValueError(f"role must be one of {', '.join(REVIEW_ROLES)}")
        if self.role == "key_erratum" and self.published is None:
            raise ValueError("a key_erratum needs published")
        if self.exam.new != (self.new_exam is not None):
            raise ValueError("new_exam is given exactly when exam.new is true")
        return self


class DecisionsFile(_Strict):
    format: Literal["crawler-decisions/1"]
    run_id: str = Field(pattern=r"^cr_\d{8}T\d{6}_[0-9a-f]{4}$")
    decided_at: datetime
    decisions: tuple[Decision, ...]

    @model_validator(mode="after")
    def _unique(self) -> DecisionsFile:
        ids = [d.decision_id for d in self.decisions]
        if len(set(ids)) != len(ids):
            raise ValueError("each content group is decided once per file")
        return self


def decision_id_for(run_id: str, content_hash: str, revisions: list[int]) -> str:
    """``dec_<sha256(run_id|content_hash|revisions)[:16]>``: names the group shown.

    ``revisions`` are the members' revisions in member order. The id names what was
    decided on, not the outcome, so a changed decision on the same revisions keeps it.
    """
    text = "|".join([run_id, content_hash, ",".join(str(r) for r in revisions)])
    return "dec_" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def load_decisions(path: Path) -> DecisionsFile:
    """Read and validate a decisions file, or raise ``DecisionsError``."""
    try:
        size = path.stat().st_size
    except OSError as error:
        raise DecisionsError(f"cannot read {path.name}: {error.strerror}") from None
    if size > MAX_FILE_BYTES:
        raise DecisionsError(f"{path.name} is over {MAX_FILE_BYTES} bytes")
    return parse_decisions(path.read_bytes(), path.name)


def parse_decisions(data: bytes, name: str = "decisions") -> DecisionsFile:
    if len(data) > MAX_FILE_BYTES:
        raise DecisionsError(f"{name} is over {MAX_FILE_BYTES} bytes")
    try:
        raw = json.loads(data)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise DecisionsError(f"{name} is not valid JSON") from None
    try:
        return DecisionsFile.model_validate(raw)
    except ValidationError as error:
        first = error.errors()[0]
        where = ".".join(str(p) for p in first["loc"]) or "file"
        raise DecisionsError(f"{name}: {where}: {first['msg']}") from None


# --- import (FR-026, FR-028, FR-021b) -------------------------------------------------


@dataclass(frozen=True)
class MemberRow:
    """A member's proposal rows as the import sees them, under a row lock."""

    proposal_id: str
    current_revision: int
    shown_revision: int
    url: str
    domain: str
    content_hash: str | None
    validation_status: str
    review_state: str
    labels: Mapping[str, Any]
    checks: Mapping[str, Any]


@dataclass
class ImportSummary:
    lines: list[str] = field(default_factory=list)
    approved: int = 0
    rejected: int = 0
    stale: int = 0
    unchanged: int = 0
    reopened: int = 0

    def line(self) -> str:
        return (
            f"import-decisions: approved={self.approved} rejected={self.rejected} "
            f"stale={self.stale} unchanged={self.unchanged} reopened={self.reopened}"
        )


def _labels_digest(labels: Mapping[str, Any]) -> str:
    material = {k: v for k, v in labels.items() if not str(k).endswith("_confidence")}
    return hashlib.sha256(
        json.dumps(material, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _evidence(row: MemberRow) -> list[str]:
    evidence = [row.url]
    key = (row.checks.get("key_located") or {}).get("evidence")
    if isinstance(key, str) and key.startswith(("https://", "http://")):
        evidence.append(key)
    return evidence


def _member_rows(
    conn: psycopg.Connection, decision: Decision, *, lock: bool
) -> dict[str, MemberRow]:
    """Each member's current row (locked when ``lock``) and the row it was shown at."""
    ids = [m.proposal_id for m in decision.members]
    if lock:
        # The same per-URL lock ``write_proposal`` takes, then the rows themselves, in
        # a fixed order. Reading the current revisions only after both are held means
        # a revision committed while this waited is seen, and the decision is stale.
        urls = conn.execute(
            "SELECT DISTINCT normalised_url FROM crawler_proposals "
            "WHERE proposal_id = ANY(%s) ORDER BY 1",
            (ids,),
        ).fetchall()
        for (url,) in urls:
            conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (url,)
            )
        conn.execute(
            "SELECT 1 FROM crawler_proposals WHERE proposal_id = ANY(%s) "
            "ORDER BY proposal_id, revision FOR UPDATE",
            (ids,),
        )
    current = conn.execute(
        "SELECT proposal_id, max(revision) FROM crawler_proposals "
        "WHERE proposal_id = ANY(%s) GROUP BY proposal_id",
        (ids,),
    ).fetchall()
    latest = {str(pid): int(rev) for pid, rev in current}
    rows: dict[str, MemberRow] = {}
    for member in decision.members:
        shown = conn.execute(
            "SELECT url, domain, content_hash, validation_status, review_state, "
            "labels, "
            "checks FROM crawler_proposals WHERE proposal_id = %s AND revision = %s",
            (member.proposal_id, member.revision),
        ).fetchone()
        if shown is None or member.proposal_id not in latest:
            continue
        url, domain, digest, status, review_state, labels, checks = shown
        rows[member.proposal_id] = MemberRow(
            member.proposal_id,
            latest[member.proposal_id],
            member.revision,
            url,
            domain,
            digest,
            status,
            review_state,
            labels,
            checks,
        )
    return rows


def validate_against_store(
    conn: psycopg.Connection, file: DecisionsFile, catalog: Catalog
) -> None:
    """Every rule that needs the store, checked before anything is written."""
    added: set[str] = set()
    for decision in file.decisions:
        rows = _member_rows(conn, decision, lock=False)
        where = decision.decision_id
        for member in decision.members:
            row = rows.get(member.proposal_id)
            if row is None:
                raise DecisionsError(
                    f"{where}: {member.proposal_id} revision {member.revision} not "
                    f"found"
                )
            if row.content_hash != decision.content_hash:
                raise DecisionsError(f"{where}: members must share the group's content")
            if decision.decision == "approve" and row.validation_status != "passed":
                raise DecisionsError(
                    f"{where}: {member.proposal_id} is not passed and cannot be "
                    f"approved"
                )
        if decision.decision != "approve" or decision.exam is None:
            continue
        key = decision.exam.key
        known = key in catalog.exams or key in added
        if decision.exam.new and known and key not in added:
            raise DecisionsError(
                f"{where}: exam {key} already exists; new must be false"
            )
        if not decision.exam.new and not known:
            raise DecisionsError(f"{where}: unknown exam {key}; mark it new to add it")
        if decision.exam.new:
            keyed = any(
                rows[m.proposal_id].labels.get("key_status")
                in {"linked_file", "inline"}
                for m in decision.members
            )
            if not keyed:
                raise DecisionsError(
                    f"{where}: a new exam needs a member whose answer key is linked or "
                    "inline (publishes_keys)"
                )
            added.add(key)


def _already(state: LedgerState, sid: str, decision: Decision) -> bool:
    """True when the source's latest review already says exactly this."""
    source = state.sources.get(sid)
    if source is None:
        return False
    if decision.decision == "reject":
        return (
            source.decision == "rejected" and source.decision_reason == decision.reason
        )
    approval = source.approvals.get(decision.edition_id or "")
    return (
        source.decision == "approved"
        and approval is not None
        and approval.role == decision.role
        and approval.published == decision.published
    )


def _events(
    state: LedgerState,
    catalog: Catalog,
    decision: Decision,
    rows: Mapping[str, MemberRow],
) -> list[tuple[str, dict[str, object]]]:
    events: list[tuple[str, dict[str, object]]] = []
    did = decision.decision_id
    primary = next(m for m in decision.members if m.primary)
    exam_key = decision.exam.key if decision.exam is not None else None
    if decision.decision == "approve" and exam_key is not None:
        adds_exam = (
            decision.exam is not None
            and decision.exam.new
            and decision.new_exam is not None
            and exam_key not in state.exams_added
            and exam_key not in catalog.exams
        )
        if adds_exam and decision.new_exam is not None:
            row = rows[primary.proposal_id]
            events.append(
                (
                    "exam_added",
                    {
                        "exam_key": exam_key,
                        "name": decision.new_exam.name,
                        "body": decision.new_exam.body,
                        "language": decision.new_exam.language,
                        "domain": row.domain,
                        "decision_id": did,
                        "proposal_id": primary.proposal_id,
                        "revision": primary.revision,
                        "evidence": _evidence(row),
                    },
                )
            )
        allowed = (
            set(catalog.exams[exam_key].domains) if exam_key in catalog.exams else set()
        )
        allowed |= set(state.allowlisted.get(exam_key, frozenset()))
        if events and events[0][0] == "exam_added":
            allowed.add(str(events[0][1]["domain"]))
        for member in decision.members:
            domain = rows[member.proposal_id].domain
            if member.origin == "third_party" and domain not in allowed:
                events.append(
                    (
                        "domain_allowlisted",
                        {
                            "exam_key": exam_key,
                            "domain": domain,
                            "origin": member.origin,
                            "decision_id": did,
                        },
                    )
                )
                allowed.add(domain)
    for member in decision.members:
        row = rows[member.proposal_id]
        sid = source_id_for(row.url)
        source = state.sources.get(sid)
        if source is None or (
            source.method == "crawler" and source.revision != member.revision
        ):
            events.append(
                (
                    "candidate",
                    {
                        "source_id": sid,
                        "url": row.url,
                        "domain": row.domain,
                        "method": "crawler",
                        "found_on": None,
                        "proposal_id": member.proposal_id,
                        "revision": member.revision,
                        "origin": member.origin,
                        "labels_digest": _labels_digest(row.labels),
                        "evidence": _evidence(row),
                        "decision_id": did,
                    },
                )
            )
    for member in decision.members:
        sid = source_id_for(rows[member.proposal_id].url)
        approve = decision.decision == "approve"
        events.append(
            (
                "review",
                {
                    "source_id": sid,
                    "decision": "approved" if approve else "rejected",
                    "edition_id": decision.edition_id if approve else None,
                    "role": decision.role if approve else None,
                    "published": decision.published.isoformat()
                    if approve and decision.published
                    else None,
                    "reason": None if approve else decision.reason,
                    "decision_id": did,
                },
            )
        )
    return events


def _set_review_state(
    conn: psycopg.Connection, decision: Decision, state_name: str
) -> None:
    for member in decision.members:
        conn.execute(
            "UPDATE crawler_proposals SET review_state = %s "
            "WHERE proposal_id = %s AND revision = %s",
            (state_name, member.proposal_id, member.revision),
        )


def import_one(
    conn: psycopg.Connection,
    ledger: Path,
    catalog: Catalog,
    decision: Decision,
    at: str,
) -> str:
    """One decision in one transaction: stale check under row locks, then one block."""
    with conn.transaction():
        rows = _member_rows(conn, decision, lock=True)
        if any(r.current_revision != r.shown_revision for r in rows.values()) or len(
            rows
        ) != len(decision.members):
            return "stale"
        state = replay_ledger(read_ledger(ledger))
        view = catalog_view(catalog, state)
        target = "approved" if decision.decision == "approve" else "rejected"
        sids = [source_id_for(rows[m.proposal_id].url) for m in decision.members]
        if all(_already(state, sid, decision) for sid in sids):
            _set_review_state(conn, decision, target)
            return "unchanged"
        events = _events(state, view, decision, rows)
        append_events(ledger, events, at=at)
        _set_review_state(conn, decision, target)
    return target


def reopen_sweep(conn: psycopg.Connection, ledger: Path, at: str) -> int:
    """Append ``review_reopened`` for each approved source whose content changed."""
    state = replay_ledger(read_ledger(ledger))
    done = {(e.text("source_id"), e.number("to_revision")) for e in state.reopened}
    rows = conn.execute(
        "SELECT p.proposal_id, p.revision, p.url, p.content_hash, "
        "p.labels->>'key_status', prev.revision, prev.content_hash, "
        "prev.labels->>'key_status' FROM crawler_proposals p "
        "JOIN crawler_proposals prev ON prev.proposal_id = p.proposal_id "
        "AND prev.revision = p.revision - 1 "
        "WHERE p.labels->'flags' ? 'reopened_after_approval' ORDER BY p.proposal_id"
    ).fetchall()
    count = 0
    for pid, revision, url, digest, key, prev_revision, prev_digest, prev_key in rows:
        sid = source_id_for(url)
        source = state.sources.get(sid)
        if source is None or source.method != "crawler" or (sid, revision) in done:
            continue
        changed = [
            name
            for name, now, before in (
                ("content", digest, prev_digest),
                ("key", key, prev_key),
            )
            if now != before
        ]
        append_events(
            ledger,
            [
                (
                    "review_reopened",
                    {
                        "source_id": sid,
                        "proposal_id": pid,
                        "from_revision": source.revision or prev_revision,
                        "to_revision": revision,
                        "changed": changed,
                    },
                )
            ],
            at=at,
        )
        count += 1
    return count


def import_decisions(
    conn: psycopg.Connection,
    ledger: Path,
    catalog: Catalog,
    file: DecisionsFile | None,
    at: str,
) -> ImportSummary:
    """The reopen sweep, then each decision. Validation errors write nothing."""
    summary = ImportSummary()
    if file is not None:
        state = replay_ledger(read_ledger(ledger))
        validate_against_store(conn, file, catalog_view(catalog, state))
    summary.reopened = reopen_sweep(conn, ledger, at)
    for decision in file.decisions if file is not None else ():
        outcome = import_one(conn, ledger, catalog, decision, at)
        summary.lines.append(f"{decision.decision_id} {outcome}")
        if outcome == "approved":
            summary.approved += 1
        elif outcome == "rejected":
            summary.rejected += 1
        elif outcome == "stale":
            summary.stale += 1
        else:
            summary.unchanged += 1
    return summary


def suspended_sources(conn: psycopg.Connection, state: LedgerState) -> frozenset[str]:
    """Crawler sources with a revision newer than the one approved (FR-021b).

    ``pipeline fetch`` skips them as soon as the crawler commits the new revision,
    before the next import's sweep reopens them in the ledger.
    """
    crawler = {
        s.proposal_id: (sid, s.revision)
        for sid, s in state.sources.items()
        if s.method == "crawler"
        and s.proposal_id is not None
        and s.revision is not None
    }
    if not crawler:
        return frozenset()
    rows = conn.execute(
        "SELECT proposal_id, max(revision) FROM crawler_proposals "
        "WHERE proposal_id = ANY(%s) GROUP BY proposal_id",
        (list(crawler),),
    ).fetchall()
    return frozenset(
        crawler[pid][0]
        for pid, latest in rows
        if latest is not None and latest > (crawler[pid][1] or 0)
    )


def origin_counts(
    conn: psycopg.Connection, state: LedgerState, catalog: Catalog
) -> dict[str, dict[str, dict[str, int]]]:
    """Per exam: official and third-party question and key counts (FR-029).

    Origin is the source's ledger origin (no column), joined to questions and keys
    through the artifacts each source supplied.
    """
    origins = source_origins(state)
    edition_exam = {e.edition_id: e.exam_id for e in catalog.editions}
    counts: dict[str, dict[str, dict[str, int]]] = {}
    for table in ("questions", "answer_keys"):
        join = (
            "SELECT a.source_id, a.edition_id, count(*) FROM questions q "
            "JOIN artifacts a ON a.sha256 = q.artifact_sha256 GROUP BY 1, 2"
            if table == "questions"
            else "SELECT a.source_id, a.edition_id, count(*) FROM answer_keys k "
            "JOIN artifacts a ON a.sha256 = k.artifact_sha256 GROUP BY 1, 2"
        )
        for sid, edition_id, n in conn.execute(join).fetchall():
            exam = edition_exam.get(str(edition_id), str(edition_id).split("-", 1)[0])
            origin = origins.get(str(sid), "official")
            entry = counts.setdefault(exam, {}).setdefault(
                origin, {"questions": 0, "keys": 0}
            )
            entry["questions" if table == "questions" else "keys"] += int(n)
    return counts
