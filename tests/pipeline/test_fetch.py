"""Tests for discover and fetch: the allowlist, robots.txt, and the rate limit.

Everything runs against an in-memory website (``httpx.MockTransport``) and a
virtual clock, so no test touches the network or waits (FR-027).
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Mapping, Sequence
from itertools import pairwise
from typing import TYPE_CHECKING

import pytest

from pipeline.acquire import (
    USER_AGENT,
    DiscoverSummary,
    FetchSummary,
    add_manual_url,
    discover,
    fetch,
)
from pipeline.blobs import LocalBlobStore
from pipeline.manifests import SourceState, load_catalog, read_ledger, replay_ledger

if TYPE_CHECKING:
    from conftest import DatasetBuilder, FakeClock, Web

SAT = "sat-2020-main"
HOST = "sat.exams.test"
ARCHIVE = f"https://{HOST}/archive"
ROBOTS = f"https://{HOST}/robots.txt"
ALLOW_ALL = "User-agent: *\nAllow: /\n"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def html(*hrefs: str) -> str:
    links = "".join(f'<a href="{h}">link</a>' for h in hrefs)
    return f"<html><body>{links}</body></html>"


def run_discover(
    ds: DatasetBuilder,
    web: Web,
    blobs: LocalBlobStore,
    clock: FakeClock,
    *,
    exam: str | None = "sat",
) -> DiscoverSummary:
    async def go() -> DiscoverSummary:
        async with web.client() as client:
            return await discover(
                load_catalog(ds.catalog_path),
                ds.ledger_path,
                blobs,
                client,
                clock,
                at=ds.at(),
                exam=exam,
            )

    return asyncio.run(go())


def run_fetch(
    ds: DatasetBuilder,
    web: Web,
    blobs: LocalBlobStore,
    clock: FakeClock,
    *,
    exam: str | None = "sat",
    refresh: bool = False,
    source_ids: Sequence[str] = (),
) -> FetchSummary:
    async def go() -> FetchSummary:
        async with web.client() as client:
            return await fetch(
                load_catalog(ds.catalog_path),
                ds.ledger_path,
                blobs,
                client,
                clock,
                at=ds.at(),
                exam=exam,
                refresh=refresh,
                source_ids=source_ids,
            )

    return asyncio.run(go())


def sources(ds: DatasetBuilder) -> Mapping[str, SourceState]:
    return replay_ledger(read_ledger(ds.ledger_path)).sources


class TestDiscover:
    def test_links_become_pending_candidates_and_off_allowlist_ones_are_rejected(
        self,
        dataset: DatasetBuilder,
        web: Web,
        blob_store: LocalBlobStore,
        clock: FakeClock,
    ) -> None:
        page = html(
            "/tests/2020.pdf",
            f"https://{HOST}/tests/2021.pdf#page=2",
            "https://mirror.example.net/sat2020.pdf",
            "mailto:someone@example.test",
            "#top",
        )
        web.add(ARCHIVE, page, headers={"content-type": "text/html"})
        dataset.save()
        before = set(sources(dataset))

        summary = run_discover(dataset, web, blob_store, clock)

        found = {s.url: s for sid, s in sources(dataset).items() if sid not in before}
        assert set(found) == {
            ARCHIVE,
            f"https://{HOST}/news",
            f"https://{HOST}/tests/2020.pdf",
            f"https://{HOST}/tests/2021.pdf",
            "https://mirror.example.net/sat2020.pdf",
        }
        assert found[f"https://{HOST}/tests/2020.pdf"].decision == "pending"
        assert found[f"https://{HOST}/tests/2020.pdf"].found_on == ARCHIVE
        assert found[f"https://{HOST}/tests/2020.pdf"].method == "index_crawl"
        mirror = found["https://mirror.example.net/sat2020.pdf"]
        assert (mirror.decision, mirror.decision_reason) == (
            "rejected",
            "non_allowlisted",
        )
        assert summary.rejected_off_allowlist == 1
        assert blob_store.exists(sha(page.encode()))  # the seed page is stored

    def test_nothing_is_fetched_from_a_candidate_it_finds(
        self,
        dataset: DatasetBuilder,
        web: Web,
        blob_store: LocalBlobStore,
        clock: FakeClock,
    ) -> None:
        web.add(ARCHIVE, html("/tests/2020.pdf", "https://mirror.example.net/x.pdf"))
        dataset.save()
        run_discover(dataset, web, blob_store, clock)
        requested = web.urls()
        assert f"https://{HOST}/tests/2020.pdf" not in requested
        assert not any("mirror.example.net" in url for url in requested)

    def test_sitemap_locations_are_candidates(
        self,
        dataset: DatasetBuilder,
        web: Web,
        blob_store: LocalBlobStore,
        clock: FakeClock,
    ) -> None:
        sitemap = (
            '<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/'
            f'sitemap/0.9"><url><loc>https://{HOST}/a.pdf</loc></url></urlset>'
        )
        web.add(ARCHIVE, sitemap, headers={"content-type": "application/xml"})
        dataset.save()
        run_discover(dataset, web, blob_store, clock)
        assert f"https://{HOST}/a.pdf" in {s.url for s in sources(dataset).values()}

    def test_a_second_run_appends_nothing(
        self,
        dataset: DatasetBuilder,
        web: Web,
        blob_store: LocalBlobStore,
        clock: FakeClock,
    ) -> None:
        web.add(ARCHIVE, html("/tests/2020.pdf"))
        dataset.save()
        run_discover(dataset, web, blob_store, clock)
        before = dataset.ledger_path.read_bytes()
        summary = run_discover(dataset, web, blob_store, clock)
        assert dataset.ledger_path.read_bytes() == before
        assert summary.candidates_added == 0

    def test_a_seed_that_robots_disallows_is_not_requested(
        self,
        dataset: DatasetBuilder,
        web: Web,
        blob_store: LocalBlobStore,
        clock: FakeClock,
    ) -> None:
        web.add(ROBOTS, "User-agent: *\nDisallow: /archive\n")
        web.add(ARCHIVE, html("/tests/2020.pdf"))
        dataset.save()
        summary = run_discover(dataset, web, blob_store, clock)
        assert ARCHIVE not in web.urls()
        assert summary.refused >= 1
        seed = next(s for s in sources(dataset).values() if s.url == ARCHIVE)
        assert seed.fetch_outcome == "refused_robots"

    def test_the_ledger_never_holds_page_text(
        self,
        dataset: DatasetBuilder,
        web: Web,
        blob_store: LocalBlobStore,
        clock: FakeClock,
    ) -> None:
        marker = "SYNTHETIC-STEM-MARKER"
        web.add(ARCHIVE, f"<p>{marker}</p>" + html("/tests/2020.pdf"))
        dataset.save()
        run_discover(dataset, web, blob_store, clock)
        assert marker not in dataset.ledger_path.read_text(encoding="utf-8")


class TestManualCandidates:
    def test_a_manual_url_is_recorded_as_pending(self, dataset: DatasetBuilder) -> None:
        dataset.save()
        catalog = load_catalog(dataset.catalog_path)
        url = f"https://{HOST}/found/by/hand.pdf"
        assert add_manual_url(catalog, dataset.ledger_path, url, SAT, at=dataset.at())
        source = next(s for s in sources(dataset).values() if s.url == url)
        assert (source.method, source.decision) == ("manual", "pending")

    def test_adding_the_same_url_twice_appends_nothing(
        self, dataset: DatasetBuilder
    ) -> None:
        dataset.save()
        catalog = load_catalog(dataset.catalog_path)
        url = f"https://{HOST}/hand.pdf"
        add_manual_url(catalog, dataset.ledger_path, url, SAT, at=dataset.at())
        before = dataset.ledger_path.read_bytes()
        assert not add_manual_url(
            catalog, dataset.ledger_path, url, SAT, at=dataset.at()
        )
        assert dataset.ledger_path.read_bytes() == before

    def test_an_off_allowlist_url_is_rejected_on_entry(
        self, dataset: DatasetBuilder
    ) -> None:
        dataset.save()
        catalog = load_catalog(dataset.catalog_path)
        url = "https://prep-site.example.net/sat.pdf"
        add_manual_url(catalog, dataset.ledger_path, url, SAT, at=dataset.at())
        source = next(s for s in sources(dataset).values() if s.url == url)
        assert (source.decision, source.decision_reason) == (
            "rejected",
            "non_allowlisted",
        )

    def test_an_unknown_edition_is_refused(self, dataset: DatasetBuilder) -> None:
        dataset.save()
        catalog = load_catalog(dataset.catalog_path)
        with pytest.raises(ValueError, match="unknown edition"):
            add_manual_url(
                catalog,
                dataset.ledger_path,
                f"https://{HOST}/x",
                "sat-1999-main",
                at="t",
            )


class TestFetch:
    def approved(self, ds: DatasetBuilder, path: str, role: str = "paper") -> str:
        return ds.add_source(
            f"https://{ds.catalog['exams']['sat']['domains'][0]}{path}", SAT, role
        )

    def test_an_approved_file_is_stored_and_recorded(
        self,
        dataset: DatasetBuilder,
        web: Web,
        blob_store: LocalBlobStore,
        clock: FakeClock,
    ) -> None:
        source = self.approved(dataset, "/p.pdf")
        web.add(
            f"https://{HOST}/p.pdf",
            b"%PDF synthetic",
            headers={"content-type": "application/pdf"},
        )
        dataset.save()
        summary = run_fetch(dataset, web, blob_store, clock)
        record = sources(dataset)[source].fetches[-1]
        assert (record.outcome, record.sha256, record.size) == (
            "fetched",
            sha(b"%PDF synthetic"),
            14,
        )
        assert record.media_type == "application/pdf"
        assert blob_store.get(sha(b"%PDF synthetic")) == b"%PDF synthetic"
        assert summary.fetched == 1

    def test_unapproved_and_rejected_candidates_are_never_requested(
        self,
        dataset: DatasetBuilder,
        web: Web,
        blob_store: LocalBlobStore,
        clock: FakeClock,
    ) -> None:
        self.approved(dataset, "/ok.pdf")
        dataset.add_source(f"https://{HOST}/pending.pdf", SAT, "paper", approve=False)
        rejected = dataset.add_source(f"https://{HOST}/rejected.pdf", SAT, "paper")
        dataset.review(rejected, SAT, "paper", approve=False, reason="not official")
        web.add(f"https://{HOST}/ok.pdf", b"ok")
        dataset.save()
        run_fetch(dataset, web, blob_store, clock)
        assert f"https://{HOST}/pending.pdf" not in web.urls()
        assert f"https://{HOST}/rejected.pdf" not in web.urls()

    def test_an_explicitly_requested_unapproved_source_is_recorded_not_fetched(
        self,
        dataset: DatasetBuilder,
        web: Web,
        blob_store: LocalBlobStore,
        clock: FakeClock,
    ) -> None:
        pending = dataset.add_source(
            f"https://{HOST}/pending.pdf", SAT, "paper", approve=False
        )
        dataset.save()
        run_fetch(dataset, web, blob_store, clock, source_ids=[pending])
        assert web.calls == []
        assert sources(dataset)[pending].fetch_outcome == "not_approved"

    def test_an_approved_url_off_the_allowlist_is_refused(
        self,
        dataset: DatasetBuilder,
        web: Web,
        blob_store: LocalBlobStore,
        clock: FakeClock,
    ) -> None:
        source = dataset.add_source("https://mirror.example.net/sat.pdf", SAT, "paper")
        web.add("https://mirror.example.net/sat.pdf", b"mirror copy")
        dataset.save()
        run_fetch(dataset, web, blob_store, clock)
        assert sources(dataset)[source].fetch_outcome == "refused_domain"
        assert not any("mirror.example.net" in url for url in web.urls())

    def test_robots_disallow_is_refused_and_recorded(
        self,
        dataset: DatasetBuilder,
        web: Web,
        blob_store: LocalBlobStore,
        clock: FakeClock,
    ) -> None:
        source = self.approved(dataset, "/private/p.pdf")
        web.add(ROBOTS, "User-agent: *\nDisallow: /private/\n")
        web.add(f"https://{HOST}/private/p.pdf", b"secret")
        dataset.save()
        run_fetch(dataset, web, blob_store, clock)
        assert sources(dataset)[source].fetch_outcome == "refused_robots"
        assert f"https://{HOST}/private/p.pdf" not in web.urls()

    def test_our_user_agent_is_sent_and_robots_rules_for_it_apply(
        self,
        dataset: DatasetBuilder,
        web: Web,
        blob_store: LocalBlobStore,
        clock: FakeClock,
    ) -> None:
        assert "LLM-Stereotypes-dataset" in USER_AGENT
        source = self.approved(dataset, "/p.pdf")
        web.add(ROBOTS, "User-agent: LLM-Stereotypes-dataset\nDisallow: /\n")
        web.add(f"https://{HOST}/p.pdf", b"x")
        dataset.save()
        run_fetch(dataset, web, blob_store, clock)
        assert sources(dataset)[source].fetch_outcome == "refused_robots"

    @pytest.mark.parametrize(
        ("status", "outcome"),
        [
            (401, "login_required"),
            (403, "login_required"),
            (404, "http_error"),
            (503, "http_error"),
        ],
    )
    def test_http_failures_are_recorded_by_kind(
        self,
        dataset: DatasetBuilder,
        web: Web,
        blob_store: LocalBlobStore,
        clock: FakeClock,
        status: int,
        outcome: str,
    ) -> None:
        source = self.approved(dataset, "/p.pdf")
        web.add(f"https://{HOST}/p.pdf", b"", status=status)
        dataset.save()
        run_fetch(dataset, web, blob_store, clock)
        assert sources(dataset)[source].fetch_outcome == outcome

    def test_a_redirect_off_the_allowlist_is_not_followed(
        self,
        dataset: DatasetBuilder,
        web: Web,
        blob_store: LocalBlobStore,
        clock: FakeClock,
    ) -> None:
        source = self.approved(dataset, "/p.pdf")
        web.add(
            f"https://{HOST}/p.pdf",
            status=302,
            headers={"location": "https://login.example.net/sso"},
        )
        web.add("https://login.example.net/sso", b"sign in")
        dataset.save()
        run_fetch(dataset, web, blob_store, clock)
        assert sources(dataset)[source].fetch_outcome == "login_required"
        assert "https://login.example.net/sso" not in web.urls()

    def test_a_redirect_within_the_allowlist_is_followed(
        self,
        dataset: DatasetBuilder,
        web: Web,
        blob_store: LocalBlobStore,
        clock: FakeClock,
    ) -> None:
        source = self.approved(dataset, "/p.pdf")
        web.add(
            f"https://{HOST}/p.pdf", status=301, headers={"location": "/files/p.pdf"}
        )
        web.add(f"https://{HOST}/files/p.pdf", b"%PDF moved")
        dataset.save()
        run_fetch(dataset, web, blob_store, clock)
        assert sources(dataset)[source].fetches[-1].sha256 == sha(b"%PDF moved")

    def test_requests_to_one_domain_are_spaced_by_the_rate_limit(
        self,
        dataset: DatasetBuilder,
        web: Web,
        blob_store: LocalBlobStore,
        clock: FakeClock,
    ) -> None:
        web.add(ROBOTS, ALLOW_ALL)
        for name in ("a", "b", "c"):
            self.approved(dataset, f"/{name}.pdf")
            web.add(f"https://{HOST}/{name}.pdf", name.encode())
        dataset.save()
        run_fetch(dataset, web, blob_store, clock)
        urls = [url for url, _ in web.calls if HOST in url]
        assert urls.count(ROBOTS) == 1  # robots.txt is read once per domain
        assert {f"https://{HOST}/{n}.pdf" for n in ("a", "b", "c")} <= set(urls)
        times = [t for url, t in web.calls if HOST in url]
        assert all(later - earlier >= 5 for earlier, later in pairwise(times))

    def test_an_exam_can_override_the_rate_limit(
        self,
        dataset: DatasetBuilder,
        web: Web,
        blob_store: LocalBlobStore,
        clock: FakeClock,
    ) -> None:
        dataset.catalog["exams"]["sat"]["rate_limit_seconds"] = 12
        for name in ("a", "b"):
            self.approved(dataset, f"/{name}.pdf")
            web.add(f"https://{HOST}/{name}.pdf", name.encode())
        dataset.save()
        run_fetch(dataset, web, blob_store, clock)
        times = [t for url, t in web.calls if HOST in url]
        assert all(later - earlier >= 12 for earlier, later in pairwise(times))

    def test_changed_content_at_the_same_url_keeps_the_first_file(
        self,
        dataset: DatasetBuilder,
        web: Web,
        blob_store: LocalBlobStore,
        clock: FakeClock,
    ) -> None:
        source = self.approved(dataset, "/p.pdf")
        url = f"https://{HOST}/p.pdf"
        web.add(url, b"version one")
        dataset.save()
        run_fetch(dataset, web, blob_store, clock)
        web.add(url, b"version two")
        run_fetch(dataset, web, blob_store, clock, refresh=True)
        shas = [r.sha256 for r in sources(dataset)[source].fetches]
        assert shas == [sha(b"version one"), sha(b"version two")]
        assert blob_store.get(sha(b"version one")) == b"version one"
        assert blob_store.get(sha(b"version two")) == b"version two"

    def test_refetching_unchanged_content_appends_nothing(
        self,
        dataset: DatasetBuilder,
        web: Web,
        blob_store: LocalBlobStore,
        clock: FakeClock,
    ) -> None:
        self.approved(dataset, "/p.pdf")
        web.add(f"https://{HOST}/p.pdf", b"same")
        dataset.save()
        run_fetch(dataset, web, blob_store, clock)
        before = dataset.ledger_path.read_bytes()
        summary = run_fetch(dataset, web, blob_store, clock, refresh=True)
        assert dataset.ledger_path.read_bytes() == before
        assert summary.unchanged == 1

    def test_files_already_fetched_are_not_requested_again_by_default(
        self,
        dataset: DatasetBuilder,
        web: Web,
        blob_store: LocalBlobStore,
        clock: FakeClock,
    ) -> None:
        self.approved(dataset, "/p.pdf")
        web.add(f"https://{HOST}/p.pdf", b"same")
        dataset.save()
        run_fetch(dataset, web, blob_store, clock)
        run_fetch(dataset, web, blob_store, clock)
        assert web.urls().count(f"https://{HOST}/p.pdf") == 1

    def test_the_exam_filter_limits_what_is_fetched(
        self,
        dataset: DatasetBuilder,
        web: Web,
        blob_store: LocalBlobStore,
        clock: FakeClock,
    ) -> None:
        self.approved(dataset, "/p.pdf")
        dataset.add_source("https://enem.exams.test/p.pdf", "enem-2020-main", "paper")
        web.add(f"https://{HOST}/p.pdf", b"sat")
        web.add("https://enem.exams.test/p.pdf", b"enem")
        dataset.save()
        run_fetch(dataset, web, blob_store, clock, exam="sat")
        assert "https://enem.exams.test/p.pdf" not in web.urls()

    def test_the_ledger_never_holds_file_text(
        self,
        dataset: DatasetBuilder,
        web: Web,
        blob_store: LocalBlobStore,
        clock: FakeClock,
    ) -> None:
        marker = "SYNTHETIC-STEM-MARKER"
        self.approved(dataset, "/p.pdf")
        web.add(f"https://{HOST}/p.pdf", f"%PDF {marker}".encode())
        dataset.save()
        run_fetch(dataset, web, blob_store, clock)
        assert marker not in dataset.ledger_path.read_text(encoding="utf-8")
