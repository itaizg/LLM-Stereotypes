"""The review page: one row per content group of a run's ``passed`` proposals (FR-024).

``crawler review RUN_ID`` writes one self-contained HTML file. The maintainer approves
or
rejects each group, choosing the exam, edition and role and confirming each URL's
origin; "Save decisions" downloads ``decisions-<run_id>.json`` for
``pipeline import-decisions`` (contracts/decisions-file.md).

Safety (research R15, the team's XSS rule):
- every string derived from crawled content is escaped with ``html.escape`` and shown as
  text; the embedded data is JSON with ``<`` escaped, in a non-executable data block;
- a Content-Security-Policy allows only the page's own inline script and style, by hash,
  and nothing remote.

Everything in a decision except the maintainer's choices is computed here, in Python:
``decision_skeletons`` gives each group's ``decision_id``, content hash and members with
the revisions shown. The page's script only copies a skeleton and adds the choices, the
same way ``merge_choices`` does, so the file's structure is tested without a browser.
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from string import Template
from typing import Any

import psycopg

from pipeline.decisions import FORMAT, Decision, decision_id_for
from pipeline.manifests import REVIEW_ROLES


@dataclass(frozen=True)
class Member:
    proposal_id: str
    revision: int
    url: str
    origin: str
    labels: Mapping[str, Any]
    checks: Mapping[str, Any]


@dataclass(frozen=True)
class Group:
    content_hash: str
    members: tuple[Member, ...]  # primary first

    @property
    def primary(self) -> Member:
        return self.members[0]


def load_groups(conn: psycopg.Connection, run_id: str) -> list[Group]:
    """The run's current ``passed`` proposals, grouped by sampled-content hash."""
    if (
        conn.execute(
            "SELECT 1 FROM crawler_runs WHERE run_id = %s", (run_id,)
        ).fetchone()
        is None
    ):
        raise LookupError(f"unknown run {run_id}")
    rows = conn.execute(
        "SELECT proposal_id, revision, url, labels, checks, content_hash "
        "FROM crawler_proposals WHERE run_id = %s AND validation_status = 'passed' "
        "AND review_state <> 'stale_superseded' AND content_hash IS NOT NULL "
        "ORDER BY created_at, proposal_id",
        (run_id,),
    ).fetchall()
    grouped: dict[str, list[Member]] = {}
    for proposal_id, revision, url, labels, checks, digest in rows:
        grouped.setdefault(digest, []).append(
            Member(
                proposal_id, revision, url, str(labels.get("origin")), labels, checks
            )
        )
    groups = []
    for digest, members in grouped.items():
        # FR-023a: an official URL is primary when there is one, else the first found.
        ordered = sorted(
            enumerate(members), key=lambda pair: (pair[1].origin != "official", pair[0])
        )
        groups.append(Group(digest, tuple(m for _, m in ordered)))
    return groups


def decision_skeletons(run_id: str, groups: Sequence[Group]) -> list[dict[str, Any]]:
    """Each group's decision without the maintainer's choices."""
    skeletons = []
    for group in groups:
        revisions = [m.revision for m in group.members]
        skeletons.append(
            {
                "decision_id": decision_id_for(run_id, group.content_hash, revisions),
                "content_hash": group.content_hash,
                "members": [
                    {
                        "proposal_id": m.proposal_id,
                        "revision": m.revision,
                        "primary": index == 0,
                        "origin": m.origin,
                    }
                    for index, m in enumerate(group.members)
                ],
            }
        )
    return skeletons


def merge_choices(
    skeleton: Mapping[str, Any], choices: Mapping[str, Any]
) -> dict[str, Any]:
    """The reference for the page's merge step: a skeleton plus the choices, validated.

    ``choices`` holds ``decision`` and, as the page collects them, ``exam``,
    ``new_exam``, ``edition_id``, ``role``, ``published``, ``reason`` and ``origins``
    (proposal id → confirmed origin).
    """
    origins = choices.get("origins") or {}
    members = [
        {**member, "origin": origins.get(member["proposal_id"], member["origin"])}
        for member in skeleton["members"]
    ]
    approve = choices.get("decision") == "approve"
    decision = {
        "decision_id": skeleton["decision_id"],
        "content_hash": skeleton["content_hash"],
        "members": members,
        "decision": choices.get("decision"),
        "exam": choices.get("exam") if approve else None,
        "new_exam": choices.get("new_exam") if approve else None,
        "edition_id": choices.get("edition_id") if approve else None,
        "role": choices.get("role") if approve else None,
        "published": choices.get("published") if approve else None,
        "reason": None if approve else choices.get("reason"),
    }
    Decision.model_validate(decision)
    return decision


# --- rendering ------------------------------------------------------------------------

STYLE = """
:root { color-scheme: light dark; --line: #8884; --muted: #888; --flag: #b45309; }
body { font: 14px/1.45 system-ui, sans-serif; margin: 16px; }
header { display: flex; gap: 12px; align-items: center; flex-wrap: wrap; }
table { border-collapse: collapse; width: 100%; margin-top: 12px; }
td, th { border-top: 1px solid var(--line); padding: 8px; vertical-align: top;
  text-align: left; }
.alt { color: var(--muted); font-size: 12px; }
.flag { color: var(--flag); font-weight: 600; margin-right: 6px; }
.terms { color: var(--muted); font-size: 12px; max-width: 32em; }
.decision label { display: block; margin: 2px 0; }
input[type=text], select { max-width: 14em; }
.hidden { display: none; }
"""

SCRIPT = """
"use strict";
const data = JSON.parse(document.getElementById("review-data").textContent);
const byId = new Map(data.skeletons.map((s) => [s.decision_id, s]));
function value(row, name) {
  const el = row.querySelector('[name="' + name + '"]');
  if (!el) return null;
  if (el.type === "checkbox") return el.checked;
  return el.value.trim() === "" ? null : el.value.trim();
}
function choicesOf(row) {
  const picked = row.querySelector('input[type="radio"]:checked');
  if (!picked || picked.value === "skip") return null;
  const origins = {};
  row.querySelectorAll("select[data-origin]").forEach((el) => {
    origins[el.dataset.origin] = el.value;
  });
  const isNew = value(row, "exam_new") === true;
  return {
    decision: picked.value,
    exam: { key: value(row, "exam_key"), new: isNew },
    new_exam: isNew
      ? { name: value(row, "new_name"), body: value(row, "new_body"),
          language: value(row, "new_language") }
      : null,
    edition_id: value(row, "edition_id"),
    role: value(row, "role"),
    published: value(row, "published"),
    reason: value(row, "reason"),
    origins: origins,
  };
}
// Mirrors crawler.review.merge_choices: copy the skeleton, add the choices.
function merge(skeleton, choices) {
  const approve = choices.decision === "approve";
  return {
    decision_id: skeleton.decision_id,
    content_hash: skeleton.content_hash,
    members: skeleton.members.map((m) =>
      Object.assign({}, m, { origin: choices.origins[m.proposal_id] || m.origin })),
    decision: choices.decision,
    exam: approve ? choices.exam : null,
    new_exam: approve ? choices.new_exam : null,
    edition_id: approve ? choices.edition_id : null,
    role: approve ? choices.role : null,
    published: approve ? choices.published : null,
    reason: approve ? null : choices.reason,
  };
}
document.getElementById("save").addEventListener("click", () => {
  const decisions = [];
  document.querySelectorAll("tr[data-decision]").forEach((row) => {
    const choices = choicesOf(row);
    if (choices) decisions.push(merge(byId.get(row.dataset.decision), choices));
  });
  const file = { format: data.format, run_id: data.run_id,
    decided_at: new Date().toISOString(), decisions: decisions };
  const blob = new Blob([JSON.stringify(file, null, 2)], { type: "application/json" });
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  link.download = "decisions-" + data.run_id + ".json";
  link.click();
  const saved = document.getElementById("saved");
  saved.textContent = decisions.length + " decision(s) saved";
});
function applyFilter() {
  const flag = document.getElementById("flag-filter").value;
  const lang = document.getElementById("lang-filter").value;
  document.querySelectorAll("tr[data-decision]").forEach((row) => {
    const flags = row.dataset.flags.split(" ");
    const show = (flag === "" || flags.includes(flag))
      && (lang === "" || row.dataset.lang === lang);
    row.classList.toggle("hidden", !show);
  });
}
document.getElementById("flag-filter").addEventListener("change", applyFilter);
document.getElementById("lang-filter").addEventListener("change", applyFilter);
"""

PAGE = Template(
    """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="$csp">
<title>Crawler review $run_id</title>
<style>$style</style></head><body>
<header><h1>Review: run $run_id</h1>
<span>$count content group(s) to review</span>
<label>Flag <select id="flag-filter"><option value="">all</option>
<option value="restrictive_terms">restrictive_terms</option>
<option value="lexicon_missing">lexicon_missing</option></select></label>
<label>Language <select id="lang-filter"><option value="">all</option>$languages
</select></label>
<button id="save" type="button">Save decisions</button>
<span id="saved"></span></header>
<p>Approving a row approves every URL in it.
Crawled text below is shown as text only.</p>
<table><thead><tr><th>Source</th><th>Labels</th><th>Checks</th><th>Decision</th></tr></thead>
<tbody>$rows</tbody></table>
<script type="application/json" id="review-data">$data</script>
<script>$script</script>
</body></html>
"""
)


def _e(value: object) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _sha256(source: str) -> str:
    return (
        "sha256-"
        + base64.b64encode(hashlib.sha256(source.encode("utf-8")).digest()).decode()
    )


def csp() -> str:
    return (
        "default-src 'none'; "
        f"script-src '{_sha256(SCRIPT)}'; style-src '{_sha256(STYLE)}'; "
        "img-src 'none'; base-uri 'none'; form-action 'none'"
    )


def _link(url: str) -> str:
    safe = url if url.startswith(("https://", "http://")) else "#"
    return (
        f'<a href="{_e(safe)}" rel="noreferrer noopener" target="_blank">{_e(url)}</a>'
    )


def _checks(checks: Mapping[str, Any]) -> str:
    parts = []
    for name in ("reachable", "downloadable", "mcq", "key_located"):
        check = checks.get(name) or {}
        detail = ""
        if name == "mcq" and check.get("item_count") is not None:
            detail = f" ({_e(check['item_count'])} items)"
        if name == "key_located":
            status = check.get("key_status")
            evidence = check.get("evidence")
            detail = f" ({_e(status)})" + (f" {_link(evidence)}" if evidence else "")
        parts.append(f"{_e(name)}: {_e(check.get('result'))}{detail}")
    return "<br>".join(parts)


def _row(index: int, group: Group, skeleton: Mapping[str, Any]) -> str:
    primary = group.primary
    labels = primary.labels
    flags = sorted({f for m in group.members for f in m.labels.get("flags", [])})
    shown = [
        f
        for f in flags
        if f in {"restrictive_terms", "lexicon_missing", "reopened_after_approval"}
    ]
    alternates = "".join(
        f'<div class="alt">alternate: {_link(m.url)} ({_e(m.origin)})</div>'
        for m in group.members[1:]
    )
    terms = ""
    if labels.get("terms_restrictive") or labels.get("terms_excerpt"):
        terms = f'<div class="terms">terms: {_e(labels.get("terms_excerpt"))}</div>'
    origin_controls = "".join(
        f'<label>{_e(m.url)} <select data-origin="{_e(m.proposal_id)}">'
        + "".join(
            f'<option value="{o}"{" selected" if o == m.origin else ""}>{o}</option>'
            for o in ("official", "third_party")
        )
        + "</select></label>"
        for m in group.members
    )
    roles = "".join(f'<option value="{r}">{r}</option>' for r in REVIEW_ROLES)
    keywords = json.dumps(labels.get("keyword_counts", {}), ensure_ascii=False)
    label_text = (
        f"kind {_e(labels.get('kind'))}, language {_e(labels.get('language'))}<br>"
        f"site {_e(labels.get('site_type'))}, page {_e(labels.get('page_type'))} "
        f"({_e(labels.get('page_type_confidence'))}), topic "
        f"{_e(labels.get('topic'))}<br>"
        f"stated: {_e(labels.get('exam_name_stated'))} "
        f"{_e(labels.get('exam_year_stated'))}<br>"
        f"keywords: {_e(keywords)}"
    )
    return (
        f'<tr data-decision="{_e(skeleton["decision_id"])}" '
        f'data-flags="{_e(" ".join(flags))}" '
        f'data-lang="{_e(labels.get("language"))}">'
        f"<td>{_link(primary.url)} ({_e(primary.origin)}){alternates}"
        + "".join(f'<span class="flag">{_e(f)}</span>' for f in shown)
        + f"{terms}</td><td>{label_text}</td><td>{_checks(primary.checks)}</td>"
        '<td class="decision">'
        f'<label><input type="radio" name="decision-{index}" value="skip" checked> '
        "undecided</label>"
        f'<label><input type="radio" name="decision-{index}" value="approve"> '
        "approve</label>"
        f'<label><input type="radio" name="decision-{index}" value="reject"> '
        "reject</label>"
        '<label>exam <input type="text" name="exam_key" placeholder="enem"></label>'
        '<label><input type="checkbox" name="exam_new"> new exam</label>'
        '<label>name <input type="text" name="new_name"></label>'
        '<label>body <input type="text" name="new_body"></label>'
        '<label>language <input type="text" name="new_language" '
        'placeholder="pt"></label>'
        '<label>edition <input type="text" name="edition_id"></label>'
        f'<label>role <select name="role">{roles}</select></label>'
        '<label>published <input type="text" name="published" '
        'placeholder="YYYY-MM-DD"></label>'
        '<label>reason <input type="text" name="reason"></label>'
        f"{origin_controls}</td></tr>"
    )


def render(run_id: str, groups: Sequence[Group]) -> str:
    skeletons = decision_skeletons(run_id, groups)
    data = json.dumps(
        {"format": FORMAT, "run_id": run_id, "skeletons": skeletons}, ensure_ascii=False
    ).replace("<", "\\u003c")
    languages = sorted({str(g.primary.labels.get("language")) for g in groups})
    return PAGE.substitute(
        csp=csp(),
        run_id=_e(run_id),
        style=STYLE,
        count=len(groups),
        languages="".join(
            f'<option value="{_e(lang)}">{_e(lang)}</option>' for lang in languages
        ),
        rows="".join(
            _row(i, g, s)
            for i, (g, s) in enumerate(zip(groups, skeletons, strict=True))
        ),
        data=data,
        script=SCRIPT,
    )


def write_review(conn: psycopg.Connection, run_id: str, out: Path) -> int:
    groups = load_groups(conn, run_id)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(run_id, groups), encoding="utf-8")
    return len(groups)
