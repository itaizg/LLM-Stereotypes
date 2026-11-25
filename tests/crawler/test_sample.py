"""``sample_content``: checks 2-4 on a bounded sample (FR-012 - FR-018, R13)."""

from __future__ import annotations

import asyncio

import pytest
from crawler_fakes import (
    FakeClassifier,
    FakeDetector,
    FakeResolver,
    ReplayWeb,
    VirtualClock,
    make_services,
    make_state,
)

from crawler.contracts import (
    CheckResult,
    KeyStatus,
    Kind,
    ProbeResult,
    ReasonCode,
    SampleResult,
    ToolError,
    ToolErrorCode,
    UrlInput,
    normalise_url,
)
from crawler.evals.replay import minimal_pdf
from crawler.lexicon import load_lexicon
from crawler.runstate import RunState
from crawler.sample import (
    Page,
    detect_structure,
    key_status,
    read_page,
    sample_content,
)

URL = "https://exams.example/p"

ITEMS = "\n".join(
    f"{n}. Which blorp number {n} is quindle?\nA) one\nB) two\nC) three\nD) four"
    for n in range(1, 7)
)


def passed_probe(state: RunState, url: str = URL) -> None:
    state.probes[normalise_url(url)] = ProbeResult(
        url=url,
        final_url=url,
        status=200,
        hops=0,
        robots_allowed=True,
        latency_s=0.1,
        reachable=CheckResult.PASSED,
    )


def sample(
    web: ReplayWeb,
    state: RunState,
    *,
    classifier: FakeClassifier | None = None,
    detector: FakeDetector | None = None,
) -> SampleResult | ToolError:
    async def go() -> SampleResult | ToolError:
        async with web.client() as client:
            services = make_services(
                client,
                FakeResolver(),
                VirtualClock(),
                classifier=classifier,
                detector=detector,
            )
            from crawler.probe import RobotsCache

            return await sample_content(
                UrlInput(url=URL), state, services, RobotsCache()
            )

    return asyncio.run(go())


def html(body: str, title: str = "Practice test") -> str:
    paragraphs = "".join(f"<p>{line}</p>" for line in body.splitlines())
    return f"<html><head><title>{title}</title></head><body>{paragraphs}</body></html>"


def test_no_probe_means_no_request(web: ReplayWeb) -> None:
    result = sample(web, make_state())
    assert isinstance(result, ToolError)
    assert result.error is ToolErrorCode.NOT_RUN_UNREACHABLE
    assert web.log == []


def test_a_practice_page_passes_every_check(web: ReplayWeb) -> None:
    web.add(
        URL,
        html("Choose the best answer.\n" + ITEMS + "\nAnswer: B"),
        headers={"content-type": "text/html"},
    )
    state = make_state()
    passed_probe(state)
    result = sample(web, state)
    assert isinstance(result, SampleResult)
    assert (result.kind, result.downloadable, result.mcq) == (
        Kind.WEB_PAGE,
        CheckResult.PASSED,
        CheckResult.PASSED,
    )
    assert result.item_count == 6 and result.choices_consistent
    assert result.question_mark_share == 1.0
    assert result.key_status is KeyStatus.INLINE
    assert result.content_hash and result.content_hash.startswith("sha256:")
    assert "blorp" not in result.model_dump_json()  # no text comes back (FR-020)


def test_a_pdf_with_a_mismatched_type_fails(web: ReplayWeb) -> None:
    web.add(URL, minimal_pdf(["x"]), headers={"content-type": "text/html"})
    state = make_state()
    passed_probe(state)
    result = sample(web, state)
    assert (
        isinstance(result, SampleResult) and result.reason is ReasonCode.TYPE_MISMATCH
    )


def test_a_pdf_is_read_up_to_three_pages(web: ReplayWeb) -> None:
    lines = ["Choose the best answer.", *ITEMS.splitlines()]
    web.add(URL, minimal_pdf(lines), headers={"content-type": "application/pdf"})
    state = make_state()
    passed_probe(state)
    result = sample(web, state)
    assert isinstance(result, SampleResult)
    assert (result.kind, result.pages_read, result.mcq) == (
        Kind.PDF,
        1,
        CheckResult.PASSED,
    )


def test_an_unparseable_pdf_is_a_transient_failure(web: ReplayWeb) -> None:
    web.add(
        URL, b"%PDF-1.7 truncated garbage", headers={"content-type": "application/pdf"}
    )
    state = make_state()
    passed_probe(state)
    result = sample(web, state)
    assert (
        isinstance(result, SampleResult)
        and result.reason is ReasonCode.TOO_LARGE_UNSAMPLED
    )


def test_a_script_rendered_page_is_script_only(web: ReplayWeb) -> None:
    web.add(URL, '<html><script src="a.js"></script><div id="root"></div></html>')
    state = make_state()
    passed_probe(state)
    result = sample(web, state)
    assert isinstance(result, SampleResult) and result.reason is ReasonCode.SCRIPT_ONLY


def test_a_script_page_linking_an_exam_file_is_downloadable(web: ReplayWeb) -> None:
    web.add(
        URL,
        '<html><script src="a.js"></script><a href="/exam-2024.pdf">Exam 2024'
        "</a></html>",
    )
    state = make_state()
    passed_probe(state)
    result = sample(web, state)
    assert (
        isinstance(result, SampleResult) and result.downloadable is CheckResult.PASSED
    )


@pytest.mark.parametrize(
    ("page_type", "confidence", "reason"),
    [
        ("article", 0.95, ReasonCode.NOT_MCQ_PAGE_TYPE),
        ("past_paper", 0.6, ReasonCode.NOT_MCQ_PAGE_TYPE),
    ],
)
def test_the_page_type_gate(
    web: ReplayWeb, page_type: str, confidence: float, reason: ReasonCode
) -> None:
    web.add(URL, html("Choose the best answer.\n" + ITEMS))
    state = make_state()
    passed_probe(state)
    result = sample(web, state, classifier=FakeClassifier(page_type, confidence))
    assert isinstance(result, SampleResult) and result.reason is reason


def test_an_instruction_phrase_is_needed_when_a_lexicon_exists(web: ReplayWeb) -> None:
    web.add(URL, html(ITEMS))
    state = make_state()
    passed_probe(state)
    result = sample(web, state)
    assert isinstance(result, SampleResult)
    assert result.reason is ReasonCode.NO_INSTRUCTION_PHRASE


def test_an_unknown_language_is_flagged_not_failed(web: ReplayWeb) -> None:
    web.add(URL, html(ITEMS))
    state = make_state()
    passed_probe(state)
    result = sample(web, state, detector=FakeDetector("en", 0.6))
    assert isinstance(result, SampleResult)
    assert (result.language, result.lexicon_missing, result.mcq) == (
        "unknown",
        True,
        CheckResult.PASSED,
    )


def test_the_classifier_is_skipped_over_its_cost_cap(web: ReplayWeb) -> None:
    web.add(URL, html("Choose the best answer.\n" + ITEMS))
    state = make_state(classifier_max_cost_usd=0.000001)
    passed_probe(state)
    classifier = FakeClassifier()
    result = sample(web, state, classifier=classifier)
    assert isinstance(result, SampleResult)
    assert (result.mcq, result.reason) == (
        CheckResult.NOT_RUN,
        ReasonCode.NOT_CLASSIFIED,
    )
    assert classifier.calls == []


def test_the_classifier_cost_is_metered(web: ReplayWeb) -> None:
    web.add(URL, html("Choose the best answer.\n" + ITEMS))
    state = make_state()
    passed_probe(state)
    result = sample(web, state)
    assert isinstance(result, SampleResult)
    expected = state.config.classifier_model.cost_usd(1_000, 100)
    assert result.cost_usd == pytest.approx(expected) and state.reserved_usd == 0.0
    assert state.cost_usd == pytest.approx(expected)


@pytest.mark.parametrize(
    ("text", "items", "consistent"),
    [
        (ITEMS, 6, True),
        ("1. Bring pencils.\n2. Sleep well.\n3. Eat.\n4. Arrive.\n5. Read.", 0, False),
        (
            "\n".join(f"{n}. Q{n}?\n① a\n② b\n③ c\n④ d\n⑤ e" for n in range(1, 6)),
            5,
            True,
        ),
        (
            "\n".join(f"{n}. Q{n}?\n(A) a (B) b (C) c (D) d" for n in range(1, 6)),
            5,
            True,
        ),
        ("\n".join(f"{n}. Q{n}?\nA) a\nB) b" for n in range(1, 6)), 0, False),
        (
            "\n".join(
                f"{n}. Q{n}?\nA) a\nB) b\nC) c" + ("\nD) d" if n % 2 else "")
                for n in range(1, 7)
            ),
            6,
            False,
        ),
        (
            "\n".join(f"{n}. Q{n}?\nא. a\nב. b\nג. c\nד. d" for n in range(1, 6)),
            5,
            True,
        ),
        (
            "\n".join(
                f"{n}. Q{n}?\n\uff21. a\n\uff22. b\n\uff23. c\n\uff24. d"
                for n in range(1, 6)
            ),
            5,
            True,
        ),
    ],
)
def test_structure_detection(text: str, items: int, consistent: bool) -> None:
    structure = detect_structure(text)
    assert structure.item_count == items
    assert structure.consistent is consistent


def test_key_status_order(web: ReplayWeb) -> None:
    import httpx

    from crawler.netsafe import SafeResponse

    def page(body: str) -> Page:
        return read_page(
            SafeResponse(
                URL,
                URL,
                200,
                httpx.Headers({"content-type": "text/html"}),
                body.encode(),
                False,
                0,
                0.0,
                None,
            )
        )

    lexicon = load_lexicon()
    linked = page('<a href="/files/gabarito.pdf">Gabarito</a>')
    assert key_status(linked, lexicon, URL, set())[0] is KeyStatus.LINKED_FILE
    likely = page('<a href="/answers">Answer key</a>')
    assert key_status(likely, lexicon, URL, set()) == (
        KeyStatus.LIKELY,
        "https://exams.example/answers",
    )
    plain = page("<p>nothing here</p>")
    assert key_status(plain, lexicon, URL, set())[0] is KeyStatus.NOT_FOUND
    assert key_status(plain, lexicon, URL, {"exams.example"})[0] is KeyStatus.LIKELY
