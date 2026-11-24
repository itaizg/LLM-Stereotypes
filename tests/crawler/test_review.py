"""The review page (FR-023a, FR-024, Story 2): groups, flags, escaping, CSP."""

from __future__ import annotations

import base64
import hashlib
import json
import re
from pathlib import Path

import psycopg
import pytest
from crawler_fakes import add_proposal, add_run

from crawler import cli
from crawler.review import SCRIPT, STYLE, load_groups, render, write_review

RUN_ID = "cr_20261123T101500_a3f9"
INJECTION = "<script>alert('x')</script><img src=x onerror=alert(1)>"


@pytest.fixture
def seeded(crawler_db: psycopg.Connection) -> psycopg.Connection:
    """12 passed groups (one with three mirrors) and 30 failed proposals."""
    add_run(crawler_db)
    add_proposal(crawler_db, "https://mirror-a.test/p.pdf", digest="sha256:same")
    add_proposal(
        crawler_db,
        "https://board.test/p.pdf",
        digest="sha256:same",
        origin="official",
        terms="Scraping is prohibited.",
        flags=("restrictive_terms",),
    )
    add_proposal(crawler_db, "https://mirror-b.test/p.pdf", digest="sha256:same")
    for n in range(10):
        add_proposal(crawler_db, f"https://site{n}.test/exam.pdf")
    add_proposal(
        crawler_db,
        "https://evil.test/x",
        title=INJECTION,
        language="xx",
        flags=("lexicon_missing",),
    )
    for n in range(30):
        add_proposal(crawler_db, f"https://bad{n}.test/x", status="failed")
    return crawler_db


def test_only_passed_groups_appear_one_row_each(seeded: psycopg.Connection) -> None:
    groups = load_groups(seeded, RUN_ID)
    assert len(groups) == 12
    page = render(RUN_ID, groups)
    assert page.count("<tr data-decision=") == 12
    assert "bad0.test" not in page


def test_mirrors_group_under_the_official_primary(seeded: psycopg.Connection) -> None:
    group = next(
        g for g in load_groups(seeded, RUN_ID) if g.content_hash == "sha256:same"
    )
    assert group.primary.url == "https://board.test/p.pdf"
    assert [m.origin for m in group.members] == [
        "official",
        "third_party",
        "third_party",
    ]
    page = render(RUN_ID, [group])
    assert page.count('class="alt"') == 2


def test_flags_terms_and_checks_are_shown(seeded: psycopg.Connection) -> None:
    page = render(RUN_ID, load_groups(seeded, RUN_ID))
    assert '<span class="flag">restrictive_terms</span>' in page
    assert '<span class="flag">lexicon_missing</span>' in page
    assert "Scraping is prohibited." in page
    assert "mcq: passed (9 items)" in page


def test_crawled_text_is_inert(seeded: psycopg.Connection) -> None:
    """Story 2, scenario 3: markup in crawled text is escaped, never executed."""
    page = render(RUN_ID, load_groups(seeded, RUN_ID))
    assert INJECTION not in page
    assert "&lt;script&gt;alert(&#x27;x&#x27;)&lt;/script&gt;" in page
    assert page.count("<script") == 2  # the data block and the page's own script
    assert 'href="javascript:' not in page
    data = re.search(r'id="review-data">(.*?)</script>', page, re.S)
    assert data is not None and "<" not in data.group(1)


def sha(source: str) -> str:
    return (
        "sha256-" + base64.b64encode(hashlib.sha256(source.encode()).digest()).decode()
    )


def test_the_policy_allows_only_the_pages_own_code(seeded: psycopg.Connection) -> None:
    page = render(RUN_ID, load_groups(seeded, RUN_ID))
    policy = re.search(r'Content-Security-Policy" content="([^"]+)"', page)
    assert policy is not None
    assert policy.group(1).startswith("default-src 'none'")
    assert f"'{sha(SCRIPT)}'" in policy.group(1) and f"'{sha(STYLE)}'" in policy.group(
        1
    )
    assert f"<script>{SCRIPT}</script>" in page and f"<style>{STYLE}</style>" in page
    assert "onclick" not in page and "onerror=" not in page.replace("onerror=alert", "")


def test_the_cli_writes_the_page_and_refuses_unknown_runs(
    seeded: psycopg.Connection,
    crawler_dsn: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    out = tmp_path / "review.html"
    assert (
        cli.main(
            ["review", RUN_ID, "--out", str(out)], env={"PIPELINE_DSN": crawler_dsn}
        )
        == 0
    )
    assert "groups=12" in capsys.readouterr().out and out.exists()
    assert (
        cli.main(
            ["review", "cr_20000101T000000_0000"], env={"PIPELINE_DSN": crawler_dsn}
        )
        == 2
    )
    with pytest.raises(LookupError):
        write_review(seeded, "cr_20000101T000000_0000", out)
    block = re.search(r'id="review-data">(.*?)</script>', out.read_text(), re.S)
    assert block is not None
    assert json.loads(block.group(1))["run_id"] == RUN_ID
