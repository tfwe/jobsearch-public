"""Tests for the triage screen and its routes: filters, rendering, preview, scan, redirects.

A live server on port 0 against a seeded temporary store: no real postings, no network.
"""
from __future__ import annotations

import http.client
import json
import re
import threading
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import pytest

from jobsearch import serve, triage
from jobsearch.store import Store

USER = "tester"
PASSWORD = "not-a-real-password"

HIGH = "greenhouse:acme/1"        # score 34, new, plain English
MID = "ashby:beta/2"              # score 22, new, asks for French
LOW = "lever:gamma/3"             # score 5, new
ZERO = "lever:delta/4"            # score 0, new: hidden by default
SHORT = "greenhouse:acme/5"       # shortlisted, score 30


def _seed(path):
    store = Store(str(path))
    rows = [
        {"uid": HIGH, "source": "greenhouse", "company": "Acme", "title": "ML Engineer",
         "location": "Remote, Canada", "url": "https://example.invalid/1",
         "description": "Build Python and Kubernetes pipelines for detection.",
         "posted_at": "2026-11-21", "remote": True},
        {"uid": MID, "source": "ashby", "company": "Beta", "title": "Senior Engineer",
         "location": "Montréal", "url": "https://example.invalid/2",
         "description": "Professional working proficiency in French and English is required. Go and SQL.",
         "posted_at": "2026-11-01", "remote": False},
        {"uid": LOW, "source": "lever", "company": "Gamma", "title": "Analyst",
         "location": "Toronto", "url": "https://example.invalid/3",
         "description": "Spreadsheets and reports.", "posted_at": "2026-11-22",
         "remote": False},
        {"uid": ZERO, "source": "lever", "company": "Delta", "title": "Sales Lead",
         "location": "Austin, TX", "url": "https://example.invalid/4",
         "description": "Quota and pipeline management.", "posted_at": "2026-11-20",
         "remote": False},
        {"uid": SHORT, "source": "greenhouse", "company": "Acme", "title": "Platform Engineer",
         "location": "Remote, Canada", "url": "https://example.invalid/5",
         "description": "Kubernetes and Terraform.", "posted_at": "2026-11-19", "remote": True},
    ]
    store.upsert_postings(rows)
    for uid, score in ((HIGH, 34.0), (MID, 22.0), (LOW, 5.0), (ZERO, 0.0), (SHORT, 30.0)):
        store.set_score(uid, score, "general", ["seeded"])
    store.set_status(SHORT, "shortlisted")
    store.close()


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "jobs.db"
    _seed(path)
    return path


@pytest.fixture
def server(db, tmp_path, monkeypatch):
    monkeypatch.setenv("JOBSEARCH_SERVE_USER", USER)
    monkeypatch.setenv("JOBSEARCH_SERVE_PASSWORD", PASSWORD)
    monkeypatch.delenv(serve.SCAN_ENV, raising=False)
    monkeypatch.setattr(serve, "DASHBOARD_PATH", tmp_path / "dashboard.html")
    monkeypatch.setattr(serve, "PACKETS_ROOT", tmp_path / "packets")
    (tmp_path / "packets").mkdir()
    (tmp_path / "last_scan.json").write_text(json.dumps(
        {"scanned_at": "2026-11-23T20:00:00+00:00", "sources": [{"name": f"b{i}"} for i in range(7)],
         "problems": []}), encoding="utf-8")
    srv = serve.make_server("127.0.0.1", 0, USER, PASSWORD, db_path=str(db))
    thread = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    try:
        yield srv.server_address[1]
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=5)


def _auth():
    import base64
    return {"Authorization": "Basic " + base64.b64encode(
        f"{USER}:{PASSWORD}".encode()).decode()}


def _get(port, path, accept="text/html"):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request("GET", path, headers={**_auth(), "Accept": accept})
        resp = conn.getresponse()
        return resp.status, dict(resp.getheaders()), resp.read().decode("utf-8")
    finally:
        conn.close()


def _post(port, path, fields, accept="text/html"):
    body = urlencode(fields).encode()
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request("POST", path, body=body,
                     headers={**_auth(), "Accept": accept,
                              "Content-Type": "application/x-www-form-urlencoded"})
        resp = conn.getresponse()
        return resp.status, dict(resp.getheaders()), resp.read().decode("utf-8")
    finally:
        conn.close()


# ---------- auto-advance: the next posting in the same list ----------

def test_next_uid_follows_the_visible_order(db):
    store = Store(str(db))
    try:
        default = triage.parse_filters({})
        assert triage.next_uid(store, HIGH, default) == MID     # 34 then 22
        assert triage.next_uid(store, MID, default) == ""       # last above the floor
        wide = triage.parse_filters({"show": ["1"], "score": ["0"]})
        assert triage.next_uid(store, MID, wide) == LOW         # 22 then 5
    finally:
        store.close()


def test_skip_and_apply_advance_to_the_next_posting(server):
    uid = "greenhouse%3Aacme%2F1"
    status, _, html = _get(server, f"/posting/{uid}?step=review")
    assert status == 200
    assert html.count('name="status" value="skipped"') == 1
    assert html.count('name="status" value="applied"') == 1
    footer = html[html.index('class="bottombar"'):]
    footer = footer[:footer.index("</footer>")]
    assert footer.count("data-advance") == 2
    nxt = "/posting/ashby%3Abeta%2F2?step=review"
    assert html.count(f'name="return" value="{nxt}"') == 2


def test_the_last_posting_returns_to_the_list_instead(server):
    uid = "ashby%3Abeta%2F2"          # 22 is last above the default score floor
    status, _, html = _get(server, f"/posting/{uid}?step=review")
    assert status == 200
    skip = html[html.index('name="status" value="skipped"'):]
    skip = skip[:skip.index("</form>")]
    back = skip[skip.index('name="return" value="') + len('name="return" value="'):]
    back = back[:back.index('"')]
    assert back.startswith("/?") and "sel=" in back


def test_a_decided_posting_offers_no_skip_or_apply(server, db):
    store = Store(str(db))
    try:
        store.set_status("greenhouse:acme/5", "applied")   # seeded shortlisted
    finally:
        store.close()
    uid = "greenhouse%3Aacme%2F5"
    status, _, html = _get(server, f"/posting/{uid}?step=review")
    assert status == 200
    assert 'name="status" value="skipped"' not in html
    assert 'name="status" value="applied"' not in html


# ---------- filter parsing (pure) ----------

def test_parse_filters_defaults_to_the_inbox():
    f = triage.parse_filters({})
    assert f["view"] == "inbox" and f["score"] == 15.0 and f["show"] is False


def test_parse_filters_survives_junk():
    f = triage.parse_filters({"view": ["nope"], "score": ["abc"], "days": ["99"],
                              "q": ["x" * 500]})
    assert f["view"] == "inbox" and f["score"] == 15.0 and f["days"] == ""
    assert len(f["q"]) == 120


def test_list_url_round_trips_through_parse_filters():
    f = {"view": "applied", "q": "ml", "score": 20.0, "remote": True,
         "source": "ashby", "nofr": True, "days": "7", "show": True, "sel": HIGH}
    url = triage.list_url(f)
    query = dict(p.split("=", 1) for p in url.split("?", 1)[1].split("&"))
    back = triage.parse_filters({k: [v] for k, v in query.items()})
    assert back["view"] == "applied" and back["score"] == 20.0
    assert back["remote"] and back["nofr"] and back["days"] == "7" and back["show"]


def test_every_status_appears_exactly_once_in_the_nav():
    seen = [s for statuses in triage.VIEW_STATUSES.values() for s in statuses]
    assert sorted(seen) == sorted(("new", "shortlisted", "prepped", "applied", "interview",
                                   "offer", "rejected", "closed", "skipped"))


# ---------- the page ----------

def test_triage_page_renders_rows_highest_score_first(server):
    status, headers, text = _get(server, "/")
    assert status == 200
    assert headers["Cache-Control"] == "no-store"
    assert "Job board" in text
    # Only the inbox at or above 15: HIGH, MID. The zero-score row is held back.
    i_high, i_mid = text.index("ML Engineer"), text.index("Senior Engineer")
    assert i_high < i_mid
    assert "Sales Lead" not in text
    assert "1 ineligible posting hidden" in text


def test_show_reveals_the_rows_it_counts(server):
    _, _, text = _get(server, "/?show=1")
    assert "Sales Lead" not in text  # out of reach, still held back by the default filter
    _, _, text = _get(server, "/?show=1&reach=0")
    assert "Sales Lead" in text and "Analyst" in text
    assert "Including ineligible" in text
    assert 'class="hidden-note"' not in text


def test_french_requirement_is_flagged_on_the_row(server):
    _, _, text = _get(server, "/")
    row = text[text.index(MID):]
    assert "French required" in row
    assert "Seniority: Senior" in row


def test_no_french_filter_hides_the_posting(server):
    _, _, text = _get(server, "/?nofr=1")
    assert "ML Engineer" in text
    assert "Senior Engineer" not in text
    assert ">1</strong> to review" in text


def test_shortlisted_view_and_counts(server):
    _, _, text = _get(server, "/?view=shortlisted")
    assert "Platform Engineer" in text
    assert '<span class="n" data-count-for="inbox">2</span>' in text
    assert '<span class="n" data-count-for="shortlisted">1</span>' in text


def test_search_and_source_filters(server):
    _, _, text = _get(server, "/?q=beta")
    assert "Senior Engineer" in text and "ML Engineer" not in text
    _, _, text = _get(server, "/?source=greenhouse")
    assert "Platform Engineer" in text or "ML Engineer" in text
    assert "Senior Engineer" not in text


def test_unknown_view_falls_back_to_the_inbox(server):
    status, _, text = _get(server, "/?view=<script>alert(1)</script>&score=9999")
    assert status == 200
    assert "<script>alert" not in text
    assert "view: Inbox" in text


def test_page_carries_no_external_links_except_the_posting(server):
    _, _, text = _get(server, "/")
    assert "<link" not in text.lower()
    assert "@import" not in text
    links = [block for block in text.split("href=\"")[1:]]
    external = [block.split("\"")[0] for block in links if block.startswith("http")]
    assert all(url.startswith("https://example.invalid/") for url in external), external


def test_theme_tokens_and_both_schemes_are_in_the_page(server):
    _, _, text = _get(server, "/")
    assert "--bg:#f6f5f1" in text and "--bg:#121413" in text
    assert "prefers-color-scheme:dark" in text
    assert '[data-theme="dark"]' in text
    assert "color-scheme:dark" in text
    assert "localStorage.getItem('jb-theme')" in text


def test_keyboard_hints_and_row_hooks(server):
    _, _, text = _get(server, "/")
    for key in ("J", "S", "X", "Enter", "O", "/"):
        assert f'class="kbd">{key}</span>' in text
    assert 'data-uid="greenhouse:acme/1"' in text
    assert 'class="score hi"' in text      # 34 is the high band
    assert 'class="score mid"' in text     # 22 is the mid band


# ---------- preview ----------

def test_preview_selects_the_first_row_by_default(server):
    _, _, text = _get(server, "/")
    assert "Reading-order score" in text
    assert "Before you spend time" in text
    assert "Open workspace" in text
    assert "A prompt for where to look first, not a verdict on fit." in text


def test_preview_fragment_is_served_alone(server):
    status, headers, text = _get(server, "/preview/" + MID.replace("/", "%2F"))
    assert status == 200 and headers["Content-Type"].startswith("text/html")
    assert "<!DOCTYPE" not in text
    assert "Reading-order score" in text and "French looks required" in text


def test_preview_fragment_404s_for_an_unknown_uid(server):
    assert _get(server, "/preview/nope:none/0")[0] == 404
    assert _get(server, "/preview/")[0] == 404


def test_preview_flags_seniority_as_a_warning(server):
    _, _, text = _get(server, "/preview/" + MID.replace("/", "%2F"))
    assert "Seniority: Senior" in text
    assert 'class="check warn"' in text


def test_preview_bars_sum_to_the_stored_score(server):
    _, _, text = _get(server, "/preview/" + HIGH.replace("/", "%2F"))
    assert '<div class="big">34<small>' in text
    for label in ("Skills overlap", "Role match", "Location gate", "Seniority demanded"):
        assert label in text


# ---------- actions ----------

def test_shortlist_post_returns_to_the_list_with_the_selection(server, db):
    back = "/?view=inbox&sel=" + HIGH
    status, headers, _ = _post(server, "/api/postings/" + HIGH.replace("/", "%2F") + "/status",
                               {"status": "shortlisted", "return": back})
    assert status == 303
    assert headers["Location"] == back
    store = Store(str(db))
    try:
        assert store.get(HIGH)["status"] == "shortlisted"
        kinds = [e["kind"] for e in store.events(HIGH)]
        assert kinds.count("status") == 1
    finally:
        store.close()


def test_offsite_return_target_is_ignored(server):
    status, headers, _ = _post(server, "/api/postings/" + HIGH.replace("/", "%2F") + "/status",
                               {"status": "skipped", "return": "//evil.example/x"})
    assert status == 303
    assert headers["Location"] == "/posting/greenhouse%3Aacme%2F1"


def test_scan_status_reports_disabled(server):
    _, _, body = _get(server, serve.SCAN_PATH, accept="application/json")
    data = json.loads(body)
    assert data["ok"] is True and data["job"]["enabled"] is False
    assert data["job"]["state"] == "idle"


def test_scan_start_is_refused_when_disabled(server):
    status, _, body = _post(server, serve.SCAN_PATH, {"return": "/"}, accept="application/json")
    assert status == 503
    assert "JOBSEARCH_SERVE_SCAN" in json.loads(body)["error"]


def test_scan_button_is_disabled_in_the_sidebar(server):
    _, _, text = _get(server, "/")
    assert "Scan disabled" in text
    assert "JOBSEARCH_SERVE_SCAN=1" in text


def test_scan_post_starts_a_child_process_when_enabled(server, monkeypatch):
    monkeypatch.setenv(serve.SCAN_ENV, "1")
    started = []

    class Fake:
        returncode = 0

        def communicate(self, timeout=None):
            started.append(timeout)
            return "fetched 0 postings", None

        def kill(self):
            started.append("killed")

    import subprocess
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: Fake())
    status, _, body = _post(server, serve.SCAN_PATH, {"return": "/"}, accept="application/json")
    assert status == 200
    assert json.loads(body)["job"]["state"] in ("queued", "running", "done")
    assert started and started[0] == serve.SCAN_TIMEOUT_S


# ---------- the click path can never be a dead end ----------

def test_row_click_falls_back_to_navigation():
    js = triage.TRIAGE_JS
    # The pane swap must not rewrite the document URL: replacing it made Firefox
    # re-request /?sel=... as a fresh navigation without the credential it had.
    assert "replaceState" not in js
    # Any fetch failure navigates to the row's own href, which the server renders.
    assert "window.location.href = row.href" in js
    assert "if (!uid || !window.fetch) { fallback(); return; }" in js


def test_modified_clicks_are_left_to_the_browser():
    js = triage.TRIAGE_JS
    assert "ev.defaultPrevented" in js and "ev.metaKey" in js and "ev.ctrlKey" in js


def test_every_row_carries_a_working_href(server):
    """The href is the no-JS path, so it has to select its own posting."""
    from urllib.parse import quote
    _, _, text = _get(server, "/")
    for uid in (HIGH, MID):
        assert "/?sel=" + quote(uid, safe="") in text


def test_preview_fragment_is_cache_free(server):
    _, headers, _ = _get(server, "/preview/" + HIGH.replace("/", "%2F"))
    assert headers["Cache-Control"] == "no-store"


# ---------- seniority reach ----------

def _row(uid, title, level=None, years=None, score=20.0):
    return {"uid": uid, "source": "lever", "company": "Co", "title": title, "location": "Toronto",
            "score": score, "posted_at": "2026-11-20", "first_seen": "2026-11-20",
            "needs_french": False, "remote": False, "level": level, "level_years": years}


def test_parse_filters_defaults_reach_on_and_reach_zero_turns_it_off():
    assert triage.parse_filters({})["reach"] is True
    assert triage.parse_filters({"reach": ["0"]})["reach"] is False
    assert "reach=0" in triage.list_url(triage.parse_filters({"reach": ["0"]}))
    assert "reach" not in triage.list_url(triage.parse_filters({}))


def test_query_kwargs_reach_only_applies_to_inbox_and_shortlisted_only():
    assert triage._query_kwargs(triage.parse_filters({}))["reach_only"] is True
    off = triage.parse_filters({"reach": ["0"]})
    assert triage._query_kwargs(off)["reach_only"] is False
    for view in ("applied", "skipped", "prepped"):
        kw = triage._query_kwargs(triage.parse_filters({"view": [view]}))
        assert kw["reach_only"] is False, view
    assert triage._query_kwargs(triage.parse_filters({"view": ["shortlisted"]}))["reach_only"] is True


def test_level_badge_covers_out_stretch_and_in_reach():
    assert triage.level_badge(_row("a", "x", level="staff")) == ("staff-level", "out")
    assert triage.level_badge(_row("b", "x", level="mid", years=8)) == ("asks 8+ yrs", "out")
    assert triage.level_badge(_row("c", "x", level="senior")) == ("senior-level", "warn")
    assert triage.level_badge(_row("d", "x", level="mid", years=3)) == ("asks 3 yrs", "warn")
    assert triage.level_badge(_row("e", "x", level="mid", years=2)) == ("", "")
    assert triage.level_badge(_row("f", "x")) == ("", "")


def test_out_of_reach_row_renders_row_out_and_the_lvl_out_chip():
    html = triage._rows_html([_row("u1", "Staff Analyst", level="staff")],
                             triage.parse_filters({}), "")
    assert 'class="row row-out"' in html
    assert '<span class="lvl lvl-out">staff-level</span>' in html
    clean = triage._rows_html([_row("u2", "Analyst", level="mid", years=2)],
                              triage.parse_filters({}), "")
    assert "row-out" not in clean and "lvl-" not in clean


def test_in_reach_only_chip_is_on_by_default_and_links_to_reach_zero():
    on = triage._chips_html(triage.parse_filters({}), [])
    assert "In reach only" in on and 'class="chip on"' in on
    assert 'href="/?reach=0"' in on
    off = triage._chips_html(triage.parse_filters({"reach": ["0"]}), [])
    assert '<a class="chip" href="/">In reach only</a>' in off


def test_hidden_reach_counts_only_what_the_flag_hides(db):
    store = Store(str(db))
    try:
        store._conn.execute("UPDATE postings SET level='staff' WHERE uid=?", (MID,))
        store._conn.execute("UPDATE postings SET level='staff' WHERE uid=?", (SHORT,))
        store._conn.commit()
        assert triage.hidden_reach(store, triage.parse_filters({})) == 1      # MID, inbox
        assert triage.hidden_reach(store, triage.parse_filters({"reach": ["0"]})) == 0
        assert triage.hidden_reach(store, triage.parse_filters({"view": ["shortlisted"]})) == 1
        assert triage.hidden_reach(store, triage.parse_filters({"view": ["applied"]})) == 0
    finally:
        store.close()


def test_preview_renders_the_analyze_fit_form(server):
    _, _, text = _get(server, "/preview/" + HIGH.replace("/", "%2F"))
    assert "Analyze fit" in text and "data-quick-action" in text
    assert 'name="kind" value="analysis"' in text
    assert 'method="post" action="/api/postings/' in text
    assert "Run anyway" not in text  # ML Engineer is in reach


def test_out_of_reach_preview_renders_run_anyway_with_force(server):
    _, _, text = _get(server, "/preview/" + ZERO.replace("/", "%2F"))  # "Sales Lead"
    assert "Out of reach" in text
    assert 'name="force" value="1"' in text and "Run anyway" in text
    assert "Analyze fit" in text


# ---------- search keeps the other filters; the toast reads the reply the server sends ----------

def test_search_form_carries_every_set_filter_as_a_hidden_input(server):
    _, _, text = _get(server, "/?view=shortlisted&score=40&remote=1&source=lever&nofr=1"
                              "&days=7&show=1&reach=0&q=platform")
    start = text.index('<form class="search"')
    form = text[start:text.index("</form>", start)]
    hidden = dict(re.findall(r'<input type="hidden" name="(\w+)" value="([^"]*)">', form))
    assert hidden == {"view": "shortlisted", "score": "40", "remote": "1", "source": "lever",
                      "nofr": "1", "days": "7", "show": "1", "reach": "0"}
    assert 'name="q" type="search" value="platform"' in form  # the visible box carries q


def test_search_form_keeps_reach_on_and_drops_unset_filters(server):
    _, _, text = _get(server, "/")
    start = text.index('<form class="search"')
    form = text[start:text.index("</form>", start)]
    hidden = dict(re.findall(r'<input type="hidden" name="(\w+)" value="([^"]*)">', form))
    assert hidden["reach"] == "1" and hidden["view"] == "inbox" and "remote" not in hidden


def test_triage_script_reads_the_queue_object_and_reach_why():
    js = triage.TRIAGE_JS
    assert "queue.position" in js and "queue.est_wait_s" in js
    assert "est_seconds" in js  # the fallback for the estimate only
    assert "data.position" not in js and "job.position" not in js
    assert "data.reason" not in js and "whyOf(data.reach)" in js


def test_triage_quick_action_captures_the_row_at_submit():
    js = triage.TRIAGE_JS
    capture = js.index("var row = selectedRow();\n    var uid = rowUid(row);")
    submit = js.index("postStatus(form).then(function () {\n      bumpCounts")
    assert capture < submit
    assert "if (row && row.parentNode) { row.parentNode.removeChild(row); }" in js


# ---------- enrichment markers, the Analyzed line, the AI queue panel ----------

def _packets(tmp_path):
    return tmp_path / "packets"


def _artifact(tmp_path, uid, name, data="x"):
    folder = _packets(tmp_path) / triage.packet_dir_name(uid)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_text(data, encoding="utf-8")


def _row_html(text, uid):
    """The full <a class="row"> element for one uid in the list markup."""
    at = text.index(f'data-uid="{uid}"')
    start = text.rfind('<a class="row', 0, at)
    return text[start:text.index("</a>", at) + len("</a>")]


def test_packet_dir_name_is_shared_with_serve():
    from jobsearch import serve as s
    assert s.packet_dir_name("greenhouse:acme/1") == "greenhouse_acme_1"
    assert triage.packet_dir_name("greenhouse:acme/1") == "greenhouse_acme_1"


def test_build_rows_flags_analysis_and_letter_per_row(db, tmp_path):
    _artifact(tmp_path, HIGH, "analysis.md", "Fit: good.\n")
    _artifact(tmp_path, MID, "cover_letter.pdf", "%PDF")
    store = Store(str(db))
    try:
        rows, _ = triage.build_rows(store, triage.parse_filters({"show": ["1"]}), _packets(tmp_path))
    finally:
        store.close()
    by = {r["uid"]: r for r in rows}
    assert by[HIGH]["has_analysis"] is True and by[HIGH]["has_letter"] is False
    assert by[MID]["has_analysis"] is False and by[MID]["has_letter"] is True
    assert by[LOW]["has_analysis"] is False and by[LOW]["has_letter"] is False


def test_analyzed_row_carries_row_enr_and_the_analyzed_chip(server, tmp_path):
    _artifact(tmp_path, HIGH, "analysis.md")
    _, _, text = _get(server, "/")
    row = _row_html(text, HIGH)
    assert "row-enr" in row.split(">")[0]
    assert '<span class="enr-chip" title="AI fit analysis ready">analyzed</span>' in row
    assert "enr-letter" not in row
    other = _row_html(text, MID)
    assert "row-enr" not in other and "enr-chip" not in other


def test_letter_only_row_carries_the_letter_chip_not_row_enr(server, tmp_path):
    _artifact(tmp_path, MID, "cover_letter.pdf", "%PDF")
    _, _, text = _get(server, "/")
    row = _row_html(text, MID)
    assert "row-enr" not in row.split(">")[0]
    assert '<span class="enr-chip enr-letter" title="Cover letter ready">letter</span>' in row


def test_preview_says_analyzed_and_relabels_the_button(server, tmp_path):
    _artifact(tmp_path, HIGH, "analysis.md", "Fit: good.\n")
    _, _, text = _get(server, "/preview/" + HIGH.replace("/", "%2F"))
    assert "<strong>Analyzed</strong>" in text and "analysis written" in text
    assert ">Re-run analysis</button>" in text
    assert "Analyze fit" not in text


def test_preview_without_analysis_keeps_the_analyze_label(server):
    _, _, text = _get(server, "/preview/" + MID.replace("/", "%2F"))
    assert "<strong>Analyzed</strong>" not in text
    assert ">Analyze fit</button>" in text and "Re-run analysis" not in text


def _job(jid, uid, kind, state, *, created, started=None, stage="", est=200.0):
    return {"id": jid, "uid": uid, "kind": kind, "state": state, "created": created,
            "started": started, "finished": None, "error": "", "message": "", "stage": stage,
            "stage_secs": 0.0, "stages": [], "est_seconds": est}


class _StubCopilot:
    def __init__(self, jobs):
        self._jobs = jobs

    def jobs(self):
        return [dict(j) for j in self._jobs]

    def latest(self, uid):
        return None


@pytest.fixture
def live_with(db, tmp_path, monkeypatch):
    """Start a live triage server with the given copilot stub; returns its port."""
    monkeypatch.setenv("JOBSEARCH_SERVE_USER", USER)
    monkeypatch.setenv("JOBSEARCH_SERVE_PASSWORD", PASSWORD)
    monkeypatch.delenv(serve.SCAN_ENV, raising=False)
    monkeypatch.setattr(serve, "PACKETS_ROOT", _packets(tmp_path))
    running = []

    def start(copilot):
        srv = serve.make_server("127.0.0.1", 0, USER, PASSWORD, db_path=str(db), copilot=copilot)
        thread = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05},
                                  daemon=True)
        thread.start()
        running.append((srv, thread))
        return srv.server_address[1]

    yield start
    for srv, thread in running:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=5)


def _ago(seconds):
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat(timespec="seconds")


def _panel(text):
    start = text.index("<section class=\"queue-panel\"")
    return text[start:text.index("</section>", start) + len("</section>")]


def test_queue_panel_is_hidden_when_idle(server):
    _, _, text = _get(server, "/")
    open_tag = text[text.index("<section class=\"queue-panel\""):]
    open_tag = open_tag[:open_tag.index(">")]
    assert "data-queue-panel" in open_tag and "hidden" in open_tag
    assert 'aria-label="AI queue"' in open_tag
    assert "queue empty" in _panel(text)


def test_queue_panel_lists_running_with_stage_clock_and_queued_with_position(live_with):
    jobs = [
        _job("run1", HIGH, "analysis", "running", created=_ago(90), started=_ago(75),
             stage="send", est=200),
        _job("q1", MID, "letter", "queued", created=_ago(10), est=200),
    ]
    _, _, text = _get(live_with(_StubCopilot(jobs)), "/")
    panel = _panel(text)
    assert "hidden" not in panel[:panel.index(">")]
    run = panel[panel.index("data-qp-running"):panel.index("</div>", panel.index("data-qp-running"))]
    assert "Acme — ML Engineer" in run and "sending" in run
    assert 'data-qp-clock data-started="' in run and re.search(r"\d\d:\d\d", run)
    assert "min left" in run and "data-qp-left" in run
    assert f'href="/posting/{quote_uid(HIGH)}"' in run
    queued = panel[panel.index("data-qp-list"):panel.index("</ul>")]
    assert "#2" in queued and "Beta — Senior Engineer" in queued
    assert "Draft cover letter" in queued and f'href="/posting/{quote_uid(MID)}"' in queued


def quote_uid(uid):
    from urllib.parse import quote
    return quote(uid, safe="")


def test_queue_panel_markup_for_a_running_job_without_a_queue():
    panel = {"running": {"uid": HIGH, "company": "Acme", "title": "ML Engineer",
                         "kind": "analysis", "kind_label": "Analyze fit",
                         "stage_label": "sending", "stage": "send",
                         "elapsed_s": 65, "eta_s": 135, "started": _ago(65)},
             "queued": [], "count": 1}
    html = triage.queue_html(panel)
    assert html.startswith('<section class="queue-panel" data-queue-panel aria-label="AI queue">')
    assert '<h2 class="qp-t">Queue</h2>' in html
    assert 'class="qp-item qp-run" data-qp-running>' in html
    assert "01:05" in html and "about 3 min left" in html
    assert '<ul class="qp-list" data-qp-list></ul>' in html


def test_queue_html_is_hidden_with_no_active_job():
    html = triage.queue_html({"running": None, "queued": [], "count": 0})
    assert 'aria-label="AI queue" hidden>' in html
    assert 'class="qp-item qp-run" data-qp-running hidden>' in html


def test_triage_script_polls_at_the_two_cadences_and_builds_the_panel_without_innerhtml():
    js = triage.TRIAGE_JS
    assert "QUEUE_ACTIVE_MS = 3000" in js and "QUEUE_IDLE_MS = 5000" in js
    assert '"/api/jobs"' in js
    start = js.index("// -- AI queue panel")
    block = js[start:js.index("// -- keyboard")]
    assert "createElement" in block and "textContent" in block
    assert "innerHTML" not in block
    assert "analyzed" in block and "row-enr" in block


def test_queue_html_has_no_queue_empty_line_while_a_job_runs():
    running = {"uid": HIGH, "company": "Acme", "title": "ML Engineer", "kind": "prep",
                "kind_label": "Build packet", "stage_label": "building the packet", "stage": "",
                "elapsed_s": 3, "eta_s": 60, "started": _ago(3)}
    assert "queue empty" not in triage.queue_html({"running": running, "queued": [], "count": 1})
    queued = [{"uid": HIGH, "kind": "analysis", "kind_label": "Analyze fit", "position": 1,
               "eta_s": 200, "company": "Acme", "title": "ML Engineer"}]
    assert "queue empty" not in triage.queue_html({"running": None, "queued": queued, "count": 1})


def test_queue_html_keeps_queue_empty_line_when_idle():
    html = triage.queue_html({"running": None, "queued": [], "count": 0})
    assert "queue empty" in html and 'aria-label="AI queue" hidden>' in html


def test_triage_script_sends_every_request_without_url_credentials():
    """A credentialed bookmark makes Chromium refuse fetch, so every call must clean the URL."""
    js = triage.TRIAGE_JS
    assert "function cleanUrl(" in js
    import re as _re
    calls = _re.findall(r"fetch\(([^,]{0,60}),", js)
    assert calls, "no fetch calls found"
    for first in calls:
        assert first.startswith("cleanUrl("), f"unwrapped fetch URL: {first!r}"


def test_triage_script_hides_the_empty_line_while_a_job_is_active():
    """The server renders the empty line once; the poller must hide it as soon as work exists."""
    js = triage.TRIAGE_JS
    assert 'querySelector(".qp-empty")' in js
    assert "emptyLine.hidden = count > 0" in js
