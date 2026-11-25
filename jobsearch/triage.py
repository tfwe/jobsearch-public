"""The triage screen: sidebar, dense list, preview pane.

Replaces dashboard.html as `/`. Everything here is a read of the store plus the
existing scorer; the only writes are the two status forms, which POST to the same
`/api/postings/UID/status` endpoint the CLI and dashboard use, so the events trail is
unchanged. No JS is required for any of it: rows are links, actions are forms.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

from jobsearch import checks
from jobsearch.dashboard import ALLOWED_NEXT
from jobsearch.match import SCORE_SCALE, load_preferences, load_profile, score_parts
from jobsearch.reach import OUT_OF_REACH_LEVELS, classify
from jobsearch.store import Store
from jobsearch.ui import bar, btn, card, esc, flag, kbd, rel_age, rel_phrase, score_pill, \
    theme_toggle

DEFAULT_THRESHOLD = 15.0

# The nine store statuses mapped onto the sidebar. Every status appears exactly once.
NAV = (
    ("Triage", (
        ("inbox", "Inbox", ("new",)),
        ("shortlisted", "Shortlisted", ("shortlisted",)),
    )),
    ("In motion", (
        ("prepped", "Prepped", ("prepped",)),
        ("applied", "Applied", ("applied",)),
        ("interview", "Interviewing", ("interview",)),
        ("offer", "Offer", ("offer",)),
    )),
    ("Closed", (
        ("skipped", "Passed", ("skipped",)),
        ("rejected", "Rejected/withdrawn", ("rejected", "closed")),
    )),
)
VIEW_STATUSES = {key: statuses for _, items in NAV for key, _label, statuses in items}
VIEW_LABEL = {key: label for _, items in NAV for key, label, _s in items}

LIST_LIMIT = 200
REACH_VIEWS = ("inbox", "shortlisted")
AGE_FILTERS = (("1", "Posted today", 1), ("7", "Posted < 7 days", 7), ("30", "Posted < 30 days", 30))


def parse_filters(params: dict[str, list[str]]) -> dict:
    """Normalise the GET params into one dict. Unknown values fall back, never 500."""
    def one(name: str) -> str:
        return (params.get(name) or [""])[0].strip()

    view = one("view")
    if view not in VIEW_STATUSES:
        view = "inbox"
    score = one("score")
    try:
        threshold = float(score) if score else DEFAULT_THRESHOLD
    except ValueError:
        threshold = DEFAULT_THRESHOLD
    threshold = max(0.0, min(100.0, threshold))
    days = one("days")
    if days not in {key for key, _l, _n in AGE_FILTERS}:
        days = ""
    return {
        "view": view,
        "q": one("q")[:120],
        "score": threshold,
        "remote": one("remote") in ("1", "on", "true"),
        "source": one("source")[:60],
        "nofr": one("nofr") in ("1", "on", "true"),
        "days": days,
        "show": one("show") in ("1", "on", "true"),
        "reach": one("reach") != "0",
        "sel": one("sel")[:200],
    }


def _reach_on(value) -> bool:
    """The reach filter is on unless it is the explicit off value. '' means the default (on)."""
    return value not in (False, 0, "0")


def list_url(filters: dict, **changes) -> str:
    """The same view with some params changed. Empty params are dropped."""
    merged = dict(filters)
    merged.update(changes)
    pairs = []
    if merged.get("view") and merged["view"] != "inbox":
        pairs.append(("view", merged["view"]))
    if merged.get("q"):
        pairs.append(("q", merged["q"]))
    if merged.get("score") not in (None, "") and float(merged["score"]) != DEFAULT_THRESHOLD:
        pairs.append(("score", f"{float(merged['score']):g}"))
    if merged.get("remote"):
        pairs.append(("remote", "1"))
    if merged.get("source"):
        pairs.append(("source", merged["source"]))
    if merged.get("nofr"):
        pairs.append(("nofr", "1"))
    if merged.get("days"):
        pairs.append(("days", merged["days"]))
    if merged.get("show"):
        pairs.append(("show", "1"))
    if merged.get("sel"):
        pairs.append(("sel", merged["sel"]))
    if not _reach_on(merged.get("reach", True)):
        pairs.append(("reach", "0"))
    query = urlencode(pairs)
    return "/?" + query if query else "/"


def _query_kwargs(filters: dict) -> dict:
    """Store.browse arguments for a filter dict (the list and its count share these)."""
    kwargs = {
        "statuses": VIEW_STATUSES[filters["view"]],
        # The floor is the inbox's own rule; a prepped posting at score 12 must still
        # show up in Prepped, so the other views carry no score floor.
        "min_score": (None if filters["show"] else (filters["score"] or None)
                      if filters["view"] == "inbox" else None),
        "query": filters["q"] or None,
        "source": filters["source"] or None,
        "remote_canada": filters["remote"],
        "exclude_french": filters["nofr"],
        "french_sql": checks.french_sql(),
        "hide_zero": not filters["show"],
        # Reach narrows only the two views people work from; prepped and later stages
        # keep every posting, so nothing already in motion disappears.
        "reach_only": (filters["view"] in REACH_VIEWS
                       and _reach_on(filters.get("reach", True))),
    }
    if filters["days"]:
        days = next(n for key, _label, n in AGE_FILTERS if key == filters["days"])
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
        kwargs["posted_after"] = cutoff
    return kwargs


def packet_dir_name(uid: str) -> str:
    """Directory name for a posting's packet: every '/' and ':' becomes '_'."""
    return uid.replace("/", "_").replace(":", "_")


def enrichment(uid: str, packets_root) -> dict:
    """Whether a posting already has its AI artifacts: analysis.md and cover_letter.pdf."""
    empty = {"has_analysis": False, "has_letter": False, "analysis_mtime": None}
    if packets_root is None or not uid:
        return empty
    folder = Path(packets_root) / packet_dir_name(uid)
    analysis = folder / "analysis.md"
    has_analysis = analysis.is_file()
    mtime = analysis.stat().st_mtime if has_analysis else None
    return {"has_analysis": has_analysis,
            "has_letter": (folder / "cover_letter.pdf").is_file(),
            "analysis_mtime": (datetime.fromtimestamp(mtime, timezone.utc).isoformat()
                                if mtime is not None else None)}


def next_uid(store: Store, uid: str, filters: dict, limit: int = LIST_LIMIT) -> str:
    """The uid after `uid` in the same filtered order, or '' when it is last.

    Used by the workspace's skip and apply buttons so a decision advances to the
    next row instead of dropping back to the list.
    """
    rows = store.browse(**_query_kwargs(filters), limit=limit)
    uids = [str(r.get("uid") or "") for r in rows]
    if uid in uids and uids.index(uid) + 1 < len(uids):
        return uids[uids.index(uid) + 1]
    return ""


def build_rows(store: Store, filters: dict, packets_root=None) -> tuple[list[dict], int]:
    """Slim rows for the list plus the total the filters select. Each row gets has_analysis/has_letter."""
    kwargs = _query_kwargs(filters)
    rows = store.browse(**kwargs, limit=LIST_LIMIT)
    total = store.browse_count(**kwargs)
    for row in rows:
        flags = enrichment(row.get("uid") or "", packets_root)
        row["has_analysis"] = flags["has_analysis"]
        row["has_letter"] = flags["has_letter"]
    return rows, total


def build_counts(store: Store, filters: dict) -> dict[str, int]:
    """Sidebar counts, computed with the same thresholds the views use."""
    counts = {}
    reach_on = _reach_on(filters.get("reach", True))
    for key, statuses in VIEW_STATUSES.items():
        kwargs = {"statuses": statuses, "hide_zero": key != "inbox"}
        if key == "inbox":
            kwargs["min_score"] = DEFAULT_THRESHOLD
        if key in REACH_VIEWS:
            kwargs["reach_only"] = reach_on
        counts[key] = store.browse_count(**kwargs)
    return counts


def hidden_count(store: Store, filters: dict) -> int:
    """How many postings this view holds back for being unscored or below the floor.

    Counted against the same filters with the score rules lifted, so the number matches
    exactly what the "show" link puts on screen.
    """
    if filters["show"]:
        return 0
    kwargs = _query_kwargs(filters)
    lifted = _query_kwargs({**filters, "show": True})
    return max(0, store.browse_count(**lifted) - store.browse_count(**kwargs))


def hidden_reach(store: Store, filters: dict) -> int:
    """How many postings the reach filter alone holds back. The score rules stay as set."""
    kwargs = _query_kwargs(filters)
    if not kwargs.get("reach_only"):
        return 0
    lifted = {**kwargs, "reach_only": False}
    return max(0, store.browse_count(**lifted) - store.browse_count(**kwargs))


def level_badge(row: dict) -> tuple[str, str]:
    """(text, css class) for the reach badge beside a row title. ('', '') when in reach."""
    level = row.get("level")
    years = row.get("level_years")
    if level in OUT_OF_REACH_LEVELS:
        return f"{level}-level", "out"
    if years is not None and years >= 5:
        return f"asks {years}+ yrs", "out"
    if level == "senior":
        return "senior-level", "warn"
    if years in (3, 4):
        return f"asks {years} yrs", "warn"
    return "", ""


def row_flags(row: dict) -> list[str]:
    """Blocker flags for a list row: French, seniority. Both are advisory."""
    out = []
    if row.get("needs_french"):
        out.append("French required")
    senior = checks.seniority_flag(row.get("title"))
    if senior:
        out.append(f"Seniority: {senior}")
    return out


def _scan_html(scan: dict) -> str:
    dot = "warn" if scan.get("stale") else ""
    if scan.get("busy"):
        line = "Scan running"
        sub = scan.get("progress") or "starting"
    elif scan.get("age_phrase"):
        line = f"Scan finished {scan['age_phrase']}"
        sub = scan.get("sub") or ""
    else:
        line = "No scan recorded"
        sub = scan.get("sub") or ""
    button = (
        '<button class="btn btn-primary" type="submit" data-scan-start>Scan now</button>'
        if scan.get("enabled") else
        '<button class="btn" type="button" disabled aria-disabled="true" '
        'title="Scanning makes outbound requests, so the server only runs it when '
        'JOBSEARCH_SERVE_SCAN=1">Scan disabled</button>'
    )
    note = "" if scan.get("enabled") else (
        '<div class="scan-sub">Scanning is off on this server. Run the CLI scan, or '
        "start the server with JOBSEARCH_SERVE_SCAN=1.</div>"
    )
    return (
        f'<div class="scan"><div class="scan-line"><span class="dot {dot}"></span>'
        f"{esc(line)}</div>"
        f'<div class="scan-sub">{esc(sub)}</div>'
        '<form method="post" action="/api/scan" data-scan-form>'
        '<input type="hidden" name="return" value="/">'
        f"{button}</form>"
        f'<div data-scan-progress class="scan-sub" role="status"></div>'
        f"{note}</div>"
    )


def _nav_html(counts: dict[str, int], active: str) -> str:
    groups = []
    for title, items in NAV:
        links = []
        for key, label, _statuses in items:
            on = " on" if key == active else ""
            aria = ' aria-current="page"' if key == active else ""
            links.append(
                f'<a class="nav-i{on}" href="{esc(list_url({"view": key}))}"{aria}>'
                f"<span>{esc(label)}</span>"
                f'<span class="n" data-count-for="{esc(key)}">{counts.get(key, 0)}</span></a>')
        groups.append(f'<div class="nav-group"><div class="nav-t">{esc(title)}</div>'
                      + "".join(links) + "</div>")
    return "".join(groups)


def _chips_html(filters: dict, sources: list[str]) -> str:
    chips = []
    reach_on = _reach_on(filters.get("reach", True))
    chips.append(
        f'<a class="chip{" on" if reach_on else ""}" '
        f'href="{esc(list_url(filters, reach="0" if reach_on else ""))}">'
        "In reach only" + ('<span class="x">&times;</span>' if reach_on else "") + "</a>")
    score_on = float(filters["score"]) != DEFAULT_THRESHOLD or filters["score"] > 0
    chips.append(
        f'<a class="chip{" on" if score_on else ""}" '
        f'href="{esc(list_url(filters, score="" if score_on else f"{DEFAULT_THRESHOLD:g}", view="inbox"))}">'
        f"Score &ge; {int(filters['score'])}"
        + ('<span class="x">&times;</span>' if score_on else "") + "</a>")
    chips.append(
        f'<a class="chip{" on" if filters["remote"] else ""}" '
        f'href="{esc(list_url(filters, remote="" if filters["remote"] else "1"))}">'
        "Remote / Canada" + ('<span class="x">&times;</span>' if filters["remote"] else "")
        + "</a>")
    src_label = filters["source"] or "All sources"
    src_links = "".join(
        f'<a class="nav-i{" on" if s == filters["source"] else ""}" '
        f'href="{esc(list_url(filters, source=s))}">{esc(s.capitalize())}</a>'
        for s in sources)
    all_off = list_url(filters, source="")
    chips.append(
        f'<details class="chip-menu"><summary class="chip{" on" if filters["source"] else ""}">'
        f"Source: {esc(src_label)}"
        + ('<span class="x">&times;</span>' if filters["source"] else "") + "</summary>"
        f'<div class="menu"><a class="nav-i{" on" if not filters["source"] else ""}" '
        f'href="{esc(all_off)}">All sources</a>{src_links}</div></details>')
    chips.append(
        f'<a class="chip{" on" if filters["nofr"] else ""}" '
        f'href="{esc(list_url(filters, nofr="" if filters["nofr"] else "1"))}">'
        "No French required" + ('<span class="x">&times;</span>' if filters["nofr"] else "")
        + "</a>")
    days_label = next((label for key, label, _n in AGE_FILTERS if key == filters["days"]),
                      "Posted < 7 days")
    chips.append(
        f'<a class="chip{" on" if filters["days"] else ""}" '
        f'href="{esc(list_url(filters, days="" if filters["days"] else "7"))}">'
        f"{esc(days_label)}"
        + ('<span class="x">&times;</span>' if filters["days"] else "") + "</a>")
    if filters["show"]:
        chips.append('<a class="chip on" href="' + esc(list_url(filters, show="")) + '">'
                     'Including ineligible<span class="x">&times;</span></a>')
    return "".join(chips)


def _enr_parts(row: dict) -> tuple[str, str]:
    """(extra row class, chip markup) for a row whose posting already has AI artifacts."""
    if row.get("has_analysis"):
        return " row-enr", '<span class="enr-chip" title="AI fit analysis ready">analyzed</span>'
    if row.get("has_letter"):
        return "", '<span class="enr-chip enr-letter" title="Cover letter ready">letter</span>'
    return "", ""


def _rows_html(rows: list[dict], filters: dict, selected: str) -> str:
    if not rows:
        return ('<div class="empty"><b>Nothing here</b>'
                "No posting matches this view and these filters.</div>")
    out = []
    for row in rows:
        uid = row.get("uid") or ""
        sel = " sel" if uid == selected else ""
        flags = "".join(flag(text) for text in row_flags(row))
        badge_text, badge_cls = level_badge(row)
        badge = (f'<span class="lvl lvl-{badge_cls}">{esc(badge_text)}</span>'
                 if badge_text else "")
        enr_cls, enr_chip = _enr_parts(row)
        row_cls = "row" + sel + (" row-out" if badge_cls == "out" else "") + enr_cls
        age = rel_age(row.get("posted_at") or row.get("first_seen"))
        source = row.get("source") or ""
        out.append(
            f'<a class="{row_cls}" href="{esc(list_url(filters, sel=uid))}" '
            f'data-uid="{esc(uid)}" data-sel="{"1" if sel else "0"}">'
            f"{score_pill(row.get('score'))}"
            f'<span class="cell-t"><span class="r-title">{esc(row.get("title") or "—")}{enr_chip}{badge}{flags}'
            f'</span><span class="r-co">{esc(row.get("company") or "—")}</span></span>'
            f'<span class="cell-l"><span class="r-loc">{esc(row.get("location") or "—")}</span>'
            f'<span class="r-src">{esc(source)}</span></span>'
            f'<span class="r-age">{esc(age)}</span></a>')
    return "".join(out)


def _mins_text(secs) -> str:
    secs = int(secs or 0)
    if secs <= 0:
        return "soon"
    return f"about {-(-secs // 60)} min"


def _clock(secs) -> str:
    secs = max(0, int(secs or 0))
    return f"{secs // 60:02d}:{secs % 60:02d}"


def _qp_label(job: dict) -> str:
    co, title = job.get("company") or "", job.get("title") or ""
    if co and title:
        return f"{co} — {title}"
    return co or title or job.get("uid") or ""


def queue_html(panel: dict | None) -> str:
    """The AI queue section. Hidden when nothing is active; /api/jobs polling rebuilds it."""
    panel = panel or {}
    running = panel.get("running")
    queued = panel.get("queued") or []
    count = int(panel.get("count") or 0)
    sec_hidden = "" if count else " hidden"
    # The muted idle line exists only when nothing is active; a live panel never carries it.
    idle_line = "" if count else '<div class="qp-empty muted" data-qp-empty>queue empty</div>'
    run_attr, run_inner = " hidden", ""
    if running:
        left = int(running.get("eta_s") or 0)
        left_text = "finishing" if left <= 0 else f"about {-(-left // 60)} min left"
        href = "/posting/" + _q(running.get("uid") or "")
        run_attr = ""
        run_inner = (
            f'<a href="{esc(href)}">{esc(_qp_label(running))}</a>'
            f'<span class="qp-kind">{esc(running.get("kind_label") or running.get("kind") or "")}</span>'
            f'<span class="qp-stage">{esc(running.get("stage_label") or "")}</span>'
            f'<span class="qp-clock" data-qp-clock data-started="{esc(running.get("started") or "")}">'
            f'{_clock(running.get("elapsed_s"))}</span>'
            f'<span class="qp-left" data-qp-left>{esc(left_text)}</span>')
    items = "".join(
        f'<li class="qp-q"><span class="qp-pos">#{int(j.get("position") or 0)}</span>'
        f'<a href="/posting/{esc(_q(j.get("uid") or ""))}">{esc(_qp_label(j))}</a>'
        f'<span class="qp-kind">{esc(j.get("kind_label") or j.get("kind") or "")}</span>'
        f'<span class="qp-when">{esc(_mins_text(j.get("eta_s")))}</span></li>'
        for j in queued)
    return (
        f'<section class="queue-panel" data-queue-panel aria-label="AI queue"{sec_hidden}>'
        '<h2 class="qp-t">Queue</h2>'
        f'<div class="qp-item qp-run" data-qp-running{run_attr}>{run_inner}</div>'
        f'<ul class="qp-list" data-qp-list>{items}</ul>'
        + idle_line +
        "</section>")


def render_triage(ctx: dict) -> str:
    """The page body. serve.py appends the script and wraps it in ui.page()."""
    filters = ctx["filters"]
    total = ctx["total"]
    hidden = ctx["hidden"]
    shown = len(ctx["rows"])
    sel = ctx["selected"]

    more = "" if total <= shown else f' <span class="muted">(showing {shown})</span>'
    hidden_html = ""
    if hidden:
        hidden_html = (
            f'<div class="hidden-note">{hidden} ineligible posting{"" if hidden == 1 else "s"} '
            f'hidden. <a href="{esc(list_url(filters, show="1"))}">show</a></div>')

    reach_hidden = ctx.get("reach_hidden", 0)
    if reach_hidden:
        hidden_html += (
            f'<div class="hidden-note">{reach_hidden} out of reach hidden. '
            f'<a href="{esc(list_url(filters, reach="0"))}">show</a></div>')

    preview = ctx.get("preview") or (
        '<div class="pv-empty">Select a posting to see it here.</div>')

    scan = ctx["scan"]
    sidebar = (
        '<aside class="sidebar" id="sidebar">'
        '<div class="brand">'
        '<svg viewBox="0 0 16 16" aria-hidden="true" fill="none" stroke="currentColor" '
        'stroke-width="1.5"><path d="M2 4h12M2 8h8M2 12h11"/></svg>'
        "Job board</div>"
        + _scan_html(scan)
        + f'<nav class="nav" aria-label="Views">{_nav_html(ctx["counts"], filters["view"])}</nav>'
        '<div class="side-foot">'
        '<div class="side-row"><a class="link-quiet" href="/stats">Stats and follow-ups</a></div>'
        '<div class="side-row"><a class="link-quiet" href="/facts">Facts and preferences</a></div>'
        + hidden_html
        + '<div class="side-row">' + theme_toggle()
        + '<button class="btn btn-sm btn-ghost side-toggle" type="button" data-side-toggle '
          'aria-controls="sidebar">Views</button></div>'
        "</div></aside>"
    )

    head = (
        '<header class="list-head">'
        '<form class="search" method="get" action="/" role="search">'
        '<svg viewBox="0 0 16 16" aria-hidden="true" fill="none" stroke="currentColor" '
        'stroke-width="1.6"><circle cx="7" cy="7" r="4.5"/><path d="M10.5 10.5L14 14"/></svg>'
        f'<label class="sr" for="q">Search title, company or location</label>'
        f'<input id="q" name="q" type="search" value="{esc(filters["q"])}" '
        'placeholder="Search title, company, location" autocomplete="off" '
        'data-search>'
        + _search_hidden(filters)
        + '<span class="kbd" aria-hidden="true">/</span>'
        "</form>"
        f'<div class="chips">{_chips_html(filters, ctx["sources"])}</div>'
        '<div class="list-meta"><span>'
        f'<strong class="mono">{total}</strong> to review{more}'
        f' &middot; sorted by score</span>'
        f'<span class="mono">view: {esc(VIEW_LABEL[filters["view"]])}</span></div>'
        "</header>"
    )

    foot = (
        '<footer class="list-foot">'
        + kbd("J", "K move") + kbd("S", "shortlist") + kbd("X", "pass")
        + kbd("Enter", "workspace") + kbd("O", "original") + kbd("/", "search")
        + '<span style="margin-left:auto">Score is a reading order, not a verdict.</span>'
        "</footer>"
    )

    main = ('<main class="main" id="list">' + head
            + queue_html(ctx.get("queue_panel"))
            + f'<div class="rows" data-rows>{_rows_html(ctx["rows"], filters, filters["sel"])}</div>'
            + foot + "</main>")

    pane = (
        f'<section class="preview" id="preview" aria-label="Selected posting">{preview}</section>')

    return ('<a class="skip" href="#list">Skip to the list</a>'
            f'<div class="app">{sidebar}{main}{pane}</div>'
            '<div class="scrim" data-scrim></div>'
            '<div class="toast" data-toast role="status" aria-live="polite"></div>')


# ---------- preview pane ----------

def build_preview(store: Store, uid: str, filters: dict, packets_root=None) -> dict | None:
    """Everything the right pane shows for one posting, or None when it is gone."""
    posting = store.get(uid)
    if posting is None:
        return None
    profile = load_profile()
    prefs = load_preferences()
    parts = score_parts(posting, profile, prefs=prefs)
    quote = checks.french_quote(posting.get("description"))
    return {
        "posting": posting,
        "parts": parts,
        "prefs": prefs,
        "french_quote": quote,
        "french_text": checks.looks_french(posting.get("description")),
        "seniority": checks.seniority_flag(posting.get("title")),
        "reach": classify(posting.get("title") or "", posting.get("description") or ""),
        "filters": filters,
        "allowed_next": ALLOWED_NEXT.get(posting.get("status") or "", ()),
        **enrichment(uid, packets_root),
    }


def _check_lines(p: dict) -> str:
    """The 'before you spend time' list: gate, language, seniority, experience."""
    rows = []
    parts = p["parts"]
    posting = p["posting"]
    eligible = parts.get("eligible", True)
    if eligible:
        rows.append(("ok", "Location passes the gate", posting.get("location") or "no location"))
    else:
        rows.append(("warn", "Outside the gate", parts.get("why") or "ineligible"))
    if "CA" in ((p.get("prefs") or {}).get("countries") or []):
        rows.append(("ok", "Relocation open (CA)", "preferences.yaml lists CA under countries"))
    if p["french_quote"]:
        rows.append(("warn", "French looks required", p["french_quote"]))
    elif p.get("french_text"):
        rows.append(("warn", "The posting text is in French",
                     "no requirement phrased, but the body is French"))
    else:
        rows.append(("ok", "No French requirement found", "phrase check on the posting text"))
    if p["seniority"]:
        rows.append(("warn", f"Seniority: {p['seniority']}", "the title asks for a level above target"))
    reach = p.get("reach")
    if reach:
        if reach["verdict"] == "out_of_reach":
            rows.append(("warn", "Out of reach", reach["why"] or "out of reach"))
        elif reach["verdict"] == "stretch":
            rows.append(("warn", "Stretch", reach["why"] or "stretch"))
        else:
            rows.append(("ok", "In reach", "no level barrier found"))
    exp = next((r for r in parts.get("reasons") or [] if r.startswith("experience:")), "")
    if exp:
        rows.append(("warn", "Experience asked", exp))
    days = checks.posted_days(posting.get("posted_at"))
    if days is not None and days > 21:
        rows.append(("warn", "Older posting", checks.posted_phrase(posting.get("posted_at"))))
    if p.get("has_analysis"):
        ago = rel_phrase(p.get("analysis_mtime"))
        rows.append(("ok", "Analyzed", f"analysis written {ago}" if ago else "analysis.md is on disk"))
    out = []
    for kind, label, detail in rows:
        mark = "!" if kind == "warn" else "&#10003;"
        out.append(f'<div class="check {kind}"><span class="mk">{mark}</span>'
                   f'<span><strong>{esc(label)}</strong>'
                   + (f'<br><span class="meta">{esc(detail)}</span>' if detail else "")
                   + "</span></div>")
    return "".join(out)


def _search_hidden(filters: dict) -> str:
    """Hidden inputs that carry every other filter through a search. q is the visible box.

    Set filters are emitted; off ones are left out, except reach, which is always sent so a
    search cannot turn it back on.
    """
    pairs = [("view", filters["view"]), ("score", f"{float(filters['score']):g}")]
    if filters.get("remote"):
        pairs.append(("remote", "1"))
    if filters.get("source"):
        pairs.append(("source", filters["source"]))
    if filters.get("nofr"):
        pairs.append(("nofr", "1"))
    if filters.get("days"):
        pairs.append(("days", filters["days"]))
    if filters.get("show"):
        pairs.append(("show", "1"))
    pairs.append(("reach", "1" if _reach_on(filters.get("reach", True)) else "0"))
    return "".join(f'<input type="hidden" name="{name}" value="{esc(value)}">'
                   for name, value in pairs)


def _status_forms(posting: dict, filters: dict) -> str:
    """Shortlist / Pass as real forms, plus the open-workspace link."""
    uid = posting.get("uid") or ""
    action = "/api/postings/" + _q(uid) + "/status"
    back = list_url(filters, sel=uid)
    allowed = ALLOWED_NEXT.get(posting.get("status") or "", ())
    parts = []
    if "shortlisted" in allowed:
        parts.append(
            f'<form method="post" action="{esc(action)}" data-quick data-status="shortlisted">'
            '<input type="hidden" name="status" value="shortlisted">'
            f'<input type="hidden" name="return" value="{esc(back)}">'
            '<input type="hidden" name="from" value="' + esc(posting.get("status") or "") + '">'
            '<button class="btn" type="submit">Shortlist <span class="kbd">S</span></button>'
            "</form>")
    if "skipped" in allowed:
        parts.append(
            f'<form method="post" action="{esc(action)}" data-quick data-status="skipped">'
            '<input type="hidden" name="status" value="skipped">'
            f'<input type="hidden" name="return" value="{esc(back)}">'
            '<input type="hidden" name="from" value="' + esc(posting.get("status") or "") + '">'
            '<button class="btn" type="submit">Pass <span class="kbd">X</span></button>'
            "</form>")
    return "".join(parts)


def _q(value: str) -> str:
    from urllib.parse import quote
    return quote(value, safe="")


def _analyze_html(p: dict) -> str:
    """Analyze fit, plus the override when reach holds the posting back."""
    uid = p["posting"].get("uid") or ""
    action = esc("/api/postings/" + _q(uid) + "/action")
    label = "Re-run analysis" if p.get("has_analysis") else "Analyze fit"
    html = (f'<form method="post" action="{action}" data-quick-action>'
            '<input type="hidden" name="kind" value="analysis">'
            f'<button class="btn btn-primary" type="submit">{label}</button></form>')
    reach = p.get("reach") or {}
    if reach.get("verdict") == "out_of_reach":
        html += (f'<span class="meta">Held back: {esc(reach.get("why") or "out of reach")}</span>'
                 f'<form method="post" action="{action}" data-quick-action>'
                 '<input type="hidden" name="kind" value="analysis">'
                 '<input type="hidden" name="force" value="1">'
                 '<button class="btn" type="submit">Run anyway</button></form>')
    return f'<div class="btn-row">{html}</div>'


def render_preview(p: dict) -> str:
    """The preview pane for one posting (also served alone as a fragment)."""
    posting = p["posting"]
    parts = p["parts"]
    filters = p["filters"]
    uid = posting.get("uid") or ""
    score = float(posting.get("score") or 0)
    url = posting.get("url") or ""
    is_http = url.startswith("http://") or url.startswith("https://")

    meta = " &middot; ".join(
        [esc(posting.get("company") or "—"), esc(posting.get("source") or "—"),
         esc(checks.posted_phrase(posting.get("posted_at")))])
    sub = f'{esc(posting.get("location") or "—")} &middot; status: {esc(posting.get("status") or "—")}'

    if parts.get("eligible", True):
        bars = (bar("Skills overlap", round(parts.get("skills", 0), 1), maximum=SCORE_SCALE)
                + bar("Role match", round(parts.get("role", 0), 1), maximum=SCORE_SCALE)
                + bar("Location gate", round(parts.get("location", 0), 1), maximum=SCORE_SCALE)
                + bar("Seniority demanded", round(parts.get("seniority", 0), 1),
                      maximum=SCORE_SCALE, negative=parts.get("seniority", 0) < 0))
        score_body = (f'<div class="big">{esc(f"{score:g}")}'
                      f"<small> / {esc(f'{SCORE_SCALE:g}')} scale</small></div>"
                      f'<div class="bars">{bars}</div>'
                      '<div class="caption">A prompt for where to look first, not a verdict '
                      "on fit.</div>")
    else:
        score_body = (f'<div class="big">0</div>'
                      f'<div class="meta" style="margin-top:4px">{esc(parts.get("why") or "ineligible")}'
                      "</div>"
                      '<div class="caption">It is in the store, but nothing about it invites a '
                      "read first.</div>")

    actions = []
    actions.append(btn("Open workspace", href=f'/posting/{_q(uid)}', primary=True,
                       attrs='data-open-workspace'))
    actions.append(_analyze_html(p))
    forms = _status_forms(posting, filters)
    if forms:
        actions.append(f'<div class="btn-row">{forms}</div>')
    if is_http:
        actions.append(f'<a class="link-quiet" href="{esc(url)}" target="_blank" '
                       'rel="noopener" data-original>Open original posting (you submit there)</a>')
    else:
        actions.append('<span class="meta">No usable posting URL stored.</span>')

    return (
        '<div class="pv-top"><span class="card-t">Preview</span>'
        '<button class="btn btn-sm btn-ghost pv-close" type="button" data-pv-close '
        'aria-controls="preview">Close</button></div>'
        '<div class="pv-scroll"><div class="pv-body">'
        f'<div class="pv-meta">{meta}</div>'
        f'<h2 class="h2">{esc(posting.get("title") or "—")}</h2>'
        f'<div class="pv-sub">{sub}</div>'
        + card("Reading-order score", score_body)
        + card("Before you spend time", f'<div class="checks">{_check_lines(p)}</div>')
        + "</div></div>"
        f'<div class="actions" data-actions="{esc(uid)}">{"".join(actions)}</div>')


# ---------- script ----------

TRIAGE_JS = r"""<script>
(function () {
  "use strict";
  // Enhancement only: rows, actions and filters all work without this file's help.
  var rowsBox = document.querySelector("[data-rows]");
  var preview = document.getElementById("preview");
  if (!rowsBox || !preview) { return; }

  var SEARCH = document.querySelector("[data-search]");
  var toast = document.querySelector("[data-toast]");
  var scrim = document.querySelector("[data-scrim]");
  var toastTimer = null, toastUndo = null;

  // A bookmark that keeps credentials in the URL makes fetch throw in Chromium
  // ("Request cannot be constructed from a URL that includes credentials"), and the
  // failure is silent: the queue panel and the preview just stop updating. Every
  // request resolves against the page and drops the userinfo first.
  function cleanUrl(u) {
    try {
      var url = new URL(u, window.location.href);
      url.username = "";
      url.password = "";
      return url.toString();
    } catch (e) { return u; }
  }

  function rows() { return Array.prototype.slice.call(rowsBox.querySelectorAll(".row")); }
  function rowUid(row) { return row ? row.getAttribute("data-uid") : ""; }
  function selectedRow() { return rowsBox.querySelector(".row.sel"); }

  // -- preview --------------------------------------------------------------
  // Below 1100px the preview is a slide-over and needs the dim backdrop; above it the
  // pane is a column, and dimming the page over it would hide the very thing that
  // changed. The stylesheet scopes .scrim.on to the same breakpoint; this keeps the
  // class off entirely on a wide window.
  function slideOver() {
    return !!(window.matchMedia && window.matchMedia("(max-width:1100px)").matches);
  }

  function markSelected(uid) {
    rows().forEach(function (r) {
      var on = rowUid(r) === uid;
      r.classList.toggle("sel", on);
      r.setAttribute("data-sel", on ? "1" : "0");
    });
  }

  // The pane swap must never be a dead end. If the fetch is missing, refused, not ok,
  // or throws, fall back to plain navigation: the row's own href renders the same
  // page with that posting selected, so the click always changes something.
  function loadPreview(uid) {
    var row = rowByUid(uid);
    function fallback() {
      if (row && row.href) { window.location.href = row.href; }
    }
    if (!uid || !window.fetch) { fallback(); return; }
    var url = "/preview/" + encodeURIComponent(uid);
    fetch(cleanUrl(url), {headers: {"Accept": "text/html"}, credentials: "same-origin",
                cache: "no-store"})
      .then(function (res) {
        if (!res.ok) { throw new Error("HTTP " + res.status); }
        return res.text();
      })
      .then(function (text) {
        markSelected(uid);
        preview.innerHTML = text;
        preview.classList.add("open");
        if (scrim && slideOver()) { scrim.classList.add("on"); }
      })
      .catch(fallback);
  }

  function rowByUid(uid) {
    var list = rows();
    for (var i = 0; i < list.length; i += 1) {
      if (rowUid(list[i]) === uid) { return list[i]; }
    }
    return null;
  }

  function move(delta) {
    var list = rows();
    if (!list.length) { return; }
    var at = list.indexOf(selectedRow());
    var next = at < 0 ? (delta > 0 ? 0 : list.length - 1) : at + delta;
    if (next < 0 || next >= list.length) { return; }
    var row = list[next];
    if (row.scrollIntoView) { row.scrollIntoView({block: "nearest"}); }
    loadPreview(rowUid(row));
  }

  rowsBox.addEventListener("click", function (ev) {
    // Modified clicks (new tab, middle click) keep the browser's own behaviour.
    if (ev.defaultPrevented || ev.button !== 0 || ev.metaKey || ev.ctrlKey || ev.shiftKey ||
        ev.altKey) { return; }
    var row = ev.target.closest ? ev.target.closest(".row") : null;
    if (!row || !rowUid(row)) { return; }
    ev.preventDefault();
    loadPreview(rowUid(row));
  });

  document.addEventListener("click", function (ev) {
    if (ev.target.closest && ev.target.closest("[data-pv-close]")) {
      preview.classList.remove("open");
      if (scrim) { scrim.classList.remove("on"); }
    }
  });

  // -- toast ---------------------------------------------------------------
  function say(message, undoFn) {
    if (!toast) { return; }
    toast.textContent = "";
    var span = document.createElement("span");
    span.textContent = message;
    toast.appendChild(span);
    toastUndo = undoFn || null;
    if (undoFn) {
      var b = document.createElement("button");
      b.type = "button";
      b.className = "undo";
      b.textContent = "Undo";
      b.addEventListener("click", function () { undoFn(); hide(); });
      toast.appendChild(b);
    }
    toast.classList.add("on");
    if (toastTimer) { clearTimeout(toastTimer); }
    toastTimer = setTimeout(hide, 5000);
  }
  function hide() {
    if (toast) { toast.classList.remove("on"); }
    toastUndo = null;
    if (toastTimer) { clearTimeout(toastTimer); toastTimer = null; }
  }

  // -- status writes -------------------------------------------------------
  function postStatus(form) {
    return fetch(cleanUrl(form.getAttribute("action")), {
      method: "POST",
      body: new URLSearchParams(new FormData(form)),
      headers: {"Accept": "application/json"},
      credentials: "same-origin"
    }).then(function (res) {
      return res.json().catch(function () { return {}; }).then(function (data) {
        if (!res.ok || data.ok === false) { throw new Error(data.error || ("HTTP " + res.status)); }
        return data;
      });
    });
  }

  function bumpCounts(fromStatus, toStatus) {
    function bump(name, delta) {
      var el = document.querySelector('[data-count-for="' + name + '"]');
      if (!el) { return; }
      var n = parseInt(el.textContent, 10) || 0;
      el.textContent = String(Math.max(0, n + delta));
    }
    function viewFor(status) {
      if (status === "new") { return "inbox"; }
      if (status === "rejected" || status === "closed") { return "rejected"; }
      return status;
    }
    if (fromStatus) { bump(viewFor(fromStatus), -1); }
    if (toStatus) { bump(viewFor(toStatus), 1); }
  }

  function quickAction(form) {
    // The row and its uid are taken now, at submit: the selection may move while the request runs.
    var row = selectedRow();
    var uid = rowUid(row);
    var from = form.querySelector('input[name="from"]');
    var to = form.querySelector('input[name="status"]');
    var fromStatus = from ? from.value : "";
    var toStatus = to ? to.value : "";
    postStatus(form).then(function () {
      bumpCounts(fromStatus, toStatus);
      var label = toStatus === "shortlisted" ? "Shortlisted" : "Passed";
      if (row && row.parentNode) { row.parentNode.removeChild(row); }
      move(0);
      say(label + " one posting.", function () {
        var back = document.createElement("form");
        back.method = "post";
        back.action = "/api/postings/" + encodeURIComponent(uid) + "/status";
        var s = document.createElement("input");
        s.type = "hidden"; s.name = "status"; s.value = fromStatus; back.appendChild(s);
        postStatus(back).then(function () {
          bumpCounts(toStatus, fromStatus);
          say("Restored to " + fromStatus + ". Reload to bring the row back.", null);
        }, function (err) { say("Undo failed: " + err.message, null); });
      });
    }, function (err) {
      say("Could not change the status: " + err.message, null);
    });
  }

  // -- analysis queue ------------------------------------------------------
  // The reply is {ok, job, queue}: queue.position, and queue.est_wait_s (the server's estimate),
  // falling back to the job's own est_seconds when the queue object has no estimate.
  function queueLine(job, data) {
    var q = data.queue || {};
    var secs = typeof q.est_wait_s === "number" ? q.est_wait_s : Number(job.est_seconds || 0);
    var minutes = Math.max(1, Math.round(secs / 60));
    if (job.state === "running") {
      return "Analysis is running now, about " + minutes + " min.";
    }
    return (q.position ? "Analysis queued at position " + q.position : "Analysis queued") +
      ", about " + minutes + " min.";
  }

  function whyOf(reach) {
    var why = reach ? reach.why : "";
    if (Array.isArray(why)) { why = why.join("; "); }
    return why ? String(why) : "out of reach";
  }

  function analyzeAction(form) {
    var buttons = form.querySelectorAll("button");
    function setBusy(off) {
      for (var i = 0; i < buttons.length; i += 1) { buttons[i].disabled = off; }
    }
    setBusy(true);
    fetch(cleanUrl(form.getAttribute("action")), {
      method: "POST",
      body: new URLSearchParams(new FormData(form)),
      headers: {"Accept": "application/json"},
      credentials: "same-origin"
    }).then(function (res) {
      return res.json().catch(function () { return {}; }).then(function (data) {
        if (res.status === 409) {
          say("Held back: " + whyOf(data.reach) + ". Run anyway queues it regardless.", null);
          return;
        }
        if (!res.ok || data.ok === false) { throw new Error(data.error || ("HTTP " + res.status)); }
        say(queueLine(data.job || {}, data), null);
      });
    }).catch(function (err) {
      say("Could not queue the analysis: " + err.message, null);
    }).then(function () { setBusy(false); });
  }

  document.addEventListener("submit", function (ev) {
    var form = ev.target;
    if (form.hasAttribute && form.hasAttribute("data-quick-action")) {
      ev.preventDefault();
      analyzeAction(form);
      return;
    }
    if (form.hasAttribute && form.hasAttribute("data-quick")) {
      ev.preventDefault();
      quickAction(form);
      return;
    }
    if (form.hasAttribute && form.hasAttribute("data-scan-form")) { ev.preventDefault(); startScan(form); }
  });

  // -- scan ----------------------------------------------------------------
  function scanLine(text) {
    var box = document.querySelector("[data-scan-progress]");
    if (box) { box.textContent = text || ""; }
  }

  function startScan(form) {
    var body = new URLSearchParams(new FormData(form));
    body.set("back", "json");
    scanLine("starting...");
    fetch(cleanUrl("/api/scan"), {method: "POST", body: body,
                        headers: {"Accept": "application/json"},
                        credentials: "same-origin"})
      .then(function (res) {
        return res.json().catch(function () { return {}; }).then(function (data) {
          if (!res.ok) { throw new Error(data.error || ("HTTP " + res.status)); }
          return data;
        });
      })
      .then(function (data) { pollScan(); })
      .catch(function (err) { scanLine("Scan did not start: " + err.message); });
  }

  function pollScan() {
    fetch(cleanUrl("/api/scan"), {headers: {"Accept": "application/json"}, cache: "no-store",
                        credentials: "same-origin"})
      .then(function (res) { return res.json(); })
      .then(function (data) {
        var job = data.job || {};
        if (job.state === "running" || job.state === "queued") {
          scanLine((job.state === "queued" ? "queued " : "scanning ") + (job.elapsed_s || 0) + "s");
          setTimeout(pollScan, 4000);
          return;
        }
        if (job.state === "done") {
          scanLine("Scan finished. Reloading...");
          setTimeout(function () { window.location.reload(); }, 900);
          return;
        }
        scanLine("Scan failed: " + (job.error || "unknown error"));
      })
      .catch(function () { setTimeout(pollScan, 8000); });
  }

  // -- sidebar / theme -----------------------------------------------------
  var sideBtn = document.querySelector("[data-side-toggle]");
  if (sideBtn) {
    sideBtn.addEventListener("click", function () {
      var side = document.getElementById("sidebar");
      if (!side) { return; }
      side.classList.toggle("open");
      if (scrim) { scrim.classList.toggle("on", side.classList.contains("open")); }
    });
  }
  if (scrim) {
    scrim.addEventListener("click", function () {
      preview.classList.remove("open");
      var side = document.getElementById("sidebar");
      if (side) { side.classList.remove("open"); }
      scrim.classList.remove("on");
    });
  }

  var themeBtn = document.querySelector("[data-theme-toggle]");
  function themeLabel() {
    var dark = document.documentElement.getAttribute("data-theme") === "dark" ||
      (!document.documentElement.getAttribute("data-theme") &&
        window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches);
    var label = themeBtn ? themeBtn.querySelector("[data-theme-label]") : null;
    if (label) { label.textContent = dark ? "Light theme" : "Dark theme"; }
  }
  if (themeBtn) {
    themeBtn.addEventListener("click", function () {
      var dark = document.documentElement.getAttribute("data-theme") === "dark";
      var next = dark ? "light" : "dark";
      document.documentElement.setAttribute("data-theme", next);
      try { localStorage.setItem("jb-theme", next); } catch (e) { /* private mode */ }
      themeLabel();
    });
    themeLabel();
  }

  // -- AI queue panel ------------------------------------------------------
  // Rebuilt from GET /api/jobs. Only createElement and textContent touch the DOM here.
  var QUEUE_ACTIVE_MS = 3000, QUEUE_IDLE_MS = 5000;
  var qpSection = document.querySelector("[data-queue-panel]");
  var qpRunning = document.querySelector("[data-qp-running]");
  var qpList = document.querySelector("[data-qp-list]");
  var prevActive = null;  // id -> {uid, kind}; null until the first reply seeds it
  var qpEta = null;       // {eta, at}: the running job's estimate and when it arrived

  function clockText(secs) {
    secs = Math.max(0, Math.floor(secs));
    var m = Math.floor(secs / 60), s = secs % 60;
    return (m < 10 ? "0" : "") + m + ":" + (s < 10 ? "0" : "") + s;
  }
  function minutesText(secs) {
    if (secs <= 0) { return "soon"; }
    return "about " + Math.max(1, Math.ceil(secs / 60)) + " min";
  }
  function makeEl(tag, cls, text) {
    var el = document.createElement(tag);
    if (cls) { el.className = cls; }
    if (text !== undefined && text !== null) { el.textContent = String(text); }
    return el;
  }
  function postingLabel(job) {
    var co = job.company || "", title = job.title || "";
    if (co && title) { return co + " \u2014 " + title; }
    return co || title || job.uid || "";
  }
  function postingLink(job) {
    var a = makeEl("a", "", postingLabel(job));
    a.href = "/posting/" + encodeURIComponent(job.uid || "");
    return a;
  }
  function kindLabel(job) { return job.kind_label || job.kind || ""; }

  function renderQueue(data) {
    var running = data.running || null;
    var waiting = (data.jobs || []).filter(function (j) { return j.state === "queued"; })
      .sort(function (a, b) { return (a.position || 0) - (b.position || 0); });
    var count = (data.queued || 0) + (running ? 1 : 0);
    if (qpSection) { qpSection.hidden = count === 0; }
    // The empty line is server-rendered once; it must hide the moment anything is active,
    // or the panel reads "building the packet" next to "queue empty".
    var emptyLine = qpSection ? qpSection.querySelector(".qp-empty") : null;
    if (emptyLine) { emptyLine.hidden = count > 0; }
    if (qpRunning) {
      qpRunning.hidden = !running;
      if (running) {
        while (qpRunning.firstChild) { qpRunning.removeChild(qpRunning.firstChild); }
        qpRunning.appendChild(postingLink(running));
        qpRunning.appendChild(makeEl("span", "qp-kind", kindLabel(running)));
        qpRunning.appendChild(makeEl("span", "qp-stage", running.stage_label || ""));
        var clock = makeEl("span", "qp-clock", clockText(running.elapsed_s || 0));
        clock.setAttribute("data-qp-clock", "");
        clock.setAttribute("data-started", running.started || "");
        qpRunning.appendChild(clock);
        var left = makeEl("span", "qp-left", "");
        left.setAttribute("data-qp-left", "");
        qpRunning.appendChild(left);
        qpEta = {eta: running.eta_s || 0, at: Date.now()};
      }
    }
    if (qpList) {
      while (qpList.firstChild) { qpList.removeChild(qpList.firstChild); }
      waiting.forEach(function (j) {
        var li = makeEl("li", "qp-q");
        li.appendChild(makeEl("span", "qp-pos", "#" + (j.position || 0)));
        li.appendChild(postingLink(j));
        li.appendChild(makeEl("span", "qp-kind", kindLabel(j)));
        li.appendChild(makeEl("span", "qp-when", minutesText(j.eta_s || 0)));
        qpList.appendChild(li);
      });
    }
  }

  // The clock and the minutes-left text move every second between polls.
  function tickQueue() {
    var clock = qpRunning ? qpRunning.querySelector("[data-qp-clock]") : null;
    if (clock) {
      var began = Date.parse(clock.getAttribute("data-started") || "");
      if (!isNaN(began)) { clock.textContent = clockText((Date.now() - began) / 1000); }
    }
    var left = qpRunning ? qpRunning.querySelector("[data-qp-left]") : null;
    if (left && qpEta) {
      var remain = qpEta.eta - (Date.now() - qpEta.at) / 1000;
      left.textContent = remain > 0 ? "about " + Math.max(1, Math.ceil(remain / 60)) + " min left"
                                    : "finishing";
    }
  }
  setInterval(tickQueue, 1000);

  // A row gains its marker when its job turns done on this page, not on a reload.
  function markRow(uid, kind) {
    var row = rowByUid(uid);
    if (!row || (kind !== "analysis" && kind !== "letter")) { return; }
    var chip = row.querySelector(".enr-chip");
    if (kind === "letter") {
      if (chip) { return; }
    } else {
      if (chip && !chip.classList.contains("enr-letter")) { return; }
      if (chip) { chip.parentNode.removeChild(chip); }
      row.classList.add("row-enr");
    }
    var analysed = kind === "analysis";
    chip = makeEl("span", "enr-chip" + (analysed ? "" : " enr-letter"),
                  analysed ? "analyzed" : "letter");
    chip.title = analysed ? "AI fit analysis ready" : "Cover letter ready";
    var title = row.querySelector(".r-title");
    if (title) { title.insertBefore(chip, title.querySelector(".lvl, .flag")); }
  }

  function applyQueue(data) {
    var jobs = data.jobs || [];
    var running = data.running || null;
    var now = {};
    if (running) { now[running.id] = {uid: running.uid, kind: running.kind}; }
    jobs.forEach(function (j) {
      if (j.state === "queued") { now[j.id] = {uid: j.uid, kind: j.kind}; }
    });
    if (prevActive) {
      Object.keys(prevActive).forEach(function (id) {
        if (now[id]) { return; }
        var seen = jobs.filter(function (j) { return j.id === id; })[0];
        if (seen && seen.state === "done") { markRow(prevActive[id].uid, prevActive[id].kind); }
      });
    }
    prevActive = now;
    renderQueue(data);
    return Object.keys(now).length ? QUEUE_ACTIVE_MS : QUEUE_IDLE_MS;
  }

  function queueWait(ms) { setTimeout(queuePoll, ms); }
  function queuePoll() {
    if (!window.fetch || !qpSection) { return; }
    fetch(cleanUrl("/api/jobs"), {headers: {"Accept": "application/json"}, credentials: "same-origin",
                        cache: "no-store"})
      .then(function (res) {
        if (!res.ok) { throw new Error("HTTP " + res.status); }
        return res.json();
      })
      .then(function (data) { queueWait(applyQueue(data)); },
            function () { queueWait(QUEUE_IDLE_MS); });
  }
  queuePoll();

  // -- keyboard ------------------------------------------------------------
  function typing(el) {
    if (!el) { return false; }
    var tag = (el.tagName || "").toLowerCase();
    return tag === "input" || tag === "textarea" || tag === "select" || el.isContentEditable;
  }

  document.addEventListener("keydown", function (ev) {
    if (ev.metaKey && ev.key === "k") {
      ev.preventDefault();
      if (SEARCH) { SEARCH.focus(); SEARCH.select(); }
      return;
    }
    if (typing(ev.target) || ev.ctrlKey || ev.metaKey || ev.altKey) { return; }
    var key = ev.key;
    if (key === "/") {
      ev.preventDefault();
      if (SEARCH) { SEARCH.focus(); SEARCH.select(); }
      return;
    }
    if (key === "j" || key === "J" || key === "ArrowDown") { ev.preventDefault(); move(1); return; }
    if (key === "k" || key === "K" || key === "ArrowUp") { ev.preventDefault(); move(-1); return; }
    var row = selectedRow();
    if (!row) { return; }
    var uid = rowUid(row);
    if (key === "s" || key === "S" || key === "x" || key === "X") {
      var want = (key === "s" || key === "S") ? "shortlisted" : "skipped";
      var form = preview.querySelector('form[data-status="' + want + '"]');
      if (form) { ev.preventDefault(); quickAction(form); }
      return;
    }
    if (key === "Enter") {
      ev.preventDefault();
      window.location.href = "/posting/" + encodeURIComponent(uid);
      return;
    }
    if (key === "o" || key === "O") {
      var link = preview.querySelector("[data-original]");
      if (link) { ev.preventDefault(); window.open(link.href, "_blank", "noopener"); }
    }
  });
})();
</script>"""
