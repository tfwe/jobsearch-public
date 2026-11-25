"""Static HTML dashboard for the jobsearch pipeline. 

The page is one self-contained file: inline CSS and one inline script, no external
font, no CDN. Reads go through jobsearch.store.Store and jobsearch.match.is_eligible.
"""
from __future__ import annotations

import html
import os
from datetime import datetime, timezone
from urllib.parse import quote

from .match import dedupe_postings, is_eligible
from .store import STATUSES, Store

IN_FLIGHT = ("shortlisted", "prepped", "applied")
_NO_LIMIT = 10 ** 9  # Store.list requires an int limit; this is effectively unbounded.

_CSS = """
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:#11141a;color:#e6e8ee;
font:16px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
overflow-wrap:anywhere}
.wrap{max-width:1100px;margin:0 auto;padding:14px;width:100%}
h1{font-size:1.5rem;margin:0 0 6px}
h2{font-size:1.1rem;margin:26px 0 10px;padding-bottom:4px;border-bottom:1px solid #2a2f3a}
.sub{color:#9aa3b2;font-size:.9rem;margin:0 0 10px}
.chips{display:flex;flex-wrap:wrap;gap:6px}
.chip{background:#1d222c;border:1px solid #2f3644;border-radius:999px;padding:3px 10px;
font-size:.85rem;white-space:nowrap}
.chip b{color:#fff;margin-left:4px}
.grid{display:grid;gap:10px;grid-template-columns:minmax(0,1fr)}
@media (min-width:700px){.grid{grid-template-columns:repeat(auto-fill,minmax(320px,1fr))}}
.card{background:#181c25;border:1px solid #2a2f3a;border-radius:10px;padding:12px;min-width:0}
.row{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.score{font-weight:700;padding:2px 8px;border-radius:6px;color:#0b0d12;min-width:3.2em;text-align:center}
.band-strong{background:#4ade80}
.band-possible{background:#facc15}
.band-long{background:#94a3b8}
.company{font-weight:600}
.title{margin:6px 0 2px}
.meta{color:#9aa3b2;font-size:.85rem}
.status{font-size:.8rem;border:1px solid #2f3644;border-radius:6px;padding:1px 6px;color:#cbd5e1}
a{color:#7dd3fc}
.warn{color:#fca5a5}
.ok{color:#86efac}
ul{padding-left:20px;margin:6px 0}
.empty{color:#9aa3b2;font-style:italic}
.sform{display:flex;flex-wrap:wrap;gap:6px;margin-top:10px}
.sform select,.sform input,.sform button{font:inherit;font-size:.9rem;min-height:36px;padding:4px 8px;min-width:0;border:1px solid #2a2f3a;border-radius:6px;background:#181c25;color:#e6e8ee}
footer{color:#6b7280;font-size:.8rem;margin:30px 0 10px}
[hidden]{display:none!important}
.title a{color:inherit;text-decoration:none}
.title a:hover{text-decoration:underline}
.filters{display:flex;flex-direction:column;gap:8px;margin:0 0 4px}
.fsearch{font:inherit;font-size:.95rem;min-height:40px;padding:6px 10px;width:100%;border:1px solid #2a2f3a;border-radius:6px;background:#181c25;color:#e6e8ee}
.fchip{font:inherit;font-size:.85rem;color:#e6e8ee;cursor:pointer}
.fchip[aria-pressed="true"]{border-color:#7dd3fc;color:#fff;background:#243044}
.fcount{margin:0}
"""

# Forward-only status moves a reader may pick from a card. Terminal statuses
# (offer, rejected, closed, skipped) have no entry, so their cards get no form.
ALLOWED_NEXT = {
    "new": ("shortlisted", "skipped"),
    "shortlisted": ("prepped", "applied", "skipped"),
    "prepped": ("applied", "skipped"),
    "applied": ("interview", "rejected", "closed"),
    "interview": ("offer", "rejected", "closed"),
}

FOOTER_NOTE = ("This page never submits an application, emails or messages anyone; "
               "you click Submit yourself in the employer's own portal.")


def _esc(value: object) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _band(score: float | None) -> str:
    if score is None:
        return "band-long"
    if score >= 25:
        return "band-strong"
    if score >= 15:
        return "band-possible"
    return "band-long"


def _fmt_score(score: float | None) -> str:
    return "—" if score is None else f"{score:.1f}".rstrip("0").rstrip(".")


def _sort_key(row: dict) -> tuple:
    score = row.get("score")
    return (score is None, -(score or 0.0), row.get("uid") or "")


def _date_only(ts: str | None) -> str:
    return (ts or "")[:10] or "—"


def _eligible_map(rows: list[dict]) -> dict[str, bool]:
    out = {}
    for r in rows:
        posting = {k: ("" if v is None else v) for k, v in r.items()}
        out[r["uid"]] = bool(is_eligible(posting)[0])
    return out


def _packet_slug(uid: str) -> str:
    """Same mapping as jobsearch.letter.slug_for_uid and cli._slug: the packet dir name."""
    return uid.replace(":", "_").replace("/", "_")


def _packet_link(slug: str, name: str, label: str) -> str:
    path = "/packets/" + quote(slug, safe="") + "/" + quote(name, safe="")
    return f'<a href="{_esc(path)}">{_esc(label)}</a>'


def _packet_html(uid: str | None, packets_dir: str | None) -> str:
    """One 'packet:' links line for a posting whose packet dir exists on disk; else ''."""
    if not uid or not packets_dir:
        return ""
    slug = _packet_slug(uid)
    if not slug or slug.startswith("."):  # never resolve '.', '..' or hidden dirs
        return ""
    pdir = os.path.join(packets_dir, slug)
    if not os.path.isdir(pdir):
        return ""
    parts = []
    if os.path.isfile(os.path.join(pdir, "resume.pdf")):
        parts.append(_packet_link(slug, "resume.pdf", "resume"))
    if os.path.isfile(os.path.join(pdir, "cover_letter.pdf")):
        parts.append(_packet_link(slug, "cover_letter.pdf", "cover letter"))
    else:
        parts.append('<span class="meta">no letter yet</span>')
    if os.path.isfile(os.path.join(pdir, "posting.md")):
        parts.append(_packet_link(slug, "posting.md", "posting"))
    return '<div class="meta">packet: ' + " · ".join(parts) + "</div>"


def _title_html(row: dict) -> str:
    """The job title, linking to the application timeline page at /posting/UID."""
    title = _esc(row.get("title") or "—")
    uid = row.get("uid")
    if not uid:
        return title
    return f'<a href="{_esc("/posting/" + quote(str(uid), safe=""))}">{title}</a>'


def _status_form(row: dict) -> str:
    """Compact status-update form for a card, or '' when its status has no allowed move."""
    nxt = ALLOWED_NEXT.get(row.get("status") or "")
    if not nxt:
        return ""
    placeholder = '<option value="" selected disabled>change status...</option>'
    options = placeholder + "".join(f'<option value="{_esc(s)}">{_esc(s)}</option>' for s in nxt)
    action = "/api/postings/" + quote(str(row.get("uid") or ""), safe="") + "/status"
    return (
        f'<form class="sform" method="post" action="{_esc(action)}">'
        f'<select name="status" aria-label="new status" required>{options}</select>'
        '<input name="note" placeholder="note (optional)">'
        '<button type="submit">Save</button>'
        '</form>'
    )


def _posting_card(row: dict, extra: str, packets_dir: str | None = None) -> str:
    company = _esc(row.get("company") or "—")
    location = _esc(row.get("location") or "—")
    packet = _packet_html(row.get("uid"), packets_dir)
    return (
        f'<div class="card" data-uid="{_esc(row.get("uid"))}" data-status="{_esc(row.get("status"))}">'
        f'<div class="row"><span class="score {_band(row.get("score"))}">'
        f'{_fmt_score(row.get("score"))}</span>{extra}</div>'
        f'<div class="company">{company}</div>'
        f'<div class="title">{_title_html(row)}</div>'
        f'<div class="meta">{location}</div>'
        f'{packet}'
        f'{_status_form(row)}'
        f'</div>'
    )


def _variant_html(row: dict) -> str:
    return f'<span class="meta">{_esc(row.get("variant") or "general")}</span>'


SCORED_MATCH_MIN = 15.0  # same threshold as scan's --min-score default


def _header_html(total: int, passed_gate: int, scored_matches: int, by_status: dict,
                 last_scan: dict | None, generated: str) -> str:
    scanned = (last_scan or {}).get("scanned_at") or "never"
    chips = "".join(
        f'<span class="chip">{_esc(s)}<b>{by_status.get(s, 0)}</b></span>' for s in STATUSES)
    return (
        '<header>'
        '<h1>Job pipeline</h1>'
        f'<p class="sub">last scan: {_esc(scanned)} · stored: {total}'
        f' · passed the gate: {passed_gate} · scored matches (score ≥ {SCORED_MATCH_MIN:g}):'
        f' {scored_matches} · rendered: {_esc(generated)}</p>'
        f'<div class="chips">{chips}</div>'
        '</header>'
    )


def _top_html(top: list[dict], top_n: int, store: Store, packets_dir: str | None = None) -> str:
    if not top:
        body = '<p class="empty">No eligible postings with status new.</p>'
    else:
        cards = []
        for r in top:
            extra = _variant_html(r)
            cards.append(_posting_card(r, extra, packets_dir))
        body = f'<div class="grid">{"".join(cards)}</div>'
    return f'<h2>Top matches <span class="meta">(up to {top_n})</span></h2>{body}'


def _inflight_html(rows: list[dict], store: Store, packets_dir: str | None = None) -> str:
    if not rows:
        return ('<h2>In flight</h2>'
                '<p class="empty">Nothing shortlisted, prepped or applied.</p>')
    cards = []
    for r in rows:
        status_events = [e for e in store.events(r["uid"]) if e.get("kind") == "status"]
        when = _date_only(status_events[-1]["ts"]) if status_events else "—"
        extra = (f'<span class="status">{_esc(r.get("status"))}</span>'
                 f'<span class="meta">since {_esc(when)}</span>')
        cards.append(_posting_card(r, extra, packets_dir))
    return f'<h2>In flight</h2><div class="grid">{"".join(cards)}</div>'


def _problem_text(p: object) -> str:
    if isinstance(p, dict):
        name = p.get("name") or p.get("source") or ""
        detail = p.get("detail") or ""
        return f"{name}: {detail}" if name else str(detail)
    return str(p)


def _health_html(last_scan: dict | None) -> str:
    if not last_scan:
        return ('<h2>Source health</h2>'
                '<p class="empty">No scan recorded yet. Run scan to populate this section.</p>')
    line = (f'fetched {_esc(last_scan.get("fetched", "?"))}, '
            f'new {_esc(last_scan.get("new", "?"))}, '
            f'updated {_esc(last_scan.get("updated", "?"))}')
    dead_scans = last_scan.get("dead_scans") or {}
    problems = last_scan.get("problems") or []
    items = []
    for p in problems:
        text = _problem_text(p)
        note = ""
        for key, count in dead_scans.items():
            if key and key in text and isinstance(count, int) and count > 1:
                note = f' <span class="warn">dead for {count} scans</span>'
                break
        items.append(f'<li class="warn">{_esc(text)}{note}</li>')
    if items:
        problem_html = f'<p>Problems:</p><ul>{"".join(items)}</ul>'
    else:
        problem_html = '<p class="ok">No source problems in the last scan.</p>'
    return f'<h2>Source health</h2><p class="meta">{line}</p>{problem_html}'


# Inline filter UI. Built entirely in the DOM at load time, so with JS off the
# server-rendered markup is unchanged and no filter controls exist.
_FILTER_JS = r"""
(function () {
  var header = document.querySelector('header');
  var nodes = document.querySelectorAll('.card[data-uid]');
  if (!header || !nodes.length) { return; }
  var items = [];
  Array.prototype.forEach.call(nodes, function (card) {
    var text = Array.prototype.filter.call(card.children, function (k) {
      return !k.classList.contains('sform');
    }).map(function (k) { return k.textContent; }).join(' ').toLowerCase();
    items.push({ card: card, text: text, status: card.getAttribute('data-status') || '' });
  });
  var statuses = ['all'];
  items.forEach(function (it) {
    if (it.status && statuses.indexOf(it.status) === -1) { statuses.push(it.status); }
  });
  var active = 'all';

  var bar = document.createElement('div');
  bar.className = 'filters';
  var input = document.createElement('input');
  input.type = 'search';
  input.className = 'fsearch';
  input.placeholder = 'Search title, company, location';
  input.setAttribute('aria-label', 'Search postings');
  var chips = document.createElement('div');
  chips.className = 'chips';
  statuses.forEach(function (s) {
    var b = document.createElement('button');
    b.type = 'button';
    b.className = 'chip fchip';
    b.setAttribute('data-filter', s);
    b.setAttribute('aria-pressed', s === 'all' ? 'true' : 'false');
    b.textContent = s;
    chips.appendChild(b);
  });
  var count = document.createElement('p');
  count.className = 'meta fcount';
  count.setAttribute('aria-live', 'polite');
  bar.appendChild(input);
  bar.appendChild(chips);
  bar.appendChild(count);
  header.insertAdjacentElement('afterend', bar);

  var sections = [];
  Array.prototype.forEach.call(document.querySelectorAll('h2'), function (h) {
    var cards = [];
    var n = h.nextElementSibling;
    while (n && n.tagName !== 'H2' && n.tagName !== 'FOOTER') {
      Array.prototype.forEach.call(n.querySelectorAll('.card[data-uid]'), function (c) { cards.push(c); });
      n = n.nextElementSibling;
    }
    sections.push({ heading: h, cards: cards });
  });

  function apply() {
    var q = input.value.trim().toLowerCase();
    var shown = 0;
    items.forEach(function (it) {
      var ok = (active === 'all' || it.status === active) && (q === '' || it.text.indexOf(q) !== -1);
      it.card.hidden = !ok;
      if (ok) { shown += 1; }
    });
    sections.forEach(function (s) {
      if (!s.cards.length) { return; }
      var any = s.cards.some(function (c) { return !c.hidden; });
      s.heading.hidden = !any;
    });
    count.textContent = shown + ' of ' + items.length + ' matched';
  }

  input.addEventListener('input', apply);
  chips.addEventListener('click', function (e) {
    var b = e.target.closest ? e.target.closest('button[data-filter]') : null;
    if (!b || !chips.contains(b)) { return; }
    active = b.getAttribute('data-filter');
    Array.prototype.forEach.call(chips.querySelectorAll('button'), function (x) {
      x.setAttribute('aria-pressed', x === b ? 'true' : 'false');
    });
    apply();
  });
  apply();
})();
"""


def render_dashboard(db_path: str, out_path: str,
                     last_scan: dict | None = None, top_n: int = 40,
                     packets_dir: str | None = None) -> str:
    """Write the standalone dashboard HTML to out_path and return out_path.

    packets_dir defaults to <db_path's directory>/packets; cards get packet links
    only for postings whose <packets_dir>/<slug> directory exists.
    """
    if packets_dir is None:
        packets_dir = os.path.join(os.path.dirname(db_path), "packets")
    store = Store(db_path)
    try:
        stats = store.stats()
        rows = store.list(limit=_NO_LIMIT)
        eligible = _eligible_map(rows)
        passed_gate = sum(1 for r in rows if eligible.get(r["uid"]))
        scored_matches = sum(1 for r in rows
                             if r.get("score") is not None and r["score"] >= SCORED_MATCH_MIN)

        new_eligible = [r for r in rows if r.get("status") == "new" and eligible[r["uid"]]]
        new_eligible.sort(key=_sort_key)
        new_eligible = dedupe_postings(new_eligible)  # before the cap, so a repost cannot take a slot
        top = new_eligible[:max(0, int(top_n))]

        in_flight = [r for r in rows if r.get("status") in IN_FLIGHT]
        in_flight.sort(key=lambda r: (IN_FLIGHT.index(r["status"]),) + _sort_key(r))

        generated = datetime.now(timezone.utc).isoformat(timespec="minutes")
        parts = [
            _header_html(stats["total"], passed_gate, scored_matches, stats["by_status"],
                         last_scan, generated),
            _top_html(top, top_n, store, packets_dir),
            _inflight_html(in_flight, store, packets_dir),
            _health_html(last_scan),
        ]
    finally:
        store.close()

    page = (
        '<!DOCTYPE html>\n<html lang="en"><head>'
        '<meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta name="color-scheme" content="dark">'
        '<title>Job pipeline</title>'
        f'<style>{_CSS}</style>'
        '</head><body><div class="wrap">'
        + "".join(parts)
        + f'<footer>generated {_esc(generated)} UTC from the local jobsearch database · {_esc(FOOTER_NOTE)}</footer>'
        + '</div><script>' + _FILTER_JS + '</script></body></html>\n'
    )
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(page)
    return out_path
