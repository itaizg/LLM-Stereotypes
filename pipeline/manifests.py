"""The committed manifests as typed data: catalog.yaml and ledger.jsonl.

Two committed files hold every human decision and every fetched hash, with no
question text (contracts/manifests.md):

* ``catalog.yaml``: human-authored exams and editions with their evidence.
* ``ledger.jsonl``: machine-written, append-only events (candidate URLs, review
  decisions, fetch outcomes, hand-checks).

Both are parsed here into frozen dataclasses at the boundary, so nothing
downstream handles untyped YAML or JSON (constitution Principle I).
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import yaml

from pipeline.corpus import canonical_json, sha256_hex
from pipeline.store import IntegrityRefusal

DEFAULT_RATE_LIMIT_SECONDS = 5.0

EDITION_STATUSES = ("ingested", "not_released", "blocked")
EDITION_KINDS = ("administered", "practice")


class CatalogError(IntegrityRefusal):
    """catalog.yaml is malformed or breaks a catalog rule."""


class LedgerError(IntegrityRefusal):
    """ledger.jsonl is malformed, or its seq numbers do not strictly increase."""


# --- narrowing helpers (the only place untyped parsed data is touched) --------


def _mapping(value: object, where: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise CatalogError(f"{where}: expected a mapping")
    return {str(k): v for k, v in value.items()}


def _text(raw: Mapping[str, object], key: str, where: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise CatalogError(f"{where}: {key} must be a non-empty string")
    return value


def _opt_text(raw: Mapping[str, object], key: str) -> str | None:
    value = raw.get(key)
    if value is None:
        return None
    return str(value)


def _opt_date(raw: Mapping[str, object], key: str, where: str) -> date | None:
    value = raw.get(key)
    if value is None:
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        raise CatalogError(f"{where}: {key} is not an ISO date") from None


def _number(raw: Mapping[str, object], key: str, default: float, where: str) -> float:
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise CatalogError(f"{where}: {key} must be a number")
    return float(value)


def _str_list(value: object, where: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise CatalogError(f"{where}: expected a list of strings")
    return tuple(str(v) for v in value)


# --- catalog ----------------------------------------------------------------


@dataclass(frozen=True)
class Seed:
    """An official index page or sitemap that discovery crawls (research R1)."""

    url: str
    complete_listing: bool = False


@dataclass(frozen=True)
class ParseConfig:
    """Per-exam layout, as data: one generic parser reads it (research R8)."""

    item_marker: str
    choice_markers: tuple[str, ...]
    columns: int
    key_table: str


@dataclass(frozen=True)
class Exam:
    exam_id: str
    name: str
    body: str
    language: str
    publishes_keys: bool
    keys_evidence: Mapping[str, str]
    domains: tuple[str, ...]
    rate_limit_seconds: float
    seeds: tuple[Seed, ...]
    parse: ParseConfig
    parse_english: ParseConfig | None
    card_notes: str


@dataclass(frozen=True)
class EvidenceItem:
    type: str
    source_id: str | None
    artifact_sha256: str | None
    note: str


@dataclass(frozen=True)
class Edition:
    edition_id: str
    exam_id: str
    year: int
    sitting: str
    kind: str
    status: str
    status_reason: str | None
    release_date: date | None
    official_url: str | None
    lineage: str
    language: str
    evidence: tuple[EvidenceItem, ...]
    english_item_map: Mapping[str, str] | None


@dataclass(frozen=True)
class TranslationConfig:
    model: str
    base_url: str


@dataclass(frozen=True)
class Catalog:
    version: int
    translation: TranslationConfig
    default_rate_limit_seconds: float
    exams: Mapping[str, Exam]
    editions: tuple[Edition, ...]


def _parse_config(
    raw: Mapping[str, object], base: ParseConfig | None, where: str
) -> ParseConfig:
    """Read a ``parse`` block; ``base`` supplies defaults for ``parse_english``."""
    item_marker = raw.get("item_marker", base.item_marker if base else None)
    key_table = raw.get("key_table", base.key_table if base else None)
    markers = raw.get("choice_markers", base.choice_markers if base else None)
    columns = raw.get("columns", base.columns if base else 1)
    if not isinstance(item_marker, str) or not item_marker:
        raise CatalogError(f"{where}: item_marker is required")
    if not isinstance(key_table, str) or not key_table:
        raise CatalogError(f"{where}: key_table is required")
    if isinstance(columns, bool) or not isinstance(columns, int) or columns < 1:
        raise CatalogError(f"{where}: columns must be a positive integer")
    return ParseConfig(
        item_marker=item_marker,
        choice_markers=_str_list(
            list(markers) if isinstance(markers, tuple) else markers, where
        ),
        columns=columns,
        key_table=key_table,
    )


def _seed(raw_seed: object, where: str) -> Seed:
    raw = _mapping(raw_seed, f"{where}.seeds")
    return Seed(
        url=_text(raw, "url", f"{where}.seeds"),
        complete_listing=raw.get("complete_listing") is True,
    )


def _exam(exam_id: str, raw_exam: object) -> Exam:
    where = f"exams.{exam_id}"
    raw = _mapping(raw_exam, where)
    evidence = raw.get("keys_evidence")
    parse = _parse_config(
        _mapping(raw.get("parse"), f"{where}.parse"), None, f"{where}.parse"
    )
    english = raw.get("parse_english")
    seeds_raw = raw.get("seeds") or []
    if not isinstance(seeds_raw, list):
        raise CatalogError(f"{where}.seeds: expected a list")
    return Exam(
        exam_id=exam_id,
        name=_text(raw, "name", where),
        body=_text(raw, "body", where),
        language=_text(raw, "language", where),
        publishes_keys=raw.get("publishes_keys") is True,
        keys_evidence=(
            {str(k): str(v) for k, v in evidence.items()}
            if isinstance(evidence, dict)
            else {}
        ),
        domains=tuple(
            d.lower() for d in _str_list(raw.get("domains"), f"{where}.domains")
        ),
        rate_limit_seconds=_number(raw, "rate_limit_seconds", -1.0, where),
        seeds=tuple(_seed(s, where) for s in seeds_raw),
        parse=parse,
        parse_english=(
            _parse_config(
                _mapping(english, f"{where}.parse_english"),
                parse,
                f"{where}.parse_english",
            )
            if english is not None
            else None
        ),
        card_notes=str(raw.get("card_notes") or ""),
    )


def _evidence(raw_list: object, where: str) -> tuple[EvidenceItem, ...]:
    if raw_list is None:
        return ()
    if not isinstance(raw_list, list):
        raise CatalogError(f"{where}.evidence: expected a list")
    items: list[EvidenceItem] = []
    for index, entry in enumerate(raw_list):
        raw = _mapping(entry, f"{where}.evidence[{index}]")
        items.append(
            EvidenceItem(
                type=_text(raw, "type", f"{where}.evidence[{index}]"),
                source_id=_opt_text(raw, "source_id"),
                artifact_sha256=_opt_text(raw, "artifact_sha256"),
                note=str(raw.get("note") or ""),
            )
        )
    return tuple(items)


def _edition(raw_edition: object, index: int) -> Edition:
    where = f"editions[{index}]"
    raw = _mapping(raw_edition, where)
    year = raw.get("year")
    if isinstance(year, bool) or not isinstance(year, int):
        raise CatalogError(f"{where}: year must be an integer")
    item_map = raw.get("english_item_map")
    return Edition(
        edition_id=_text(raw, "edition_id", where),
        exam_id=_text(raw, "exam", where),
        year=year,
        sitting=_text(raw, "sitting", where),
        kind=_text(raw, "kind", where),
        status=_text(raw, "status", where),
        status_reason=_opt_text(raw, "status_reason"),
        release_date=_opt_date(raw, "release_date", where),
        official_url=_opt_text(raw, "official_url"),
        lineage=_text(raw, "lineage", where),
        language=_text(raw, "language", where),
        evidence=_evidence(raw.get("evidence"), where),
        english_item_map=(
            {str(k): str(v) for k, v in item_map.items()}
            if isinstance(item_map, dict)
            else None
        ),
    )


def load_catalog(path: Path) -> Catalog:
    """Parse ``catalog.yaml`` into dataclasses; shape errors raise ``CatalogError``.

    Rule checks (66 cells, evidence, approved sources) are a separate step,
    ``validate_catalog``, so a malformed file and an incomplete one are
    reported differently.
    """
    try:
        parsed: object = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise CatalogError(
            f"{path}: cannot read catalog ({type(error).__name__})"
        ) from None
    top = _mapping(parsed, str(path))
    defaults = _mapping(top.get("defaults") or {}, "defaults")
    default_rate = _number(
        defaults, "rate_limit_seconds", DEFAULT_RATE_LIMIT_SECONDS, "defaults"
    )
    translation = _mapping(top.get("translation") or {}, "translation")
    exams_raw = _mapping(top.get("exams") or {}, "exams")
    exams = {
        exam_id: _exam(exam_id, body) for exam_id, body in sorted(exams_raw.items())
    }
    editions_raw = top.get("editions") or []
    if not isinstance(editions_raw, list):
        raise CatalogError("editions: expected a list")
    version = top.get("version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise CatalogError("version must be an integer")
    return Catalog(
        version=version,
        translation=TranslationConfig(
            model=_text(translation, "model", "translation"),
            base_url=_text(translation, "base_url", "translation"),
        ),
        default_rate_limit_seconds=default_rate,
        exams=exams,
        editions=tuple(_edition(e, i) for i, e in enumerate(editions_raw)),
    )


def rate_limit_for(catalog: Catalog, exam_id: str) -> float:
    """Seconds between requests to one domain: the exam's override, else the default."""
    override = catalog.exams[exam_id].rate_limit_seconds
    return override if override >= 0 else catalog.default_rate_limit_seconds


# --- ledger -----------------------------------------------------------------

LEDGER_KINDS = ("candidate", "review", "fetch", "handcheck", "translation_review")


@dataclass(frozen=True)
class LedgerEvent:
    """One ledger line. ``fields`` holds the kind-specific payload."""

    seq: int
    kind: str
    at: str
    fields: Mapping[str, object]

    def text(self, key: str) -> str | None:
        value = self.fields.get(key)
        return None if value is None else str(value)

    def number(self, key: str) -> int | None:
        value = self.fields.get(key)
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        return value


def read_ledger(path: Path) -> list[LedgerEvent]:
    """Read every event; raise ``LedgerError`` on bad JSON or a bad ``seq``."""
    if not path.exists():
        return []
    events: list[LedgerEvent] = []
    last = 0
    for number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not line.strip():
            continue
        try:
            raw: object = json.loads(line)
        except json.JSONDecodeError:
            raise LedgerError(f"{path}:{number}: not valid JSON") from None
        body = _ledger_mapping(raw, path, number)
        seq = body.pop("seq", None)
        kind = body.pop("kind", None)
        at = body.pop("at", None)
        if isinstance(seq, bool) or not isinstance(seq, int):
            raise LedgerError(f"{path}:{number}: seq must be an integer")
        if kind not in LEDGER_KINDS or not isinstance(at, str):
            raise LedgerError(f"{path}:{number}: unknown kind or missing at")
        if seq <= last:
            raise LedgerError(
                f"{path}:{number}: seq {seq} does not follow {last} "
                "(it must strictly increase)"
            )
        last = seq
        events.append(LedgerEvent(seq=seq, kind=str(kind), at=at, fields=body))
    return events


def _ledger_mapping(raw: object, path: Path, number: int) -> dict[str, object]:
    if not isinstance(raw, dict):
        raise LedgerError(f"{path}:{number}: expected a JSON object")
    return {str(k): v for k, v in raw.items()}


def append_event(
    path: Path, kind: str, fields: Mapping[str, object], *, at: str
) -> LedgerEvent:
    """Append one event with the next ``seq``, fsynced.

    Callers hold the pipeline write lock (research R16), which is what makes the
    read-last-seq-then-append here safe. ``at`` is for audit only and never feeds
    a hash or an ID, so callers pass it in rather than this module reading a clock.
    """
    if kind not in LEDGER_KINDS:
        raise LedgerError(f"unknown ledger kind: {kind!r}")
    existing = read_ledger(path)
    seq = existing[-1].seq + 1 if existing else 1
    line = canonical_json({"seq": seq, "kind": kind, "at": at, **fields}) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line)
        handle.flush()
        os.fsync(handle.fileno())
    return LedgerEvent(seq=seq, kind=kind, at=at, fields=dict(fields))


def ledger_is_prefix_extension(old: bytes, new: bytes) -> bool:
    """True if ``new`` only appends to ``old``: lines are never edited or removed."""
    return new.startswith(old) and (not old or old.endswith(b"\n"))


def source_id_for(url: str) -> str:
    """The stable id of a candidate URL: ``src_`` plus 16 hex digits of its SHA-256."""
    return "src_" + sha256_hex(url)[:16]


def events_of_kind(events: Sequence[LedgerEvent], kind: str) -> list[LedgerEvent]:
    return [e for e in events if e.kind == kind]


# --- the ledger, replayed into current state -----------------------------------

REVIEW_ROLES = (
    "paper",
    "key",
    "key_erratum",
    "english",
    "audio",
    "statement",
    "index_page",
)


@dataclass(frozen=True)
class Approval:
    """One approved use of a source: this file serves this edition in this role."""

    edition_id: str
    role: str
    published: date | None


@dataclass(frozen=True)
class FetchRecord:
    outcome: str
    sha256: str | None
    media_type: str | None
    size: int | None


@dataclass
class SourceState:
    """What the ledger currently says about one candidate URL."""

    source_id: str
    url: str
    domain: str
    method: str
    found_on: str | None
    decision: str = "pending"
    decision_reason: str | None = None
    edition_id: str | None = None
    approvals: dict[str, Approval] = field(default_factory=dict)
    fetches: list[FetchRecord] = field(default_factory=list)

    @property
    def fetch_outcome(self) -> str:
        return self.fetches[-1].outcome if self.fetches else "not_attempted"


@dataclass(frozen=True)
class LedgerState:
    sources: Mapping[str, SourceState]
    handchecks: tuple[LedgerEvent, ...]
    translation_reviews: tuple[LedgerEvent, ...]


def _opt_iso(value: str | None, where: str) -> date | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise LedgerError(f"{where}: published is not an ISO date") from None


def replay_ledger(events: Sequence[LedgerEvent]) -> LedgerState:
    """Fold the events, in ``seq`` order, into the current state of each source.

    The latest review decides a source's decision; every approval it has not
    since lost stays attached to its edition; the latest fetch decides its
    outcome, and every distinct fetched hash is kept (a file never disappears
    when the content at its URL changes, FR-009).
    """
    sources: dict[str, SourceState] = {}
    handchecks: list[LedgerEvent] = []
    reviews: list[LedgerEvent] = []
    for event in events:
        where = f"ledger seq {event.seq}"
        if event.kind == "candidate":
            sid = event.text("source_id")
            url = event.text("url")
            if sid is None or url is None:
                raise LedgerError(f"{where}: candidate needs source_id and url")
            if sid in sources:
                continue
            state = SourceState(
                source_id=sid,
                url=url,
                domain=event.text("domain") or "",
                method=event.text("method") or "manual",
                found_on=event.text("found_on"),
            )
            auto = event.fields.get("auto_decision")
            if isinstance(auto, dict) and auto.get("decision") == "rejected":
                state.decision = "rejected"
                state.decision_reason = str(auto.get("reason") or "rejected")
            sources[sid] = state
        elif event.kind in {"review", "fetch"}:
            sid = event.text("source_id")
            if sid is None or sid not in sources:
                raise LedgerError(f"{where}: {event.kind} for an unknown source")
            state = sources[sid]
            if event.kind == "fetch":
                state.fetches.append(
                    FetchRecord(
                        outcome=event.text("outcome") or "http_error",
                        sha256=event.text("sha256"),
                        media_type=event.text("media_type"),
                        size=event.number("bytes"),
                    )
                )
            elif event.text("decision") == "approved":
                edition_id = event.text("edition_id")
                role = event.text("role")
                if edition_id is None or role is None:
                    raise LedgerError(f"{where}: approval needs edition_id and role")
                state.decision = "approved"
                state.decision_reason = None
                state.edition_id = edition_id
                state.approvals[edition_id] = Approval(
                    edition_id, role, _opt_iso(event.text("published"), where)
                )
            else:
                state.decision = "rejected"
                state.decision_reason = event.text("reason") or "rejected"
                state.edition_id = None
                state.approvals.clear()
        elif event.kind == "handcheck":
            handchecks.append(event)
        elif event.kind == "translation_review":
            reviews.append(event)
    return LedgerState(sources, tuple(handchecks), tuple(reviews))


def artifact_rows(state: LedgerState) -> list[dict[str, object]]:
    """One row per (distinct fetched file, approved edition) of every source."""
    rows: list[dict[str, object]] = []
    for source in sorted(state.sources.values(), key=lambda s: s.source_id):
        first: dict[str, FetchRecord] = {}
        for record in source.fetches:
            if (
                record.outcome == "fetched"
                and record.sha256
                and record.sha256 not in first
            ):
                first[record.sha256] = record
        for sha256, record in first.items():
            for approval in source.approvals.values():
                rows.append(
                    {
                        "sha256": sha256,
                        "edition_id": approval.edition_id,
                        "source_id": source.source_id,
                        "role": approval.role,
                        "media_type": record.media_type or "application/octet-stream",
                        "published": approval.published,
                        "bytes": record.size or 0,
                    }
                )
    return rows


# --- catalog validation ------------------------------------------------------

EXAM_IDS = ("sat", "psychometric", "csat", "gaokao", "common_test", "enem")
YEARS = range(2015, 2026)
EVIDENCE_TYPES = ("release_page", "statement", "index_absence")
STATUS_REASONS = ("robots", "non_allowlisted", "login_required")
_EDITION_ID = re.compile(r"^[a-z][a-z_]*-20(1[5-9]|2[0-5])-[a-z0-9-]+$")


def _approved(state: LedgerState, source_id: str | None) -> SourceState | None:
    if source_id is None:
        return None
    source = state.sources.get(source_id)
    return source if source is not None and source.decision == "approved" else None


def validate_catalog(catalog: Catalog, state: LedgerState) -> list[str]:
    """Return every rule the catalog breaks (data-model, ``exam_editions``).

    All problems are returned together so one run shows the whole job. The
    stage refuses to write anything while this list is non-empty.
    """
    errors: list[str] = []
    for exam_id in sorted(catalog.exams):
        if exam_id not in EXAM_IDS:
            errors.append(f"unknown exam: {exam_id}")
    for exam_id in EXAM_IDS:
        exam = catalog.exams.get(exam_id)
        if exam is None:
            errors.append(f"exam {exam_id}: missing from the catalog")
            continue
        if not exam.publishes_keys:
            errors.append(f"exam {exam_id}: publishes_keys must be true (FR-006)")
        if _approved(state, exam.keys_evidence.get("source_id")) is None:
            errors.append(f"exam {exam_id}: keys_evidence must cite an approved source")

    complete = {
        exam_id: {s.url for s in exam.seeds if s.complete_listing}
        for exam_id, exam in catalog.exams.items()
    }
    artifacts: dict[str, set[str]] = {}
    for row in artifact_rows(state):
        artifacts.setdefault(str(row["edition_id"]), set()).add(str(row["role"]))
    artifact_hashes: dict[str, set[str]] = {}
    for row in artifact_rows(state):
        artifact_hashes.setdefault(str(row["edition_id"]), set()).add(
            str(row["sha256"])
        )

    seen: set[str] = set()
    covered: set[tuple[str, int]] = set()
    for edition in catalog.editions:
        label = edition.edition_id
        if label in seen:
            errors.append(f"{label}: duplicate edition_id")
        seen.add(label)
        covered.add((edition.exam_id, edition.year))
        expected = f"{edition.exam_id}-{edition.year}-{edition.sitting}"
        if label != expected or not _EDITION_ID.fullmatch(label):
            errors.append(f"{label}: edition_id must be {{exam}}-{{year}}-{{sitting}}")
        if edition.exam_id not in catalog.exams:
            errors.append(f"{label}: unknown exam {edition.exam_id}")
        if edition.year not in YEARS:
            errors.append(f"{label}: year must be 2015-2025")
        if edition.kind not in EDITION_KINDS:
            errors.append(f"{label}: kind must be one of {', '.join(EDITION_KINDS)}")
        if edition.status not in EDITION_STATUSES:
            errors.append(
                f"{label}: status must be one of {', '.join(EDITION_STATUSES)}"
            )
        if edition.status == "blocked" and edition.status_reason not in STATUS_REASONS:
            errors.append(
                f"{label}: blocked edition needs status_reason "
                f"({', '.join(STATUS_REASONS)})"
            )
        if edition.status == "ingested":
            if edition.release_date is None or edition.official_url is None:
                errors.append(
                    f"{label}: ingested edition needs release_date and official_url"
                )
            roles = artifacts.get(label, set())
            errors.extend(
                f"{label}: ingested but has no {role} artifact"
                for role in ("paper", "key")
                if role not in roles
            )
        if not edition.evidence:
            errors.append(f"{label}: no evidence")
        for item in edition.evidence:
            if item.type not in EVIDENCE_TYPES:
                errors.append(f"{label}: unknown evidence type {item.type!r}")
                continue
            source = _approved(state, item.source_id)
            stored = item.artifact_sha256 in artifact_hashes.get(label, set())
            if source is None and not stored:
                errors.append(
                    f"{label}: evidence cites source {item.source_id} "
                    "that is not approved"
                )
            elif item.type == "index_absence" and (
                source is None or source.url not in complete.get(edition.exam_id, set())
            ):
                errors.append(
                    f"{label}: index_absence needs a source that is a "
                    "complete_listing seed"
                )
        if edition.status == "not_released" and not any(
            item.type in {"statement", "index_absence"} for item in edition.evidence
        ):
            errors.append(f"{label}: not_released needs a statement or index_absence")

    errors.extend(
        f"cell {exam_id}/{year} has no edition"
        for exam_id in EXAM_IDS
        for year in YEARS
        if (exam_id, year) not in covered
    )
    errors.extend(
        f"source {source.source_id} is approved for unknown edition {edition_id}"
        for source in sorted(state.sources.values(), key=lambda s: s.source_id)
        for edition_id in sorted(source.approvals)
        if edition_id not in seen
    )
    return errors
