"""The crawler's contracts: tool inputs and outputs, proposals and hand-off tickets.

Every model here is the boundary between untrusted data (search APIs, web pages, LLM
output) and the crawler's own logic, so each one forbids unknown fields and caps every
free-text field. No model has room for question text (FR-020): outputs carry counts,
hashes, URLs, enums, confidences, a title of at most 120 characters and a terms excerpt
of at most 300.

Quantities follow research R11: the field name ends in its unit (``latency_s``,
``size_bytes``, ``cost_usd``) and the JSON schema carries it as ``unit``.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

TITLE_MAX = 120
EXCERPT_MAX = 300
NOTE_MAX = 80


class Kind(StrEnum):
    PDF = "pdf"
    WEB_PAGE = "web_page"


class Origin(StrEnum):
    OFFICIAL = "official"
    THIRD_PARTY = "third_party"


class SiteType(StrEnum):
    EXAM_BODY = "exam_body"
    PREP = "prep"
    EDUCATION = "education"
    NEWS = "news"
    BLOG = "blog"
    FORUM = "forum"
    OTHER = "other"


class PageType(StrEnum):
    PAST_PAPER = "past_paper"
    PRACTICE_TEST = "practice_test"
    QUESTION_BANK = "question_bank"
    ANSWER_KEY = "answer_key"
    ARTICLE = "article"
    OTHER = "other"


EXAM_PAGE_TYPES = frozenset(
    {
        PageType.PAST_PAPER,
        PageType.PRACTICE_TEST,
        PageType.QUESTION_BANK,
        PageType.ANSWER_KEY,
    }
)


class Topic(StrEnum):
    MATH = "math"
    READING = "reading"
    WRITING = "writing"
    SCIENCE = "science"
    LANGUAGE = "language"
    SOCIAL_STUDIES = "social_studies"
    OTHER = "other"


class KeyStatus(StrEnum):
    LINKED_FILE = "linked_file"
    INLINE = "inline"
    LIKELY = "likely"
    NOT_FOUND = "not_found"


class CheckResult(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    NOT_RUN = "not_run"


class ValidationStatus(StrEnum):
    PENDING = "pending"
    PASSED = "passed"
    FAILED = "failed"


class ReasonCode(StrEnum):
    """Why a candidate failed. Each code is transient or permanent (``config``)."""

    BLOCKED = "blocked"
    LOGIN_WALL = "login_wall"
    PAYWALL = "paywall"
    CAPTCHA = "captcha"
    NON_PUBLIC_ADDRESS = "non_public_address"
    BAD_SCHEME = "bad_scheme"
    TOO_MANY_REDIRECTS = "too_many_redirects"
    HTTP_4XX = "http_4xx"
    TYPE_MISMATCH = "type_mismatch"
    SCRIPT_ONLY = "script_only"
    NOT_MCQ_STRUCTURE = "not_mcq_structure"
    NOT_MCQ_PAGE_TYPE = "not_mcq_page_type"
    NO_INSTRUCTION_PHRASE = "no_instruction_phrase"
    NOT_PROBED = "not_probed"
    TIMEOUT = "timeout"
    HTTP_5XX = "http_5xx"
    CONNECTION_ERROR = "connection_error"
    TOO_LARGE_UNSAMPLED = "too_large_unsampled"
    NOT_CLASSIFIED = "not_classified"


class ToolErrorCode(StrEnum):
    """Refusals and failures a tool returns instead of a result (never raised)."""

    QUERY_BUDGET_EXHAUSTED = "query_budget_exhausted"
    COST_BUDGET_EXHAUSTED = "cost_budget_exhausted"
    SKIPPED_KNOWN_QUERY = "skipped_known_query"
    PROVIDERS_UNAVAILABLE = "providers_unavailable"
    NOT_RUN_UNREACHABLE = "not_run_unreachable"
    SKIPPED_DEAD_HOST = "skipped_dead_host"
    SKIPPED_PERMANENT_FAILURE = "skipped_permanent_failure"
    SKIPPED_RECHECK_WAIT = "skipped_recheck_wait"
    TOO_MANY_TOOL_CALLS = "too_many_tool_calls"
    BAD_INPUT = "bad_input"
    UNKNOWN_TOOL = "unknown_tool"
    STORE_ERROR = "store_error"
    TIMEOUT = "timeout"


class Flag(StrEnum):
    LEXICON_MISSING = "lexicon_missing"
    RESTRICTIVE_TERMS = "restrictive_terms"
    MEMORY_UNAVAILABLE = "memory_unavailable"
    REOPENED_AFTER_APPROVAL = "reopened_after_approval"


class RunStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    STOPPED_AT_LIMIT = "stopped_at_limit"
    ESCALATED = "escalated"
    FAILED = "failed"


class StopLimit(StrEnum):
    QUERIES = "queries"
    COST = "cost"
    ITERATIONS = "iterations"


class Provider(StrEnum):
    TAVILY = "tavily"
    BRAVE = "brave"


def _unit(unit: str) -> dict[str, JsonValue]:
    return {"unit": unit}


Seconds = Annotated[float, Field(ge=0, json_schema_extra=_unit("s"))]
Bytes = Annotated[int, Field(ge=0, json_schema_extra=_unit("bytes"))]
Usd = Annotated[float, Field(ge=0, json_schema_extra=_unit("USD"))]
Share = Annotated[float, Field(ge=0, le=1, json_schema_extra=_unit("1"))]
Confidence = Annotated[float, Field(ge=0, le=1)]
Title = Annotated[str, Field(max_length=TITLE_MAX)]
Excerpt = Annotated[str, Field(max_length=EXCERPT_MAX)]
Note = Annotated[str, Field(max_length=NOTE_MAX)]


class Contract(BaseModel):
    """Base for every contract: immutable, and unknown fields are an error."""

    model_config = ConfigDict(extra="forbid", frozen=True)


def clip(text: str, limit: int) -> str:
    """Collapse whitespace and cut to ``limit`` characters (titles, excerpts)."""
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def normalise_url(url: str) -> str:
    """The URL a proposal is keyed by: lower-case scheme and host, no fragment.

    The default port is dropped and an empty path becomes ``/``. The query is kept,
    because it often selects the file.
    """
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    port = parts.port
    if port is not None and (scheme, port) not in {("http", 80), ("https", 443)}:
        host = f"{host}:{port}"
    return urlunsplit((scheme, host, parts.path or "/", parts.query, ""))


def proposal_id_for(normalised_url: str) -> str:
    return "prop_" + hashlib.sha256(normalised_url.encode("utf-8")).hexdigest()[:16]


def domain_of(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


# --- tool results ------------------------------------------------------------------


class ToolError(Contract):
    """What a tool returns when it fails or refuses. It never raises into the graph."""

    error: ToolErrorCode | ReasonCode
    retryable: bool
    detail: Note | None = None


class SearchHit(Contract):
    url: str
    title: Title
    rank: int = Field(ge=1)
    provider: Provider
    query: str = Field(max_length=200)


class SearchResult(Contract):
    hits: tuple[SearchHit, ...]
    provider: Provider
    queries_used: int = Field(ge=0)


class ProbeResult(Contract):
    url: str
    final_url: str
    status: int | None
    content_type: str | None = Field(default=None, max_length=100)
    size_bytes: Bytes | None = None
    etag: str | None = Field(default=None, max_length=200)
    hops: int = Field(ge=0)
    robots_allowed: bool
    login_signal: bool = False
    paywall_signal: bool = False
    captcha_signal: bool = False
    latency_s: Seconds
    reachable: CheckResult
    reason: ReasonCode | None = None
    unchanged: bool = False


class SampleResult(Contract):
    url: str
    kind: Kind
    sample_bytes: Bytes
    pages_read: int = Field(ge=0)
    downloadable: CheckResult
    item_count: int = Field(ge=0)
    choice_counts: tuple[int, ...] = ()
    choices_consistent: bool = False
    question_mark_share: Share = 0.0
    language: str = "unknown"
    language_confidence: Confidence = 0.0
    site_type: SiteType | None = None
    site_type_confidence: Confidence = 0.0
    page_type: PageType | None = None
    page_type_confidence: Confidence = 0.0
    topic: Topic | None = None
    topic_confidence: Confidence = 0.0
    keyword_counts: dict[str, int] = Field(default_factory=dict)
    instruction_hits: int = Field(default=0, ge=0)
    key_status: KeyStatus = KeyStatus.NOT_FOUND
    key_evidence: str | None = None
    content_hash: str | None = None
    mcq: CheckResult = CheckResult.NOT_RUN
    reason: ReasonCode | None = None
    lexicon_missing: bool = False
    cost_usd: Usd = 0.0
    latency_s: Seconds = 0.0
    title: Title = ""


class TermsResult(Contract):
    domain: str
    terms_url: str | None
    excerpt: Excerpt = ""
    restrictive: bool = False


# --- proposals ---------------------------------------------------------------------


class Labels(Contract):
    """FR-019. Exam name and year are as stated on the page, never guessed."""

    origin: Origin
    kind: Kind | None = None
    language: str = "unknown"
    language_confidence: Confidence = 0.0
    site_type: SiteType | None = None
    site_type_confidence: Confidence = 0.0
    page_type: PageType | None = None
    page_type_confidence: Confidence = 0.0
    topic: Topic | None = None
    topic_confidence: Confidence = 0.0
    keyword_counts: dict[str, int] = Field(default_factory=dict)
    exam_name_stated: Title | None = None
    exam_year_stated: int | None = Field(default=None, ge=1900, le=2100)
    key_status: KeyStatus | None = None
    terms_url: str | None = None
    terms_excerpt: Excerpt = ""
    terms_restrictive: bool = False
    flags: tuple[Flag, ...] = ()

    def without_confidences(self) -> dict[str, object]:
        """The label values that count as material (FR-021a): confidences excluded."""
        data = self.model_dump(mode="json")
        return {k: v for k, v in data.items() if not k.endswith("_confidence")}


class ReachableCheck(Contract):
    result: CheckResult
    status: int | None = None
    latency_s: Seconds | None = None
    size_bytes: Bytes | None = None


class DownloadableCheck(Contract):
    result: CheckResult = CheckResult.NOT_RUN
    sample_bytes: Bytes | None = None


class McqCheck(Contract):
    result: CheckResult = CheckResult.NOT_RUN
    item_count: int | None = Field(default=None, ge=0)
    page_type_confidence: Confidence | None = None
    instruction_hits: int | None = Field(default=None, ge=0)


class KeyCheck(Contract):
    result: CheckResult = CheckResult.NOT_RUN
    key_status: KeyStatus | None = None
    evidence: str | None = None

    @model_validator(mode="after")
    def _never_failed(self) -> KeyCheck:
        if self.result is CheckResult.FAILED:
            raise ValueError("the answer-key check never fails a candidate (FR-015)")
        return self


class Checks(Contract):
    reachable: ReachableCheck
    downloadable: DownloadableCheck = DownloadableCheck()
    mcq: McqCheck = McqCheck()
    key_located: KeyCheck = KeyCheck()

    @property
    def run_count(self) -> int:
        results = (
            self.reachable.result,
            self.downloadable.result,
            self.mcq.result,
            self.key_located.result,
        )
        return sum(r is not CheckResult.NOT_RUN for r in results)

    @property
    def gates_passed(self) -> bool:
        """FR-016: checks 1-3 decide; the key check never does."""
        return all(
            r is CheckResult.PASSED
            for r in (self.reachable.result, self.downloadable.result, self.mcq.result)
        )


class Proposal(Contract):
    proposal_id: str = Field(pattern=r"^prop_[0-9a-f]{16}$")
    revision: int = Field(ge=1)
    url: str
    normalised_url: str
    domain: str
    run_id: str
    content_hash: str | None
    labels: Labels
    checks: Checks
    validation_status: Literal[ValidationStatus.PASSED, ValidationStatus.FAILED]
    reason: ReasonCode | None
    reason_class: Literal["transient", "permanent"] | None
    observed_at: datetime

    @model_validator(mode="after")
    def _rules(self) -> Proposal:
        passed = self.validation_status is ValidationStatus.PASSED
        if passed != self.checks.gates_passed:
            raise ValueError("passed exactly when reachable, downloadable and mcq pass")
        if passed != (self.reason is None) or (self.reason is None) != (
            self.reason_class is None
        ):
            raise ValueError("a failed proposal has a reason and its class")
        if self.checks.reachable.result is not CheckResult.PASSED:
            later = (
                self.checks.downloadable.result,
                self.checks.mcq.result,
                self.checks.key_located.result,
            )
            if any(r is not CheckResult.NOT_RUN for r in later):
                raise ValueError("an unreachable candidate has checks 2-4 not_run")
            if self.content_hash is not None:
                raise ValueError("an unreachable candidate has no content hash")
        if proposal_id_for(self.normalised_url) != self.proposal_id:
            raise ValueError("proposal_id must be derived from normalised_url")
        return self

    def material_fingerprint(self) -> str:
        """SHA-256 of what counts as a material change (FR-021a, data-model.md)."""
        material = {
            "content_hash": self.content_hash,
            "validation_status": str(self.validation_status),
            "reason": self.reason,
            "reason_class": self.reason_class,
            "key_status": self.labels.key_status,
            "labels": self.labels.without_confidences(),
        }
        text = json.dumps(material, sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --- hand-off ticket (constitution "Hand-off ticket"; contracts/handoff-ticket.md) ----


class TicketInput(Contract):
    url: str
    query: str | None = Field(default=None, max_length=200)
    provider: Provider | None = None
    found_on: str | None = None


class TicketOutput(Contract):
    proposal_id: str
    revision: int = Field(ge=1)
    normalised_url: str
    content_hash: str | None
    reason: ReasonCode | None
    reason_class: Literal["transient", "permanent"] | None
    key_status: KeyStatus | None
    flags: tuple[Flag, ...]
    checks: Checks
    cost_usd: Usd


class Completeness(Contract):
    checks_run: int = Field(ge=0, le=4)
    share: Share
    notes: tuple[Note, ...] = ()


class HandoffTicket(Contract):
    owner: Literal["crawler"] = "crawler"
    run_id: str
    validation_status: ValidationStatus
    input: TicketInput
    output: TicketOutput
    completeness: Completeness

    @model_validator(mode="after")
    def _consistent(self) -> HandoffTicket:
        if self.completeness.checks_run != self.output.checks.run_count:
            raise ValueError("checks_run counts the checks that ran")
        return self

    def for_storage(self) -> HandoffTicket:
        """A stored ticket is final: ``pending`` exists only until the write commits."""
        if self.validation_status is ValidationStatus.PENDING:
            raise ValueError(
                "a stored hand-off ticket is passed or failed, never pending"
            )
        return self


# --- tool inputs: what the LLM may send (anything else is refused) -------------------


class WebSearchInput(Contract):
    query: str = Field(min_length=2, max_length=200)
    provider: Provider | None = None
    lang: str | None = Field(default=None, pattern=r"^[a-z]{2,3}(-[A-Za-z0-9]{2,8})?$")


class UrlInput(Contract):
    url: str = Field(min_length=8, max_length=2000)


class TermsInput(Contract):
    domain: str = Field(min_length=3, max_length=253, pattern=r"^[A-Za-z0-9.-]+$")


class WriteProposalInput(Contract):
    """No check result, status or reason: those come from the run's records (R4)."""

    url: str = Field(min_length=8, max_length=2000)
    origin: Origin
    exam_name_stated: Title | None = None
    exam_year_stated: int | None = Field(default=None, ge=1900, le=2100)
