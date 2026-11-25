"""``sample_content``: checks 2-4 on a bounded sample (FR-011, FR-013-FR-018, R13).

The tool runs only for a URL with a passing probe in this run (FR-012, research R4). It
reads at most 2 MB, or the first 3 pages of a PDF, and returns counts, labels and
hashes,
never the text (FR-020):

- **Downloadable (check 2):** a PDF's ``%PDF-`` signature and declared content type
  agree; a web page's items are in the served HTML, or it links a file with exam or key
  terms. A page that renders its content with scripts is ``script_only``.
- **Multiple choice (check 3):** at least 5 items, each a stem followed directly by 3-5
  choices, with consistent choice counts; a page type among the exam types with
  confidence of at least 0.7; and an instruction phrase when the language has a lexicon.
- **Answer key (check 4):** ``linked_file``, ``inline``, ``likely`` or ``not_found``,
  with evidence. It never fails a candidate.

The classifier is called only when its worst-case cost fits under $0.02 and the run's
budget. Otherwise check 3 is ``not_run`` with the transient reason ``not_classified``,
so
a later run re-checks the URL (FR-016a).
"""

from __future__ import annotations

import hashlib
import io
import re
import unicodedata
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote, urljoin, urlsplit

from bs4 import BeautifulSoup, Tag

from crawler.classify import SYSTEM, estimate_input_tokens, prompt_for
from crawler.config import (
    MAX_CHOICES,
    MIN_CHOICES,
    MIN_ITEMS,
    MIN_LANGUAGE_CONFIDENCE,
    MIN_PAGE_TYPE_CONFIDENCE,
    SAMPLE_MAX_BYTES,
    SAMPLE_MAX_PDF_PAGES,
    SAMPLE_RETRIES,
    SAMPLE_TIMEOUT_S,
)
from crawler.contracts import (
    EXAM_PAGE_TYPES,
    TITLE_MAX,
    CheckResult,
    KeyStatus,
    Kind,
    PageType,
    ReasonCode,
    SampleResult,
    SiteType,
    ToolError,
    ToolErrorCode,
    Topic,
    UrlInput,
    clip,
    domain_of,
    normalise_url,
)
from crawler.lexicon import Lexicon, normalise
from crawler.netsafe import Refused, SafeResponse
from crawler.probe import RobotsCache
from crawler.runstate import Classification, RunState, Sampled, Services
from pipeline.web import decode_html

DESCRIPTION = (
    "Read a bounded sample of a reachable URL and measure whether it looks like a "
    "multiple-choice exam source. Returns counts, labels and hashes — never the text."
)

TRANSIENT = frozenset(
    {ReasonCode.TIMEOUT, ReasonCode.HTTP_5XX, ReasonCode.CONNECTION_ERROR}
)
SCRIPT_TEXT_MIN = 400

# --- structure (R13) ------------------------------------------------------------------

_STEM = re.compile(
    r"^\s*(?:(?:question|questão|quest[aã]o|문항|問題|問|第)\s*)?"
    r"(?:\d{1,3}|[\uff10-\uff19]{1,3})\s*(?:[.)\uff0e、:\uff1a]|번|题|問)?\s+\S"
    r"|^\s*(?:question|questão)\s+\d{1,3}\b",
    re.I,
)
_CHOICE_MARKERS = (
    r"\(?[A-Ea-e][).:]",  # A) (A) A. A:
    r"\([A-Ea-e]\)",
    r"[①②③④⑤]",
    r"[ㄱㄴㄷㄹㅁ][.)]",
    r"[\uff21-\uff25][.)\uff0e]",
    r"[אבגדה][.)]",
    r"\(?[1-5]\)",
)
_CHOICE = re.compile(rf"^\s*(?:{'|'.join(_CHOICE_MARKERS)})\s*\S")
_INLINE_CHOICE = re.compile(rf"(?:^|\s)(?:{'|'.join(_CHOICE_MARKERS)})\s*\S")
_ANSWER_MARKER = re.compile(
    r"(?:answer|resposta|gabarito|정답|答案|正解|解答|תשובה)\s*[:\uff1a]\s*"
    r"(?:[A-Ea-e①②③④⑤1-5])\b",
    re.I,
)
_CHARSET = re.compile(r"charset=\"?([\w.:-]+)", re.I)
_SCRIPT_APP = re.compile(
    rb"__NEXT_DATA__|<div[^>]+id=[\"'](?:root|app|__next)[\"'][^>]*>\s*</div>|"
    rb"ng-app|data-reactroot",
    re.I,
)


_CIRCLED = str.maketrans({c: f"({n})" for n, c in enumerate("①②③④⑤", start=1)})


@dataclass(frozen=True)
class Structure:
    item_count: int
    choice_counts: tuple[int, ...]
    consistent: bool
    question_mark_share: float


def _lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


def detect_structure(text: str) -> Structure:
    """Count stems followed directly by a block of 3-5 choices."""
    # NFKC turns circled numbers into plain digits, which would read as stems.
    lines = _lines(unicodedata.normalize("NFKC", text.translate(_CIRCLED)))
    counts: list[int] = []
    question_marks = 0
    i = 0
    while i < len(lines):
        if not _STEM.match(lines[i]) or _CHOICE.match(lines[i]):
            i += 1
            continue
        stem = lines[i]
        j = i + 1
        # A stem may wrap onto continuation lines before its choices.
        while j < len(lines) and j - i <= 3 and not _CHOICE.match(lines[j]):
            if _STEM.match(lines[j]):
                break
            stem += " " + lines[j]
            j += 1
        choices = 0
        while j < len(lines) and _CHOICE.match(lines[j]):
            choices += max(1, len(_INLINE_CHOICE.findall(lines[j])))
            j += 1
        if choices == 0:
            inline = len(_INLINE_CHOICE.findall(stem))
            choices = inline if inline >= MIN_CHOICES else 0
        if MIN_CHOICES <= choices <= MAX_CHOICES:
            counts.append(choices)
            if stem.rstrip().endswith(("?", "\uff1f")) or "?" in stem:
                question_marks += 1
        i = max(j, i + 1)
    consistent = bool(counts) and len(set(counts)) == 1
    share = question_marks / len(counts) if counts else 0.0
    return Structure(len(counts), tuple(counts), consistent, round(share, 3))


# --- language -------------------------------------------------------------------------


class LinguaDetector:
    """Offline language detection with a confidence (``lingua``)."""

    def __init__(self) -> None:
        from lingua import LanguageDetectorBuilder

        self._detector = (
            LanguageDetectorBuilder.from_all_languages()
            .with_preloaded_language_models()
            .build()
        )

    def detect(self, text: str) -> tuple[str, float]:
        values = self._detector.compute_language_confidence_values(text[:5_000])
        if not values:
            return "unknown", 0.0
        top = values[0]
        return top.language.iso_code_639_1.name.lower(), float(top.value)


# --- text and links ------------------------------------------------------------------


@dataclass(frozen=True)
class Page:
    kind: Kind
    text: str
    title: str
    links: tuple[tuple[str, str], ...]  # (absolute url, anchor text)
    pages_read: int
    parsed: bool
    script_only: bool


def _pdf_text(body: bytes) -> tuple[str, int, bool]:
    import pdfplumber

    try:
        with pdfplumber.open(io.BytesIO(body)) as pdf:
            pages = pdf.pages[:SAMPLE_MAX_PDF_PAGES]
            text = "\n".join(page.extract_text() or "" for page in pages)
            return text, len(pages), True
    except Exception:
        return "", 0, False


def _html_page(response: SafeResponse) -> Page:
    declared = _CHARSET.search(response.headers.get("content-type") or "")
    html = decode_html(response.body, declared.group(1) if declared else None)
    soup = BeautifulSoup(html, "html.parser")
    title_tag = soup.find("title")
    title = title_tag.get_text(" ", strip=True) if isinstance(title_tag, Tag) else ""
    links: list[tuple[str, str]] = []
    for anchor in soup.find_all("a", href=True):
        if not isinstance(anchor, Tag):
            continue
        href = anchor.get("href")
        if isinstance(href, str) and href.strip():
            absolute = urljoin(response.final_url, href.strip())
            if urlsplit(absolute).scheme in {"http", "https"}:
                links.append((absolute, anchor.get_text(" ", strip=True)))
    # A <script> written inside <title> is text, not code: html.parser makes it a tag.
    has_scripts = any(
        node.find_parent("title") is None for node in soup.find_all("script")
    )
    for node in soup(["script", "style", "noscript", "template"]):
        node.decompose()
    text = soup.get_text("\n")
    visible = len(" ".join(text.split()))
    script_only = has_scripts and (
        visible < SCRIPT_TEXT_MIN or bool(_SCRIPT_APP.search(response.body))
    )
    return Page(Kind.WEB_PAGE, text, title, tuple(links), 1, True, script_only)


def read_page(response: SafeResponse) -> Page:
    content_type = (response.headers.get("content-type") or "").lower()
    if response.body.startswith(b"%PDF-") or "pdf" in content_type:
        text, pages, parsed = _pdf_text(response.body)
        return Page(Kind.PDF, text, "", (), pages, parsed, False)
    return _html_page(response)


def pdf_types_agree(response: SafeResponse) -> bool:
    content_type = (response.headers.get("content-type") or "").lower()
    signature = response.body.startswith(b"%PDF-")
    declared = "pdf" in content_type or "octet-stream" in content_type
    return signature and declared


def content_hash(text: str, body: bytes) -> str:
    """SHA-256 of the normalised sample text (R13), or of the bytes when it has none."""
    folded = " ".join(unicodedata.normalize("NFKC", text).split())
    data = folded.encode("utf-8") if folded else body
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _filename(url: str) -> str:
    return unquote(urlsplit(url).path.rsplit("/", 1)[-1])


def key_status(
    page: Page, lexicon: Lexicon, url: str, answer_key_domains: set[str]
) -> tuple[KeyStatus, str | None]:
    """R13 order: linked file, inline markers, same-site answer-key page, none."""
    same_site: str | None = None
    for link, anchor in page.links:
        label = f"{anchor} {_filename(link).replace('_', ' ').replace('-', ' ')}"
        if lexicon.any_language_counts(label)["key"] == 0:
            continue
        if _filename(link).lower().endswith((".pdf", ".doc", ".docx", ".xls", ".xlsx")):
            return KeyStatus.LINKED_FILE, link
        if domain_of(link) == domain_of(url) and same_site is None:
            same_site = link
    if _ANSWER_MARKER.search(unicodedata.normalize("NFKC", page.text)):
        return KeyStatus.INLINE, None
    if same_site is not None:
        return KeyStatus.LIKELY, same_site
    if domain_of(url) in answer_key_domains:
        return KeyStatus.LIKELY, None
    return KeyStatus.NOT_FOUND, None


def linked_exam_file(page: Page, lexicon: Lexicon) -> bool:
    for link, anchor in page.links:
        if not _filename(link).lower().endswith(".pdf"):
            continue
        counts = lexicon.any_language_counts(f"{anchor} {_filename(link)}")
        if counts["exam"] or counts["key"]:
            return True
    return False


# --- the tool -------------------------------------------------------------------------


async def _fetch(services: Services, url: str, robots: RobotsCache) -> SafeResponse:
    last: Refused | None = None
    for _attempt in range(SAMPLE_RETRIES + 1):
        try:
            response = await services.safe.get(
                url,
                max_bytes=SAMPLE_MAX_BYTES,
                timeout_s=SAMPLE_TIMEOUT_S,
                allow=robots.checker(services.safe),
            )
        except Refused as refused:
            if refused.reason not in TRANSIENT:
                raise
            last = refused
            continue
        if response.status >= 500:
            last = Refused(ReasonCode.HTTP_5XX, str(response.status))
            continue
        return response
    raise last if last is not None else Refused(ReasonCode.CONNECTION_ERROR)


def _mcq(
    structure: Structure,
    classification: Classification | None,
    instruction_hits: int,
    lexicon_missing: bool,
) -> tuple[CheckResult, ReasonCode | None]:
    if (
        structure.item_count < MIN_ITEMS
        or not structure.consistent
        or not all(MIN_CHOICES <= c <= MAX_CHOICES for c in structure.choice_counts)
    ):
        return CheckResult.FAILED, ReasonCode.NOT_MCQ_STRUCTURE
    if classification is None:
        return CheckResult.NOT_RUN, ReasonCode.NOT_CLASSIFIED
    page_type = classification.page_type
    if (
        page_type is None
        or PageType(page_type) not in EXAM_PAGE_TYPES
        or classification.page_type_confidence < MIN_PAGE_TYPE_CONFIDENCE
    ):
        return CheckResult.FAILED, ReasonCode.NOT_MCQ_PAGE_TYPE
    if not lexicon_missing and instruction_hits < 1:
        return CheckResult.FAILED, ReasonCode.NO_INSTRUCTION_PHRASE
    return CheckResult.PASSED, None


async def _classify(
    state: RunState, services: Services, title: str, url: str, text: str
) -> tuple[Classification | None, float]:
    model = state.config.classifier_model
    worst = model.worst_case_usd(estimate_input_tokens(title, url, text))
    if worst > state.config.classifier_max_cost_usd:
        return None, 0.0
    if not await state.reserve_cost(worst):
        return None, 0.0
    tracing = services.tracing
    with tracing.span("llm.classify") as span:
        try:
            labels = await services.classifier.classify(title, url, text)
        except Exception as error:
            await state.settle_cost(worst, worst)
            tracing.mark_failed(span, type(error).__name__)
            raise
        cost = model.cost_usd(labels.input_tokens, labels.output_tokens)
        await state.settle_cost(worst, cost)
        tracing.generation(
            span,
            model.model,
            labels.input_tokens,
            labels.output_tokens,
            cost,
            f"{SYSTEM}\n\n{prompt_for(title, url, text)}",
            f"site_type={labels.site_type} page_type={labels.page_type} "
            f"topic={labels.topic}",
        )
    return labels, cost


async def sample_content(
    args: UrlInput, state: RunState, services: Services, robots: RobotsCache
) -> SampleResult | ToolError:
    norm = normalise_url(args.url)
    probe = state.probes.get(norm)
    if probe is None or probe.reachable is not CheckResult.PASSED:
        return ToolError(error=ToolErrorCode.NOT_RUN_UNREACHABLE, retryable=False)
    url = probe.final_url
    try:
        response = await _fetch(services, url, robots)
    except Refused as refused:
        return ToolError(error=refused.reason, retryable=refused.reason in TRANSIENT)
    page = read_page(response)
    base: dict[str, Any] = {
        "url": args.url,
        "kind": page.kind,
        "sample_bytes": len(response.body),
        "pages_read": page.pages_read,
        "latency_s": response.latency_s,
        "title": clip(page.title, TITLE_MAX),
    }

    def record(result: SampleResult, text: str) -> SampleResult:
        state.samples[norm] = Sampled(
            result=result, observed_at=response.observed_at, folded_text=normalise(text)
        )
        return result

    if page.kind is Kind.PDF:
        if not pdf_types_agree(response):
            return record(
                SampleResult(
                    **base,
                    downloadable=CheckResult.FAILED,
                    item_count=0,
                    reason=ReasonCode.TYPE_MISMATCH,
                ),
                "",
            )
        if not page.parsed:
            return record(
                SampleResult(
                    **base,
                    downloadable=CheckResult.FAILED,
                    item_count=0,
                    reason=ReasonCode.TOO_LARGE_UNSAMPLED,
                ),
                "",
            )
    elif page.script_only and not linked_exam_file(page, services.lexicon):
        return record(
            SampleResult(
                **base,
                downloadable=CheckResult.FAILED,
                item_count=0,
                reason=ReasonCode.SCRIPT_ONLY,
            ),
            page.title,
        )
    structure = detect_structure(page.text)
    language, confidence = (
        services.detector.detect(page.text)
        if page.text.strip()
        else (
            "unknown",
            0.0,
        )
    )
    if confidence < MIN_LANGUAGE_CONFIDENCE:
        language = "unknown"
    lexicon_missing = language == "unknown" or not services.lexicon.has(language)
    counts = services.lexicon.keyword_counts(page.text, language)
    status, evidence = key_status(page, services.lexicon, url, state.answer_key_domains)
    classification: Classification | None = None
    cost = 0.0
    if structure.item_count >= MIN_ITEMS and structure.consistent:
        classification, cost = await _classify(
            state, services, page.title, url, page.text
        )
    mcq, reason = _mcq(
        structure, classification, counts["instruction"], lexicon_missing
    )
    if (
        classification is not None
        and classification.page_type == PageType.ANSWER_KEY
        and classification.page_type_confidence >= MIN_PAGE_TYPE_CONFIDENCE
    ):
        state.answer_key_domains.add(domain_of(url))
    result = SampleResult(
        **base,
        downloadable=CheckResult.PASSED,
        item_count=structure.item_count,
        choice_counts=structure.choice_counts[:50],
        choices_consistent=structure.consistent,
        question_mark_share=structure.question_mark_share,
        language=language,
        language_confidence=round(confidence, 3),
        site_type=SiteType(classification.site_type)
        if classification and classification.site_type
        else None,
        site_type_confidence=classification.site_type_confidence
        if classification
        else 0.0,
        page_type=PageType(classification.page_type)
        if classification and classification.page_type
        else None,
        page_type_confidence=classification.page_type_confidence
        if classification
        else 0.0,
        topic=Topic(classification.topic)
        if classification and classification.topic
        else None,
        topic_confidence=classification.topic_confidence if classification else 0.0,
        keyword_counts=counts,
        instruction_hits=counts["instruction"],
        key_status=status,
        key_evidence=evidence,
        content_hash=content_hash(page.text, response.body),
        mcq=mcq,
        reason=reason,
        lexicon_missing=lexicon_missing,
        cost_usd=round(cost, 6),
    )
    return record(result, f"{page.title}\n{page.text}")
