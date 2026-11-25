"""Tests for jobsearch.dashboard.render_dashboard."""
import html
import re
from urllib.parse import quote

import pytest

from jobsearch.dashboard import _posting_card, render_dashboard
from jobsearch.store import Store

ELIGIBLE_LOC = "Toronto, Canada"


def _p(uid, company="Acme", title="AI Security Engineer", location=ELIGIBLE_LOC, **kw):
    base = {"uid": uid, "source": "greenhouse", "company": company, "title": title,
            "location": location, "url": f"https://jobs.example.com/{uid}",
            "description": "Build detection content.", "posted_at": "2026-11-01", "remote": False}
    base.update(kw)
    return base


@pytest.fixture
def seeded(tmp_path):
    """Seeds 12 rows: 7 status new (6 eligible + 1 US-only), 3 in flight, rejected, interview."""
    db = str(tmp_path / "t.db")
    s = Store(db)
    scores = [30.0, 12.0, 27.5, 18.0, 40.0, 5.0]
    s.upsert_postings([_p(f"g:new{i}", company=f"NewCo{i}") for i in range(len(scores))])
    for i, sc in enumerate(scores):
        s.set_score(f"g:new{i}", sc, "ai-security", ["title: ai security (+16)"])
    # US-only: ineligible under the default gate, high score, status new -> must not be a top match
    s.upsert_postings([_p("g:us", company="USCo", location="New York, NY")])
    s.set_score("g:us", 99.0, "ai-security", [])
    s.upsert_postings([_p("g:sh", company="Shortco"), _p("g:pr", company="Prepco"),
                       _p("g:ap", company="Applyco")])
    s.set_score("g:sh", 22.0, "sec-content", [])
    s.set_score("g:pr", 16.0, "sec-content", [])
    s.set_score("g:ap", 33.0, "sec-content", [])
    s.set_status("g:sh", "shortlisted", "good team")
    s.set_status("g:pr", "prepped")
    s.set_status("g:ap", "applied")
    s.upsert_postings([_p("g:rej", company="Rejectco"), _p("g:int", company="Interviewco")])
    s.set_status("g:rej", "rejected")
    s.set_status("g:int", "interview")
    s.close()
    return db


def _render(db, tmp_path, **kw):
    out = tmp_path / "dash.html"
    render_dashboard(db, str(out), **kw)
    return out.read_text(encoding="utf-8")


def _section(page, heading):
    start = page.index(f"<h2>{heading}")
    ends = [x for x in (page.find("<h2>", start + 1), page.find("<footer>", start + 1)) if x != -1]
    return page[start:min(ends)] if ends else page[start:]


def test_returns_out_path_and_writes_file(seeded, tmp_path):
    out = tmp_path / "x.html"
    assert render_dashboard(seeded, str(out)) == str(out)
    assert out.read_text(encoding="utf-8").startswith("<!DOCTYPE html>")


def test_status_chips_show_counts(seeded, tmp_path):
    page = _render(seeded, tmp_path)
    expected = {"new": 7, "shortlisted": 1, "prepped": 1, "applied": 1, "interview": 1,
                "offer": 0, "rejected": 1, "closed": 0, "skipped": 0}
    for status, n in expected.items():
        assert re.search(rf'<span class="chip">{status}<b>{n}</b></span>', page), status
    assert "stored: 12" in page
    assert "passed the gate: 11" in page  # all Toronto rows (6 new + 3 in flight + rejected + interview); USCo is not
    assert "eligible:" not in page


def test_header_scored_matches_equals_rows_at_or_above_15(seeded, tmp_path):
    page = _render(seeded, tmp_path)
    s = Store(seeded)
    try:
        rows = s.list(limit=10 ** 9)
    finally:
        s.close()
    expected = sum(1 for r in rows if r.get("score") is not None and r["score"] >= 15)
    assert expected == 8  # 30, 27.5, 18, 40 (new) + 22, 16, 33 (in flight) + 99 (USCo, ungated)
    m = re.search(r"scored matches \(score ≥ 15\): (\d+)", page)
    assert m, "scored-matches label missing from header"
    assert int(m.group(1)) == expected


def test_top_matches_only_new_eligible_sorted_and_capped(seeded, tmp_path):
    page = _render(seeded, tmp_path)
    top = _section(page, "Top matches")
    assert "USCo" not in top  # ineligible US-only row, despite score 99
    assert "Shortco" not in top and "Applyco" not in top  # not status new
    names = re.findall(r'class="company">([^<]+)<', top)
    # scores: NewCo0=30, 1=12, 2=27.5, 3=18, 4=40, 5=5
    assert names == ["NewCo4", "NewCo0", "NewCo2", "NewCo3", "NewCo1", "NewCo5"]

    capped = _render(seeded, tmp_path, top_n=2)
    names = re.findall(r'class="company">([^<]+)<', _section(capped, "Top matches"))
    assert names == ["NewCo4", "NewCo0"]


def test_in_flight_holds_only_active_statuses_with_status_text(seeded, tmp_path):
    page = _render(seeded, tmp_path)
    flight = _section(page, "In flight")
    for co in ("Shortco", "Prepco", "Applyco"):
        assert co in flight
    assert "Rejectco" not in flight and "Interviewco" not in flight
    assert ">shortlisted<" in flight and ">prepped<" in flight and ">applied<" in flight
    # ordered by status stage, then score desc within stage is not required; status text is
    assert flight.count('class="card"') == 3


def test_last_scan_problems_render(seeded, tmp_path):
    scan = {"scanned_at": "2026-11-23T14:00:00+00:00", "fetched": 8834, "new": 12,
            "updated": 8822, "eligible": 348,
            "sources": [{"name": "Wiz", "ats": "ashby", "ok": False, "detail": "0 postings"}],
            "problems": ["ashby:wiz: 0 postings (dead board?)"],
            "dead_scans": {"ashby:wiz": 3}}
    page = _render(seeded, tmp_path, last_scan=scan)
    health = _section(page, "Source health")
    assert "fetched 8834, new 12, updated 8822" in health
    assert "ashby:wiz: 0 postings (dead board?)" in health
    assert "dead for 3 scans" in health
    assert "2026-11-23T14:00:00+00:00" in page


def test_none_last_scan_renders_without_crashing(seeded, tmp_path):
    page = _render(seeded, tmp_path, last_scan=None)
    assert "No scan recorded yet" in _section(page, "Source health")
    assert "last scan: never" in page


def test_no_external_urls_anywhere_on_the_page(seeded, tmp_path):
    page = _render(seeded, tmp_path, last_scan={"problems": ["x"], "fetched": 1})
    assert "<link" not in page.lower()
    assert "@import" not in page
    assert "http://" not in page
    assert "https://" not in page  # card titles now link to /posting/UID, not the employer
    assert 'href="/posting/g%3Anew4"' in page


def test_company_html_is_escaped(tmp_path):
    db = str(tmp_path / "e.db")
    s = Store(db)
    s.upsert_postings([_p("g:x", company='<img src=x onerror=alert(1))> & "Co"')])
    s.set_score("g:x", 30.0, "general", [])
    s.close()
    page = _render(db, tmp_path)
    assert "&lt;img src=x onerror=alert(1))&gt; &amp; &quot;Co&quot;" in page
    assert "<img" not in page


def test_non_http_url_is_not_linked(tmp_path):
    db = str(tmp_path / "u.db")
    s = Store(db)
    s.upsert_postings([_p("g:j", company="Jsco", url="javascript:alert(1)")])
    s.set_score("g:j", 30.0, "general", [])
    s.close()
    page = _render(db, tmp_path)
    assert "javascript:" not in page
    assert 'href="javascript' not in page


def test_viewport_and_no_fixed_wide_widths(seeded, tmp_path):
    page = _render(seeded, tmp_path)
    assert 'name="viewport" content="width=device-width, initial-scale=1"' in page
    assert "minmax(320px" in page  # only applies at >=700px, single column below
    assert "grid-template-columns:minmax(0,1fr)" in page


def test_top_matches_collapse_bilingual_hopper_reposts(tmp_path):
    db = str(tmp_path / "h.db")
    s = Store(db)
    hop_en = "Forward Deployed AI Engineer, Travel Intelligence"
    hop_bi = ("Ingénieur(e) IA en déploiement avancé, Travel Intelligence// " + hop_en)
    hop2_en = "Forward Deployed Engineer, AI Trust and Reliability"
    hop2_bi = "Ingénieur(e) IA en déploiement avancé, AI Trust and Reliability// " + hop2_en
    rows = [("h:en", hop_en, 40.5), ("h:bi", hop_bi, 40.5),
            ("h2:en", hop2_en, 35.0), ("h2:bi", hop2_bi, 34.0),
            ("o:1", "Platform Engineer", 20.0)]
    s.upsert_postings([_p(uid, company="Hopper", title=t) for uid, t, _sc in rows])
    for uid, _t, sc in rows:
        s.set_score(uid, sc, "ai-platform", [])
    s.close()

    page = _render(db, tmp_path)
    top = _section(page, "Top matches")
    titles = re.findall(r'class="title">(?:<a [^>]*>)?([^<]+)<', top)
    assert titles == [hop_en, hop2_en, "Platform Engineer"]
    assert hop_bi not in top and hop2_bi not in top

    # both rows remain stored and addressable by uid
    s = Store(db)
    try:
        assert s.get("h:bi") is not None and s.get("h2:bi") is not None
    finally:
        s.close()


def test_repost_does_not_take_a_top_slot_from_a_distinct_role(tmp_path):
    db = str(tmp_path / "cap.db")
    s = Store(db)
    s.upsert_postings([
        _p("c:en", company="Hopper", title="Forward Deployed AI Engineer"),
        _p("c:bi", company="Hopper", title="Ingénieur(e) IA// Forward Deployed AI Engineer"),
        _p("c:other", company="Hopper", title="Platform Engineer"),
    ])
    s.set_score("c:en", 40.0, "ai-platform", [])
    s.set_score("c:bi", 39.0, "ai-platform", [])
    s.set_score("c:other", 10.0, "ai-platform", [])
    s.close()
    page = _render(db, tmp_path, top_n=2)
    titles = re.findall(r'class="title">(?:<a [^>]*>)?([^<]+)<', _section(page, "Top matches"))
    assert titles == ["Forward Deployed AI Engineer", "Platform Engineer"]


def _card(page, uid):
    start = page.index(f'data-uid="{uid}"')
    ends = [x for x in (page.find('class="card"', start + 1), page.find("<h2>", start),
                        page.find("<footer>", start)) if x != -1]
    return page[start:min(ends)]


def _make_packet(tmp_path, slug, *names):
    pdir = tmp_path / "packets" / slug
    pdir.mkdir(parents=True, exist_ok=True)
    for name in names:
        (pdir / name).write_bytes(b"%PDF-1.4 stub")
    return pdir


def test_card_with_resume_and_cover_letter_links_both(seeded, tmp_path):
    _make_packet(tmp_path, "g_new0", "resume.pdf", "cover_letter.pdf", "posting.md")
    page = _render(seeded, tmp_path)
    card = _card(page, "g:new0")
    assert 'href="/packets/g_new0/resume.pdf"' in card
    assert 'href="/packets/g_new0/cover_letter.pdf"' in card
    assert 'href="/packets/g_new0/posting.md"' in card
    assert "packet:" in card
    assert "no letter yet" not in card


def test_card_with_resume_only_shows_letter_placeholder(seeded, tmp_path):
    _make_packet(tmp_path, "g_new1", "resume.pdf")
    page = _render(seeded, tmp_path)
    card = _card(page, "g:new1")
    assert 'href="/packets/g_new1/resume.pdf"' in card
    assert "cover_letter.pdf" not in card
    assert "no letter yet" in card
    assert "posting.md" not in card  # only linked when the file exists


def test_card_without_packet_dir_has_no_packet_link(seeded, tmp_path):
    _make_packet(tmp_path, "g_new0", "resume.pdf")  # a different posting's packet
    page = _render(seeded, tmp_path)
    card = _card(page, "g:new2")
    assert "/packets/" not in card
    assert "packet:" not in card
    assert "no letter yet" not in card
    assert "/packets/g_new2" not in page


def test_packet_link_appears_on_in_flight_cards_too(seeded, tmp_path):
    _make_packet(tmp_path, "g_sh", "resume.pdf", "cover_letter.pdf")
    page = _render(seeded, tmp_path)
    card = _card(page, "g:sh")
    assert 'href="/packets/g_sh/cover_letter.pdf"' in card


def test_slug_replaces_slash_and_colon(tmp_path):
    db = str(tmp_path / "s.db")
    s = Store(db)
    s.upsert_postings([_p("ash:job/42", company="Slashco")])
    s.set_score("ash:job/42", 30.0, "general", [])
    s.close()
    _make_packet(tmp_path, "ash_job_42", "resume.pdf", "cover_letter.pdf")
    page = _render(db, tmp_path)
    assert 'href="/packets/ash_job_42/resume.pdf"' in _card(page, "ash:job/42")


def test_dot_uid_never_links_outside_packets(tmp_path):
    db = str(tmp_path / "d.db")
    s = Store(db)
    s.upsert_postings([_p("..", company="Dotco")])
    s.set_score("..", 30.0, "general", [])
    s.close()
    (tmp_path / "packets").mkdir()
    page = _render(db, tmp_path)
    assert "/packets/" not in page


def test_render_without_packets_dir_still_works_on_tmp_path_db(tmp_path):
    db = str(tmp_path / "r.db")
    s = Store(db)
    s.upsert_postings([_p("g:r1", company="Regressco")])
    s.set_score("g:r1", 30.0, "general", [])
    s.close()
    out = tmp_path / "r.html"
    assert render_dashboard(db, str(out)) == str(out)  # old positional call, no packets_dir
    page = out.read_text(encoding="utf-8")
    assert "Regressco" in page
    assert "/packets/" not in page  # no packets dir beside the db -> no links


def test_render_default_packets_dir_is_packets_beside_db(tmp_path):
    db_dir = tmp_path / "home"
    db_dir.mkdir()
    db = str(db_dir / "jobs.db")
    s = Store(db)
    s.upsert_postings([_p("g:r2", company="Besideco")])
    s.set_score("g:r2", 30.0, "general", [])
    s.close()
    _make_packet(db_dir, "g_r2", "resume.pdf", "cover_letter.pdf")
    out = tmp_path / "r2.html"
    render_dashboard(db, str(out))
    assert 'href="/packets/g_r2/resume.pdf"' in out.read_text(encoding="utf-8")


def test_explicit_packets_dir_overrides_default(seeded, tmp_path):
    other = tmp_path / "elsewhere"
    _make_packet(tmp_path, "g_new0", "resume.pdf", "cover_letter.pdf")  # beside db: ignored
    (other / "g_new0").mkdir(parents=True)
    (other / "g_new0" / "resume.pdf").write_bytes(b"x")
    page = _render(seeded, tmp_path, packets_dir=str(other))
    card = _card(page, "g:new0")
    assert 'href="/packets/g_new0/resume.pdf"' in card
    assert "cover_letter.pdf" not in card
    assert "no letter yet" in card


def _options(card):
    m = re.search(r'<select name="status"[^>]*>(.*?)</select>', card)
    assert m, "no status select in card"
    return re.findall(r'<option value="([^"]*)">', m.group(1))


def test_status_new_card_offers_exactly_shortlisted_and_skipped(seeded, tmp_path):
    card = _card(_render(seeded, tmp_path), "g:new0")
    assert card.count("<form ") == 1
    assert _options(card) == ["shortlisted", "skipped"]


def test_prepped_card_offers_prepped_allowed_set(seeded, tmp_path):
    card = _card(_render(seeded, tmp_path), "g:pr")
    assert _options(card) == ["applied", "skipped"]


def test_applied_card_offers_interview_rejected_closed(seeded, tmp_path):
    card = _card(_render(seeded, tmp_path), "g:ap")
    assert _options(card) == ["interview", "rejected", "closed"]


def test_shortlisted_card_offers_its_allowed_set(seeded, tmp_path):
    card = _card(_render(seeded, tmp_path), "g:sh")
    assert _options(card) == ["prepped", "applied", "skipped"]


def test_interview_card_offers_offer_rejected_closed(tmp_path):
    # interview is never listed on the dashboard, so render its card directly
    html_card = _posting_card({"uid": "g:int", "status": "interview", "score": 30.0,
                               "company": "Interviewco", "title": "T", "location": "Toronto"}, "")
    assert _options(html_card) == ["offer", "rejected", "closed"]


def test_status_select_opens_on_disabled_placeholder_then_transitions(seeded, tmp_path):
    card = _card(_render(seeded, tmp_path), "g:sh")
    m = re.search(r'<select name="status"([^>]*)>(.*?)</select>', card)
    assert m, "no status select in card"
    attrs, body = m.group(1), m.group(2)
    assert " required" in attrs
    first = re.match(r'<option value="" selected disabled>change status\.\.\.</option>', body)
    assert first, "placeholder must be the first option, selected and disabled"
    assert re.findall(r'<option value="([^"]*)">', body) == ["prepped", "applied", "skipped"]


def test_form_markup_is_post_with_note_input_and_submit(seeded, tmp_path):
    card = _card(_render(seeded, tmp_path), "g:new0")
    assert '<form class="sform" method="post" action="/api/postings/g%3Anew0/status">' in card
    assert '<select name="status"' in card
    assert '<input name="note" placeholder="note (optional)">' in card
    assert '<button type="submit">' in card
    assert "<script" not in card.lower()


def test_terminal_cards_have_no_form_and_no_api_link(tmp_path):
    # Terminal statuses are never listed on the dashboard; render their cards directly.
    db = str(tmp_path / "term.db")
    s = Store(db)
    s.upsert_postings([_p("t:live", company="Live")])
    s.set_score("t:live", 30.0, "general", [])
    s.close()
    page = _render(db, tmp_path)
    assert page.count("<form ") == 1  # only the status-new card
    for status in ("offer", "rejected", "closed", "skipped"):
        row = {"uid": f"t:{status}", "status": status, "score": 30.0,
               "company": f"Co-{status}", "title": "T", "location": "Toronto",
               "url": f"https://jobs.example.com/{status}"}
        card = _posting_card(row, "")
        assert "<form" not in card, status
        assert "/api/postings/" not in card, status
        assert 'class="card"' in card


def test_every_form_action_contains_escaped_uid(tmp_path):
    db = str(tmp_path / "esc.db")
    s = Store(db)
    tricky = 'g:a&b"c<d>'
    rows = [("g:plain", "new"), (tricky, "new"), ("ash:job/42", "shortlisted"),
            ("g:app", "applied")]
    for uid, st in rows:
        s.upsert_postings([_p(uid, company=f"Co {uid}")])
        s.set_score(uid, 30.0, "general", [])
        s.set_status(uid, st)
    s.close()
    page = _render(db, tmp_path)
    cards = re.findall(r'data-uid="([^"]*)"', page)
    assert len(cards) == len(rows)  # offer rows are not listed, so no card for them
    forms = 0
    for raw in cards:
        uid = html.unescape(raw)
        card = _card(page, raw)
        assert "<form " in card, uid  # every rendered row here is new/shortlisted/applied
        forms += 1
        enc = quote(uid, safe="")
        assert f'action="/api/postings/{html.escape(enc, quote=True)}/status"' in card
    assert forms == 4
    assert html.escape(tricky, quote=True) == "g:a&amp;b&quot;c&lt;d&gt;"
    assert 'action="/api/postings/g%3Aa%26b%22c%3Cd%3E/status"' in page
    assert "<d>" not in page  # raw angle brackets from the uid never reach the markup


def test_form_action_percent_encodes_slash_and_space_as_one_segment(tmp_path):
    uid = "ash:job/42 final"
    db = str(tmp_path / "slash.db")
    s = Store(db)
    s.upsert_postings([_p(uid, company="Co")])
    s.set_score(uid, 30.0, "general", [])
    s.set_status(uid, "shortlisted")
    s.close()
    page = _render(db, tmp_path)
    card = _card(page, html.escape(uid, quote=True))
    m = re.search(r'<form class="sform" method="post" action="([^"]*)">', card)
    assert m, card
    action = html.unescape(m.group(1))
    assert action == "/api/postings/ash%3Ajob%2F42%20final/status"
    segment = action[len("/api/postings/"):-len("/status")]
    assert "/" not in segment and " " not in segment
    assert segment == quote(uid, safe="")
    # the server splits on "/" then unquotes the first segment: must yield the original uid
    from urllib.parse import unquote, urlsplit
    parts = urlsplit(action).path[len("/api/postings/"):].split("/")
    assert len(parts) == 2 and parts[1] == "status"
    assert unquote(parts[0]) == uid


def test_footer_states_the_no_submit_guarantee(seeded, tmp_path):
    page = _render(seeded, tmp_path)
    footer = page[page.index("<footer>"):]
    assert "This page never submits an application, emails or messages anyone" in footer
    assert "you click Submit yourself in the employer&#x27;s own portal." in footer


def test_card_title_is_anchor_to_posting_page_with_encoded_uid(seeded, tmp_path):
    s = Store(seeded)
    s.upsert_postings([_p("ash:job/42", company="Slashco")])
    s.set_score("ash:job/42", 30.0, "general", [])
    s.close()
    page = _render(seeded, tmp_path)
    raws = re.findall(r'data-uid="([^"]*)"', page)
    assert "ash:job/42" in [html.unescape(r) for r in raws]
    assert len(raws) >= 8  # top matches + in-flight cards
    for raw in raws:
        uid = html.unescape(raw)
        card = _card(page, raw)
        m = re.search(r'<div class="title"><a href="([^"]*)">([^<]+)</a></div>', card)
        assert m, uid
        href = m.group(1)
        assert href == "/posting/" + quote(uid, safe=""), uid
        assert ":" not in href[len("/posting/"):] and "/" not in href[len("/posting/"):], uid
        assert 'target="_blank"' not in card, uid

    assert 'href="/posting/ash%3Ajob%2F42"' in _card(page, "ash:job/42")

    # terminal-status cards (never listed on the dashboard) get the same title anchor
    for status in ("offer", "rejected", "closed", "skipped"):
        uid = f"t:{status}/x:1"
        row = {"uid": uid, "status": status, "score": 30.0, "company": "Co",
               "title": "T", "location": "Toronto"}
        card = _posting_card(row, "")
        assert f'<div class="title"><a href="/posting/{quote(uid, safe="")}">T</a></div>' in card, status
        assert "<form" not in card, status


def test_title_anchor_escapes_hostile_uid(tmp_path):
    uid = 'g:a&b"c<d>'
    db = str(tmp_path / "hx.db")
    s = Store(db)
    s.upsert_postings([_p(uid, company="Hostco", title="Hostile <b>Title</b>")])
    s.set_score(uid, 30.0, "general", [])
    s.close()
    page = _render(db, tmp_path)
    card = _card(page, html.escape(uid, quote=True))
    assert 'href="/posting/g%3Aa%26b%22c%3Cd%3E"' in card
    assert "Hostile &lt;b&gt;Title&lt;/b&gt;" in card
    assert "<d>" not in page


def test_no_timeline_link_remains(seeded, tmp_path):
    page = _render(seeded, tmp_path)
    assert "timeline" not in page.lower()
    cards = re.findall(r'class="card"', page)
    assert len(cards) == 9  # 6 top matches + 3 in flight
    assert page.count('href="/posting/') == len(cards)  # one title link per card, nothing else


def test_each_card_carries_data_status(seeded, tmp_path):
    page = _render(seeded, tmp_path)
    got = dict(re.findall(r'<div class="card" data-uid="([^"]*)" data-status="([^"]*)">', page))
    assert got["g:new0"] == "new"
    assert got["g:sh"] == "shortlisted"
    assert got["g:pr"] == "prepped"
    assert got["g:ap"] == "applied"
    assert len(got) == 9
    assert set(got.values()) == {"new", "shortlisted", "prepped", "applied"}
    row = {"uid": "t:r", "status": "rejected", "score": 1.0, "company": "C",
           "title": "T", "location": "L"}
    assert 'data-status="rejected"' in _posting_card(row, "")


def test_page_has_one_inline_script_at_end_of_body_and_no_external_src(seeded, tmp_path):
    page = _render(seeded, tmp_path)
    assert page.count("<script>") == 1
    assert page.count("</script>") == 1
    assert not re.search(r"<script[^>]*\bsrc\s*=", page, re.I)
    assert "<script src" not in page
    assert page.index("<script>") > page.index("<footer>")
    assert page.rindex("</script>") < page.index("</body>")


def test_filter_ui_is_inserted_by_script_not_server_markup(seeded, tmp_path):
    page = _render(seeded, tmp_path)
    markup = re.sub(r"<(script|style)>.*?</\1>", "", page, flags=re.S)
    assert 'type="search"' not in markup
    assert "data-filter" not in markup
    assert 'class="filters"' not in markup
    assert "fcount" not in markup
    assert "matched" not in markup
    script = page[page.index("<script>"):page.index("</script>")]
    assert "getAttribute('data-status')" in script
    assert "' matched'" in script
    assert "closest" in script  # chip click handling
    assert "</script" not in script[len("<script>"):]


def test_js_off_render_has_every_card_and_no_hidden_or_filter_controls(seeded, tmp_path):
    page = _render(seeded, tmp_path)
    markup = re.sub(r"<(script|style)>.*?</\1>", "", page, flags=re.S)
    assert markup.count('class="card"') == 9
    assert not re.search(r"<[^>]*\shidden[\s>]", markup)
    assert "fchip" not in markup and "fsearch" not in markup
    assert "[hidden]{display:none!important}" in page  # script's hide mechanism is styled


def test_script_is_valid_javascript(seeded, tmp_path):
    import shutil
    import subprocess
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    page = _render(seeded, tmp_path)
    js = page[page.index("<script>") + len("<script>"):page.index("</script>")]
    f = tmp_path / "filter.js"
    f.write_text(js, encoding="utf-8")
    proc = subprocess.run([node, "--check", str(f)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


def test_packet_slug_copies_agree():
    """Three copies of the uid->packet-dir mapping must stay byte-identical."""
    from jobsearch.cli import _slug
    from jobsearch.dashboard import _packet_slug
    from jobsearch.letter import slug_for_uid

    uid = "ashby:render/9c7e862a-c9fe-48a8-aaf8-26f8e25c66f5"
    expected = "ashby_render_9c7e862a-c9fe-48a8-aaf8-26f8e25c66f5"
    assert slug_for_uid(uid) == expected
    assert _slug(uid) == expected
    assert _packet_slug(uid) == expected
