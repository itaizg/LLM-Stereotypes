"""Acquisition: catalog and ledger readers, and the acquisition stages.

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
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
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
