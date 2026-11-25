"""Crawler agent: finds exam-like multiple-choice sources and proposes them for review.

The package depends on ``pipeline`` (store, ledger model, HTML helpers), never the
reverse: ``pipeline/`` and ``harness/`` must not import ``crawler`` (spec 003, plan
"Structure Decision"; enforced by ``tests/crawler/test_crawler_guard.py``). The crawler
writes proposals and hand-off tickets only. It never edits the catalog, approves a
source or downloads a full file (FR-023): approval is a human act on the review page,
and ``pipeline import-decisions`` is the only path into the ledger.
"""
