"""Tests for the workspace page composition: progress steps, letter checks, exits, safety.

The workspace is assembled in serve.workspace_context() and rendered by
workspace.render_workspace(); these tests drive both without a browser.
"""
from __future__ import annotations

import os
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import pytest

from jobsearch import cli, copilot, serve, ui, workspace
from jobsearch.store import Store

UID = "greenhouse:acme/1"

# Epoch seconds in local time, so the day named in a stale sentence holds in any timezone.
PDF_TIME = datetime(2026, 11, 22, 12, 0).timestamp()
FACTS_TIME = datetime(2026, 11, 24, 9, 30).timestamp()


@pytest.fixture(autouse=True)
def _isolated_sources(tmp_path, monkeypatch):
    """Point the facts file and resume output at paths that do not exist, so no test
    reads the operator's real resume files through the stale check."""
    monkeypatch.setattr(copilot, "DEFAULT_FACTS_PATH", str(tmp_path / "no-facts.yaml"))
    monkeypatch.setattr(cli, "RESUME_OUT", tmp_path / "no-resume-out")


def _built_packet(tmp_path, monkeypatch, pdf_time):
    """A packet dir under a temp root holding a resume.pdf with the given mtime."""
    monkeypatch.setattr(serve, "PACKETS_ROOT", tmp_path / "packets")
    packet = tmp_path / "packets" / serve.packet_dir_name(UID)
    packet.mkdir(parents=True, exist_ok=True)
    pdf = packet / "resume.pdf"
    pdf.write_bytes(b"%PDF-1.4 x")
    os.utime(pdf, (pdf_time, pdf_time))
    return packet


def _facts(tmp_path, monkeypatch, when):
    facts = tmp_path / "facts.yaml"
    facts.write_text("version: 1\n", encoding="utf-8")
    os.utime(facts, (when, when))
    monkeypatch.setattr(copilot, "DEFAULT_FACTS_PATH", str(facts))
    return facts


def test_packet_is_flagged_stale_when_facts_changed_after_the_build(tmp_path, monkeypatch):
    _built_packet(tmp_path, monkeypatch, PDF_TIME)
    _facts(tmp_path, monkeypatch, FACTS_TIME)
    html = workspace.render_workspace(_ctx(tmp_path, monkeypatch, step="packet"))
    assert "data-stale-packet" in html
    assert "This packet is out of date" in html
    assert "facts.yaml changed on 2026-11-24, after this packet was built" in html
    assert "Build again before you send it." in html


def test_packet_is_not_flagged_when_the_pdf_is_newer_than_facts(tmp_path, monkeypatch):
    _built_packet(tmp_path, monkeypatch, FACTS_TIME + 60)
    _facts(tmp_path, monkeypatch, PDF_TIME)
    html = workspace.render_workspace(_ctx(tmp_path, monkeypatch, step="packet"))
    assert "data-stale-packet" not in html
    assert "This packet is out of date" not in html


def test_stale_card_sits_above_the_file_list(tmp_path, monkeypatch):
    _built_packet(tmp_path, monkeypatch, PDF_TIME)
    _facts(tmp_path, monkeypatch, FACTS_TIME)
    html = workspace.render_workspace(_ctx(tmp_path, monkeypatch, step="packet"))
    assert html.index("data-stale-packet") < html.index("data-packet-list")


def test_stale_reason_is_escaped_and_one_paragraph_per_reason(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path, monkeypatch, step="packet")
    ctx["stale"] = ["<b>x</b> changed on 2026-11-24", "second reason"]
    html = workspace.render_workspace(ctx)
    assert "&lt;b&gt;x&lt;/b&gt; changed on 2026-11-24" in html
    assert "<b>x</b>" not in html
    card = html[html.index("data-stale-packet"):html.index("data-packet-list")]
    assert card.count("<p ") == 3          # two reasons and the build-again line


def test_no_stale_key_means_no_card(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path, monkeypatch, step="packet")
    ctx.pop("stale", None)
    assert "data-stale-packet" not in workspace.render_workspace(ctx)
    ctx["stale"] = []
    assert "data-stale-packet" not in workspace.render_workspace(ctx)


def test_workspace_context_carries_the_stale_key(tmp_path, monkeypatch):
    _built_packet(tmp_path, monkeypatch, PDF_TIME)
    _facts(tmp_path, monkeypatch, FACTS_TIME)
    ctx = _ctx(tmp_path, monkeypatch, step="packet")
    assert ctx["stale"] == ["facts.yaml changed on 2026-11-24, after this packet was built"]


def test_variant_resume_newer_than_the_packet_is_named(tmp_path, monkeypatch):
    _built_packet(tmp_path, monkeypatch, PDF_TIME)
    out = tmp_path / "out"
    out.mkdir()
    pdf = out / cli.PDF_NAMES["ai-platform"]
    pdf.write_bytes(b"%PDF-1.4 y")
    os.utime(pdf, (FACTS_TIME, FACTS_TIME))
    packet = {"slug": serve.packet_dir_name(UID), "files": [{"name": "resume.pdf"}]}
    reasons = serve._stale_packet(packet, "ai-platform",
                                  facts_path=tmp_path / "no-facts.yaml", resume_out=out)
    assert reasons == ["the ai-platform resume changed on 2026-11-24, after this packet was built"]


def test_stale_check_ignores_unbuilt_packets_and_unknown_variants(tmp_path, monkeypatch):
    _built_packet(tmp_path, monkeypatch, PDF_TIME)
    _facts(tmp_path, monkeypatch, FACTS_TIME)
    empty = {"slug": serve.packet_dir_name(UID), "files": []}
    assert serve._stale_packet(empty, "ai-platform") == []          # nothing built yet
    built = {"slug": serve.packet_dir_name(UID), "files": [{"name": "resume.pdf"}]}
    assert serve._stale_packet(built, "not-a-variant") == [
        "facts.yaml changed on 2026-11-24, after this packet was built"]


def _posting(**over):
    row = {"uid": UID, "source": "greenhouse", "company": "Acme", "title": "ML Engineer",
           "location": "Remote, Canada", "url": "https://example.invalid/1",
           "status": "new", "score": 34.0, "variant": "ai-platform",
           "description": "Build Kubernetes and Python pipelines.",
           "posted_at": "2026-11-20", "remote": True}
    row.update(over)
    return row


def _ctx(tmp_path, monkeypatch, step="", **over):
    monkeypatch.setattr(serve, "PACKETS_ROOT", tmp_path / "packets")
    (tmp_path / "packets").mkdir(exist_ok=True)
    return serve.workspace_context(_posting(**over), [
        {"ts": "2026-11-23T18:06:50+00:00", "kind": "score", "detail": "scored 34"},
        {"ts": "2026-11-23T18:07:00+00:00", "kind": "status", "detail": "new->shortlisted"},
    ], None, back_url="/?view=inbox", step=step)


def test_steps_are_derived_from_what_exists(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path, monkeypatch)
    assert [s["label"] for s in ctx["steps"]] == ["Review", "Packet", "Letter", "Submit"]
    assert [s["key"] for s in ctx["steps"]] == ["review", "packet", "letter", "submit"]
    assert [s["done"] for s in ctx["steps"]] == [False, False, False, False]
    assert ctx["step"] == "review"          # first incomplete step


def test_header_is_a_title_one_meta_line_and_a_status_pill(tmp_path, monkeypatch):
    """Company, location, score and age appear once, in the single meta line."""
    html = workspace.render_workspace(_ctx(tmp_path, monkeypatch))
    head = html[:html.index("</header>")]
    assert head.count('<div class="tb-title">ML Engineer</div>') == 1
    assert head.count('class="tb-meta"') == 1
    meta = head[head.index('class="tb-meta"'):]
    meta = meta[:meta.index("</div>")]
    assert "Acme" in meta and meta.count("Acme") == 1
    assert "Remote, Canada" in meta and "score 34" in meta
    assert re.search(r"posted \d+ days? ago|posted today|posting date unknown", meta)


def test_status_is_a_pill_whose_menu_holds_the_form(tmp_path, monkeypatch):
    html = workspace.render_workspace(_ctx(tmp_path, monkeypatch))
    assert '<details class="status-menu"' in html
    assert '<summary class="status-pill"' in html
    pill = html[html.index('class="status-pill"'):html.index("</summary>")]
    assert 'id="status-text"' in pill and ">new<" in pill
    form = html[html.index('class="status-form"'):html.index("</form>")]
    assert 'name="status"' in form and 'name="note"' in form
    assert ('<form method="post" action="/api/postings/greenhouse%3Aacme%2F1/status" '
            'class="status-form"') in html
    # no-JS: the POST comes back to the same step
    assert 'name="return" value="/posting/greenhouse%3Aacme%2F1?step=review"' in form


def test_header_links_to_the_original_and_says_so(tmp_path, monkeypatch):
    html = workspace.render_workspace(_ctx(tmp_path, monkeypatch))
    head = html[:html.index("</header>")]
    assert f'<a class="orig" href="https://example.invalid/1"' in head
    assert ">Open original</a>" in head
    # no URL means no link, never a dead one
    dead = workspace.render_workspace(_ctx(tmp_path, monkeypatch, url=""))
    assert 'class="orig"' not in dead


def test_a_step_is_done_only_when_its_artifact_exists(tmp_path, monkeypatch):
    monkeypatch.setattr(serve, "PACKETS_ROOT", tmp_path / "packets")
    packet = tmp_path / "packets" / serve.packet_dir_name(UID)
    packet.mkdir(parents=True)
    (packet / "analysis.md").write_text("VERDICT\nok\n\nFIT\nsome fit\n\nGAPS\nsome gap\n")
    ctx = serve.workspace_context(_posting(), [], None)
    assert [s["done"] for s in ctx["steps"]] == [True, False, False, False]
    assert ctx["step"] == "packet"

    (packet / "resume.pdf").write_bytes(b"x")
    (packet / "cover_letter.pdf").write_bytes(b"x")
    ctx = serve.workspace_context(_posting(), [], None)
    assert [s["done"] for s in ctx["steps"]] == [True, True, True, False]
    assert ctx["step"] == "submit"


def test_all_four_steps_done_lands_on_submit(tmp_path, monkeypatch):
    monkeypatch.setattr(serve, "PACKETS_ROOT", tmp_path / "packets")
    packet = tmp_path / "packets" / serve.packet_dir_name(UID)
    packet.mkdir(parents=True)
    for name in ("analysis.md", "resume.pdf", "cover_letter.pdf", "posting.md"):
        (packet / name).write_text("x")
    ctx = serve.workspace_context(_posting(status="applied"), [], None)
    assert [s["done"] for s in ctx["steps"]] == [True, True, True, True]
    assert ctx["step"] == "submit"


def test_a_requested_step_wins_and_a_bogus_one_falls_back(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path, monkeypatch, step="letter")
    assert ctx["step"] == "letter"
    assert workspace.render_workspace(ctx).count('data-panel="letter"') == 1
    bad = _ctx(tmp_path, monkeypatch, step="bogus")
    assert bad["step"] == "review"


def test_tabs_are_plain_links_and_the_current_one_is_marked(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path, monkeypatch)
    html = workspace.render_workspace(ctx)
    assert '<nav class="tabs"' in html
    assert html.count('<a class="tab') == 4
    assert 'href="?step=review&amp;back=%2F%3Fview%3Dinbox"' in html
    assert html.count('aria-current="step"') == 1
    on = html[html.index('aria-current="step"'):]
    assert "Review" in on[:on.index("</a>")]
    # a tick appears only on a finished step, and only inside the tab bar
    nav = html[html.index('<nav class="tabs"'):html.index("</nav>")]
    assert "&#10003;" not in nav
    ctx["steps"][0]["done"] = True
    nav = workspace.render_workspace(ctx)
    nav = nav[nav.index('<nav class="tabs"'):nav.index("</nav>")]
    assert "&#10003;" in nav


def test_only_the_current_step_renders_its_panel(tmp_path, monkeypatch):
    for only, other in (("packet", "letter"), ("letter", "packet"), ("review", "packet")):
        html = workspace.render_workspace(_ctx(tmp_path, monkeypatch, step=only))
        assert f'data-panel="{only}"' in html
        assert f'data-panel="{other}"' not in html


def test_packet_reports_files_ages_and_the_resume_variant(tmp_path, monkeypatch):
    monkeypatch.setattr(serve, "PACKETS_ROOT", tmp_path / "packets")
    packet = tmp_path / "packets" / serve.packet_dir_name(UID)
    packet.mkdir(parents=True)
    (packet / "resume.pdf").write_bytes(b"x")
    (packet / "posting.md").write_bytes(b"x")
    events = [
        {"ts": "2026-11-23T18:00:00+00:00", "kind": "status",
         "detail": "shortlisted->prepped"},
        {"ts": "2026-11-23T18:00:01+00:00", "kind": "status",
         "detail": "packet staged (ai-platform resume)"},
    ]
    ctx = serve.workspace_context(_posting(status="prepped"), events, None)
    assert ctx["packet"]["resume_variant"] == "ai-platform"
    names = {f["name"] for f in ctx["packet"]["files"]}
    assert names == {"resume.pdf", "posting.md"}
    assert all(f["age"] for f in ctx["packet"]["files"])


def test_letter_checks_come_from_the_validator(tmp_path, monkeypatch):
    body = "word " * 100  # too short on purpose
    ctx = _ctx(tmp_path, monkeypatch)
    ctx["body_text"] = body
    ctx["letter_checks"] = serve._letter_checks(body, ctx["posting"]["description"])
    labels = [c["label"] for c in ctx["letter_checks"]]
    assert labels == [
        "240-320 words", "ASCII only", "No project names", "No tech-list parentheses",
        "Every number traceable", "No French requirement", "Names the company",
        "No resume sentences reused", "No cliches"]
    words = next(c for c in ctx["letter_checks"] if c["label"] == "240-320 words")
    assert words["ok"] is False and words["detail"] == "100 words"


CLEAN_BODY = (
    "Acme's data platform is where I would spend my first year. In my last role I "
    "moved nightly batch jobs onto a Kubernetes cluster and cut run time by a third, "
    "then wrote the runbooks the on-call team still uses. I read the posting twice and "
    "the work you describe, with Python pipelines feeding models in production, is the "
    "work I already do. I would bring a habit of measuring before changing anything, and "
    "I would ask the team early which failures cost them the most sleep. Thank you for "
    "reading this letter, and for the time you spend on applications like mine."
)


def test_letter_check_names_the_company_case_insensitively():
    def row(body, company):
        return next(c for c in serve._letter_checks(body, "", company=company)
                    if c["label"] == "Names the company")
    assert row(CLEAN_BODY, "Acme")["ok"] is True
    assert row(CLEAN_BODY.replace("Acme's", "ACME's"), "acme")["ok"] is True
    assert row(CLEAN_BODY, "Acme Robotics")["ok"] is False
    assert row(CLEAN_BODY, "Acme Robotics")["detail"] == "not mentioned"
    # Punctuation in the company name does not break the match, and a letter may drop "Inc."
    assert row("I admire Ramp's work and want to join Ramp.", "Ramp, Inc.")["ok"] is True
    assert row("I admire Ramp's work and want to join Ramp.", "R-A-M-P")["ok"] is False
    # Whole words only: "meta" is not a mention of "Metadata Labs"
    assert row("Metadata work is my focus.", "Meta")["ok"] is False
    # No company on the posting never makes a letter look wrong
    none = row(CLEAN_BODY, None)
    assert none["ok"] is True and none["detail"] == "no company on the posting"


def test_letter_check_flags_a_sentence_copied_from_the_resume():
    resume = ("Platform engineer with six years of warehouse work. Rebuilt the nightly "
              "billing batch on a Kubernetes cluster, cutting run time by a third.")
    def row(body, resume_text):
        return next(c for c in serve._letter_checks(body, "", company="Acme",
                                                    resume_text=resume_text)
                    if c["label"] == "No resume sentences reused")
    pasted = ("Acme's data platform is where I would spend my first year. Rebuilt the "
              "nightly billing batch on a Kubernetes cluster, cutting run time by a third. "
              "I would bring a habit of measuring before changing anything.")
    flagged = row(pasted, resume)
    assert flagged["ok"] is False
    assert flagged["detail"].startswith("15 words shared:")  # the pasted run, nothing more
    assert "rebuilt the nightly billing batch" in flagged["detail"]  # shown in lower case
    assert row(CLEAN_BODY, resume)["ok"] is True and row(CLEAN_BODY, resume)["detail"] == ""
    # Short shared phrases (under eight words) are not a pasted sentence
    assert row("A Kubernetes cluster and a third of the time, then I", resume)["ok"] is True
    # No resume is a pass with a plain note, not a failure
    absent = serve._letter_checks(pasted, "", company="Acme", resume_text=None)
    note = next(c for c in absent if c["label"] == "No resume sentences reused")
    assert note["ok"] is True and note["detail"] == "no resume to compare"


def test_letter_check_flags_a_cliche_and_passes_a_clean_letter():
    def checks(body):
        return {c["label"]: c for c in serve._letter_checks(body, "", company="Acme")}
    clean = checks(CLEAN_BODY)
    assert clean["No cliches"]["ok"] is True and clean["No cliches"]["detail"] == ""
    bad = checks(CLEAN_BODY + " I am passionate about data and excited to join. "
                 "I would leverage my skills in a fast-paced environment.")
    row = bad["No cliches"]
    assert row["ok"] is False
    assert row["detail"] == "passionate about, leverage"  # first two hits, in CLICHES order
    assert checks("Leveraged the Kubernetes cluster for Acme.")["No cliches"]["ok"] is False
    assert all(serve.CLICHES[i] != serve.CLICHES[j]
               for i in range(len(serve.CLICHES)) for j in range(i))  # no duplicates
    for phrase in ("passionate about", "leverage", "synergy", "team player",
                   "hit the ground running", "excited to", "dynamic environment",
                   "fast-paced environment", "wear many hats", "self-starter"):
        assert phrase in serve.CLICHES


def test_no_letter_means_no_checks(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path, monkeypatch)
    assert ctx["letter_checks"] == []


def test_the_blockers_card_carries_a_french_requirement(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path, monkeypatch,
               description="Professional working proficiency in French and English is required.")
    kinds = [b["kind"] for b in ctx["blockers"]]
    assert kinds == ["language"]
    html = workspace.render_workspace(ctx)
    card = html[html.index('data-blockers'):]
    card = card[:card.index("</section>")]
    assert "Decide before applying" in card and "French is required" in card
    assert "Posting says" in card and "Your facts say" in card


def test_the_blockers_card_is_calm_when_there_is_none(tmp_path, monkeypatch):
    html = workspace.render_workspace(_ctx(tmp_path, monkeypatch))
    assert 'class="card blockers calm"' in html
    assert "Nothing blocking." in html


def test_a_toronto_onsite_posting_is_a_location_blocker(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path, monkeypatch, location="Toronto, Ontario, Canada", remote=False)
    kinds = [b["kind"] for b in ctx["blockers"]]
    assert kinds == ["location"]
    assert ctx["blockers"][0]["posting"] == "Toronto, Ontario, Canada"
    assert "Winnipeg" in ctx["blockers"][0]["facts"]
    html = workspace.render_workspace(ctx)
    assert 'data-blocker="location"' in html and "Location is not Winnipeg" in html


def test_a_remote_or_winnipeg_or_multi_city_posting_is_not_a_location_blocker(
        tmp_path, monkeypatch):
    for kw in ({"location": "Remote, Canada", "remote": True},
               {"location": "Toronto, Ontario", "remote": True},
               {"location": "Winnipeg, MB", "remote": False},
               {"location": "Bellevue, WA; Winnipeg, Manitoba, Canada", "remote": False},
               {"location": "", "remote": False}):
        ctx = _ctx(tmp_path, monkeypatch, **kw)
        assert not [b for b in ctx["blockers"] if b["kind"] == "location"], kw


def test_the_rail_is_blockers_then_step_context_then_activity(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path, monkeypatch, step="letter")
    ctx["body_text"] = "short"
    ctx["letter_checks"] = [{"label": "240-320 words", "ok": False, "detail": "1 words"}]
    rail = workspace.render_workspace(ctx)
    rail = rail[rail.index('class="col col-r"'):]
    assert rail.index("data-blockers") < rail.index("Letter checks") < rail.index("Activity")


def test_the_rail_hides_cards_that_have_nothing_to_say(tmp_path, monkeypatch):
    monkeypatch.setattr(serve, "PACKETS_ROOT", tmp_path / "packets")
    (tmp_path / "packets").mkdir(exist_ok=True)
    ctx = serve.workspace_context(_posting(), [], None, step="letter")
    rail = workspace.render_workspace(ctx)
    rail = rail[rail.index('class="col col-r"'):]
    assert "Letter checks" not in rail and "Activity" not in rail
    assert "no events yet" not in rail
    # the decision card is the one thing that is always there
    assert "Decide before applying" in rail


def test_packet_step_names_the_build_that_produced_the_resume(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path, monkeypatch, step="packet")
    ctx["events"] = [{"ts": "2026-11-23T18:00:00+00:00", "kind": "status",
                      "detail": "packet staged (ai-platform resume)"}]
    rail = workspace.render_workspace(ctx)
    rail = rail[rail.index('class="col col-r"'):]
    assert "Packet source" in rail and "ai-platform" in rail
    assert "2026-11-23 18:00" in rail


def test_activity_shows_six_and_counts_the_rest(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path, monkeypatch, step="review")
    ctx["events"] = [
        {"ts": f"2026-11-23T18:0{i}:00+00:00", "kind": "status", "detail": f"ev{i}"}
        for i in range(9)
    ]
    rail = workspace.render_workspace(ctx)
    rail = rail[rail.index('class="col col-r"'):]
    assert rail.count('class="li"') == 6
    assert "ev8" in rail and "ev3" in rail and "ev2" not in rail
    assert "3 older events" in rail


def test_all_three_blockers_are_in_one_card_on_every_step(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path, monkeypatch, location="Toronto, Ontario, Canada", remote=False,
               description="Fluency in French is required. ".ljust(200, "x"),
               title="Director of Engineering",
               reach={"verdict": "out_of_reach", "why": "director-level title"})
    assert [b["kind"] for b in ctx["blockers"]] == ["language", "location", "seniority"]
    for step in ("review", "packet", "letter", "submit"):
        ctx["step"] = step
        html = workspace.render_workspace(ctx)
        card = html[html.index('data-blockers'):]
        card = card[:card.index("</section>")]
        assert card.count("data-blocker=") == 3, step
    rail = html[html.index('class="col col-r"'):]
    assert rail.index("data-blockers") < rail.index("Activity")


SECTIONED = (
    "About the role\nBuild Python pipelines and Kubernetes tooling.\n\n"
    "Location\nRemote across Canada, with quarterly travel.\n\n"
    "Requirements\nPython, Kubernetes and SQL experience.\n\n"
    "Perks\nBenefits from day one.\n"
)


def test_the_posting_splits_into_sections_with_jump_chips(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path, monkeypatch, description=SECTIONED)
    labels = [s["label"] for s in ctx["posting_sections"]]
    assert labels == ["Role", "Location", "Requirements", "Perks"]
    html = workspace.render_workspace(ctx)
    left = html[html.index('class="col col-l"'):html.index('class="col col-c"')]
    assert left.count('<a class="chip" href="#sec-') == 4
    assert '<section class="post-sec" id="sec-role-1">' in left
    assert '<h3 class="post-h">Requirements</h3>' in left
    # the sections carry the text, and nothing is lost in the split
    assert "pipelines and Kubernetes tooling." in left
    assert "Benefits from day one." in left
    # the heading line is not repeated inside its own section
    assert left.count("About the role") == 1


def test_highlights_survive_the_split(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path, monkeypatch,
               description="Requirements\nPython and Kubernetes are required.\n")
    html = workspace.render_workspace(ctx)
    left = html[html.index('class="col col-l"'):html.index('class="col col-c"')]
    assert "<mark>python</mark>" in left.lower()


def test_a_posting_with_no_headings_stays_one_block_and_shows_no_chips(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path, monkeypatch,
               description="A plain paragraph with no headings at all.\nSecond line.")
    assert len(ctx["posting_sections"]) == 1
    html = workspace.render_workspace(ctx)
    assert 'class="post-nav"' not in html
    assert '<div class="post-text">' in html


def test_a_sentence_that_mentions_a_heading_word_is_not_a_heading(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path, monkeypatch,
               description=("We are hiring for the role.\nOur requirements are simple and "
                            "we list them below in this paragraph, which is far too long to "
                            "read as a heading line.\nMore text."))
    assert [s["label"] for s in ctx["posting_sections"]] == ["Role"]


def test_matched_skills_are_highlighted_in_the_posting_text(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path, monkeypatch)
    assert "<mark>" in ctx["highlighted_html"]
    html = workspace.render_workspace(ctx)
    assert '<div class="post-text">' in html


def test_every_outbound_link_is_the_real_posting_in_a_new_tab(tmp_path, monkeypatch):
    """The header link and the submit step both leave for the same real posting."""
    for step in ("review", "submit"):
        html = workspace.render_workspace(_ctx(tmp_path, monkeypatch, step=step))
        exits = re.findall(r'<a[^>]+href="(https?://[^"]+)"[^>]*>', html)
        assert exits and set(exits) == {"https://example.invalid/1"}, step
        tags = re.findall(r'<a[^>]+href="https?://[^"]+"[^>]*>', html)
        for tag in tags:
            assert 'target="_blank"' in tag and 'rel="noopener"' in tag, tag


def test_submit_step_summarises_the_packet_and_the_blockers(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path, monkeypatch, step="submit", location="Toronto, Ontario", remote=False)
    ctx["packet"]["files"] = _files("resume.pdf", "posting.md")
    html = workspace.render_workspace(ctx)
    at = html.index("data-submit>")
    panel = html[at:html.index("</section>", at)]
    assert "2 files in the packet" in panel
    assert "built" in panel and "resume.pdf" in panel
    assert "Do not apply: 1 hard blocker." in panel and "Location is not Winnipeg" in panel
    assert panel.count("btn btn-primary") == 1  # the way out stays the one primary
    assert "Open real posting to submit" in panel
    # The liveness check is a plain form (no JS): POST to the API, back to this step.
    assert 'action="/api/postings/greenhouse%3Aacme%2F1/liveness"' in panel
    assert 'name="return" value="/posting/greenhouse%3Aacme%2F1?step=submit"' in panel
    assert ">Check if still open</button>" in panel
    assert 'data-live-status="unchecked"' in panel and "Not checked yet" in panel
    # After a check, the last status and its age replace the placeholder.
    ctx = _ctx(tmp_path, monkeypatch, step="submit", live_status="gone",
               live_checked_at=(datetime.now(timezone.utc) - timedelta(hours=1, minutes=5))
               .isoformat(timespec="seconds"))
    shown = workspace.render_workspace(ctx)
    shown = shown[shown.index("data-liveness"):]
    assert 'data-live-status="gone"' in shown and "Looks closed, checked" in shown
    assert "hour ago" in shown



def test_submit_lists_each_file_with_its_size_and_a_download_link(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path, monkeypatch, step="submit")
    ctx["packet"]["files"] = [
        {"name": "resume.pdf", "label": "resume.pdf", "age": "2m", "size": 4200,
         "size_h": "4.2 kB"},
        {"name": "posting.md", "label": "posting.md", "age": "2m", "size": 900, "size_h": "900 B"},
    ]
    html = workspace.render_workspace(ctx)
    at = html.index("data-submit>")
    panel = html[at:html.index("</section>", at)]
    assert "4.2 kB" in panel and "900 B" in panel
    assert 'href="/packets/' in panel and '?download=1" download' in panel
    assert ">View</a>" in panel and ">Download</a>" in panel


def test_submit_step_says_when_nothing_is_built_or_blocking(tmp_path, monkeypatch):
    html = workspace.render_workspace(_ctx(tmp_path, monkeypatch, step="submit"))
    at = html.index("data-submit>")
    panel = html[at:html.index("</section>", at)]
    assert "Nothing built yet." in panel
    assert "No unresolved blockers." in panel
    assert "Open real posting to submit" in panel


def test_the_bottom_bar_keeps_the_sentence_and_mark_applied_only(tmp_path, monkeypatch):
    html = workspace.render_workspace(_ctx(tmp_path, monkeypatch))
    bar = html[html.index('class="bottombar"'):html.index("</footer>")]
    assert "Nothing is sent from here. Every file is a draft you can edit." in bar
    assert "Mark as applied" in bar
    assert "Open real posting" not in bar


def test_a_non_http_url_is_never_linked(tmp_path, monkeypatch):
    html = workspace.render_workspace(_ctx(tmp_path, monkeypatch, step="submit",
                                          url="javascript:alert(1)"))
    assert "javascript:" not in html.replace("&quot;", '"') or "Open real posting" not in html
    assert "No usable posting link." in html
    assert 'class="orig"' not in html


def test_every_action_is_a_post_form(tmp_path, monkeypatch):
    html = workspace.render_workspace(_ctx(tmp_path, monkeypatch))
    for match in re.finditer(r"<form([^>]*)>", html):
        assert 'method="post"' in match.group(1), match.group(0)
    assert "<script" not in html and "onclick" not in html and "onchange" not in html


def test_status_select_offers_every_status_and_the_next_moves_first(tmp_path, monkeypatch):
    html = workspace.render_workspace(_ctx(tmp_path, monkeypatch, status="prepped"))
    assert '<optgroup label="Next move">' in html
    assert '<optgroup label="Any status">' in html
    for status in ("new", "shortlisted", "prepped", "applied", "interview", "offer",
                   "rejected", "closed", "skipped"):
        assert f'value="{status}"' in html
    assert 'name="status"' in html and "required" in html


def test_mark_as_applied_is_hidden_once_applied(tmp_path, monkeypatch):
    new_html = workspace.render_workspace(_ctx(tmp_path, monkeypatch))
    assert "Mark as applied" in new_html
    applied = workspace.render_workspace(_ctx(tmp_path, monkeypatch, status="applied"))
    assert "Mark as applied" not in applied


def test_nothing_is_sent_message_is_present(tmp_path, monkeypatch):
    html = workspace.render_workspace(_ctx(tmp_path, monkeypatch))
    assert "Nothing is sent from here. Every file is a draft you can edit." in html


# ---------- the document around the body ----------

def test_page_wraps_the_body_with_theme_tokens_and_the_script_last():
    page = ui.page("t", "<p>x</p>" + serve.POSTING_JS, body_class="screen-workspace",
                   body_attrs='data-uid="u"')
    assert page.startswith("<!DOCTYPE html>")
    assert "--bg:#f6f5f1" in page and "--bg:#121413" in page
    assert 'data-theme="dark"' in page
    assert page.rindex("<script") > page.index("<p>x</p>")
    assert 'data-uid="u"' in page


def test_no_external_resources_anywhere_in_the_shell():
    page = ui.page("t", "", body_class="x")
    assert "<link" not in page
    assert "@import" not in page
    assert "http://" not in page and "https://" not in page
    assert "src=" not in page


def test_store_helpers_used_by_the_screens(tmp_path):
    """browse() returns no description, orders by score, and its count agrees."""
    path = tmp_path / "j.db"
    store = Store(str(path))
    store.upsert_postings([
        {"uid": f"u{i}", "source": "s", "company": f"C{i}", "title": f"T{i}",
         "location": "Remote", "url": "https://example.invalid",
         "description": "x" * 500} for i in range(5)])
    for i in range(5):
        store.set_score(f"u{i}", float(i * 10), "general", [])
    rows = store.browse(limit=10)
    assert [r["score"] for r in rows] == [40.0, 30.0, 20.0, 10.0, 0.0]
    assert "description" not in rows[0]
    assert store.browse_count() == 5
    assert store.browse_count(hide_zero=True) == 4
    assert store.browse_count(min_score=25.0) == 2
    assert [r["uid"] for r in store.browse(min_score=25.0)] == ["u4", "u3"]
    assert store.sources() == ["s"]
    store.close()


def test_scrim_is_scoped_to_the_slide_over_breakpoint():
    """A dim backdrop at desktop width covers the pane it was meant to accompany."""
    css = ui.CSS
    rule = ".scrim.on{display:block}"
    assert css.count(rule) == 1
    assert css.index("@media (max-width:1100px)") < css.index(rule)
    # and the no-JS block must never leave a scrim on
    assert ".scrim.on" not in ui.NOSCRIPT_CSS
    assert ".scrim{display:none!important}" in ui.NOSCRIPT_CSS


def test_letter_action_is_labelled_for_what_it_will_do(tmp_path, monkeypatch):
    """With no letter the button drafts one; with a letter it rewrites it."""
    fresh = workspace.render_workspace(_ctx(tmp_path, monkeypatch, step="letter"))
    assert "Draft with AI" in fresh and "2-3 min" in fresh
    assert "Redraft with AI" not in fresh

    ctx = _ctx(tmp_path, monkeypatch, step="letter")
    ctx["body_text"] = "word " * 250
    again = workspace.render_workspace(ctx)
    assert "Redraft with AI" in again and "Draft with AI" not in again


def test_letter_step_with_no_letter_is_two_choices_not_an_editor(tmp_path, monkeypatch):
    html = workspace.render_workspace(_ctx(tmp_path, monkeypatch, step="letter"))
    at = html.index("data-letter>")
    panel = html[at:html.index("</section>", at)]
    assert "No letter yet." in panel
    # one primary while the fold-out is closed: the draft button
    empty = panel[:panel.index('class="own-letter"')]
    assert empty.count("btn btn-primary") == 1
    assert '<button class="btn btn-primary" type="submit" >Draft with AI' in empty
    assert "Draft with AI" in panel
    assert '<details class="own-letter"><summary class="btn">Write or paste your own' in panel
    # the editor exists, but only inside the fold-out
    assert panel.index('class="own-letter"') < panel.index('name="body"')
    # empty text: the primary PDF action cannot be pressed
    assert "data-recompile disabled" in panel


def test_letter_step_shows_the_editor_and_its_toolbar(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path, monkeypatch, step="letter")
    ctx["body_text"] = "Dear team,\n" + "line\n" * 29
    ctx["word_count"] = 250
    ctx["word_ok"] = True
    html = workspace.render_workspace(ctx)
    at = html.index("data-letter>")
    panel = html[at:html.index("</section>", at)]
    assert 'rows="31"' in panel
    assert "Save text" in panel and "Recompile PDF" in panel
    assert "data-recompile disabled" not in panel
    assert panel.count("btn btn-primary") == 1
    assert '<span class="mono" data-word-count>250</span>' in panel
    assert "target 240-320" in panel


def test_the_editor_is_at_least_twelve_lines(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path, monkeypatch, step="letter")
    ctx["body_text"] = "one line"
    html = workspace.render_workspace(ctx)
    assert 'rows="12"' in html


def test_the_checks_card_only_shows_when_there_is_a_letter(tmp_path, monkeypatch):
    without = workspace.render_workspace(_ctx(tmp_path, monkeypatch, step="letter"))
    assert "Letter checks" not in without
    ctx = _ctx(tmp_path, monkeypatch, step="letter")
    ctx["body_text"] = "short"
    ctx["letter_checks"] = [{"label": "240-320 words", "ok": False, "detail": "1 words"}]
    withchecks = workspace.render_workspace(ctx)
    assert "Letter checks" in withchecks
    assert 'data-letter-checks' in withchecks
    assert 'class="check warn"' in withchecks


def test_packet_and_letter_are_separate_actions(tmp_path, monkeypatch):
    """prep builds the resume and posting.md; the letter is its own kind, its own POST."""
    packet = workspace.render_workspace(_ctx(tmp_path, monkeypatch, step="packet"))
    assert packet.count('name="kind" value="prep"') == 1
    letter = workspace.render_workspace(_ctx(tmp_path, monkeypatch, step="letter"))
    assert letter.count('name="kind" value="letter"') == 1


# ---------- AI actions, analysis card, queue strip, reach ----------

def _html(tmp_path, monkeypatch, step="review", **keys):
    ctx = _ctx(tmp_path, monkeypatch, step=step)
    ctx.update(keys)
    return workspace.render_workspace(ctx)


def _ago(seconds: int) -> str:
    from datetime import timedelta
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat(timespec="seconds")


def _queue_strip_of(html: str) -> str:
    start = html.index("data-queue-strip")
    return html[html.rindex("<div", 0, start):html.index("</div></div>", start) + len("</div>")]


def test_ai_card_has_one_analysis_form_with_the_analyze_label(tmp_path, monkeypatch):
    html = _html(tmp_path, monkeypatch)
    assert html.count('name="kind" value="analysis"') == 1
    form = html[html.index('name="kind" value="analysis"'):]
    form = form[:form.index("</form>")]
    assert 'data-action-form' in html[html.rindex("<form", 0, html.index('name="kind" value="analysis"')):
                                      html.index('name="kind" value="analysis"')]
    assert "Analyze fit" in form and "btn-primary" in form


def test_no_critique_or_gaps_form_is_emitted(tmp_path, monkeypatch):
    html = _html(tmp_path, monkeypatch, critique_text="old", gaps_text="old gaps")
    assert 'value="critique"' not in html and 'value="gaps"' not in html
    assert ">Critique fit<" not in html and ">Find gaps<" not in html


def test_review_step_without_analysis_offers_one_analyze_primary(tmp_path, monkeypatch):
    html = _html(tmp_path, monkeypatch)
    assert "No fit analysis yet." in html
    assert 'name="kind" value="analysis"' in html and "Analyze fit" in html
    at = html.index('data-review')
    panel = html[at:html.index("</section>", at)]
    assert panel.count("btn btn-primary") == 1          # analyze is the only primary here
    foot = html[html.index('class="panel-foot"'):]
    assert "Continue to Packet" in foot and 'class="btn"' in foot


def test_the_analysis_is_parsed_into_finding_cards(tmp_path, monkeypatch):
    text = ("VERDICT\nthe role is a stretch.\n\n"
            "FIT\nIn projects (Triage), the applicant builds tooling, while facts.yaml shows 639 tests.\n\n"
            "GAPS\nFirst, production tenure: the role requires 5+ years, while facts.yaml "
            "proves one role.\n\n"
            "TO_BE_PERFECT\nOver three to four months, publish a benchmark with <script>x</script>.")
    html = _html(tmp_path, monkeypatch, analysis_text=text)
    assert html.count('<article class="finding') == 3
    assert "tag-dec" in html and "tag-str" in html and "tag-gap" in html
    assert '<span class="fk">Posting says</span>' in html
    assert '<span class="fk">Your facts say</span>' in html
    assert "the role requires 5+ years" in html and "one role." in html
    assert '<h3 class="f-title">Production tenure</h3>' in html
    # the advice folds away and carries its estimate in the summary
    assert '<details class="card perfect">' in html
    summary = html[html.index('<details class="card perfect">'):]
    summary = summary[:summary.index("</summary>")]
    assert "To be perfect" in summary and "three to four months" in summary
    # escaped, and not a monospace dump
    assert "&lt;script&gt;x&lt;/script&gt;" in html and "<script>x" not in html
    assert 'class="pre"' not in html


def test_analysis_header_says_when_and_offers_a_secondary_re_run(tmp_path, monkeypatch):
    html = _html(tmp_path, monkeypatch, analysis_text="VERDICT\nok\n\nGAPS\nA gap, but no facts "
                                                      "contrast in this one.",
                 analysis_when=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    at = html.index('class="panel-hd"')
    head = html[at:html.index("</header>", at)]
    assert "Fit analysis" in head and "finished just now" in head
    assert "Re-run" in head and "btn-primary" not in head


def test_an_unparseable_analysis_falls_back_to_a_raw_block(tmp_path, monkeypatch):
    html = _html(tmp_path, monkeypatch,
                 analysis_text="Just some prose with no headings, and <b>markup</b>.")
    assert "could not be split into findings" in html
    assert '<details class="raw-analysis">' in html
    assert "Just some prose with no headings" in html
    assert "<b>markup</b>" not in html and "&lt;b&gt;markup&lt;/b&gt;" in html
    assert "Continue to Packet" in html


def test_legacy_output_folds_away_when_there_is_no_analysis(tmp_path, monkeypatch):
    legacy = _html(tmp_path, monkeypatch, critique_text="OLD-CRIT", gaps_text="OLD-GAPS")
    assert legacy.count("OLD-CRIT") == 1 and legacy.count("OLD-GAPS") == 1
    assert "Older run output" in legacy and "No fit analysis yet." in legacy

    ctx = _ctx(tmp_path, monkeypatch)
    ctx.update(analysis_text="VERDICT\nok\n\nGAPS\ngap.\n", critique_text="OLD-CRIT",
               gaps_text="OLD-GAPS")
    html = workspace.render_workspace(ctx)
    assert "OLD-CRIT" not in html and "OLD-GAPS" not in html


def test_queue_strip_shows_a_queued_job_with_its_position(tmp_path, monkeypatch):
    job = {"kind": "analysis", "state": "queued", "position": 2, "created": _ago(30)}
    html = _html(tmp_path, monkeypatch, latest_job=job, busy=True)
    strip = _queue_strip_of(html)
    assert "data-queue-strip" in strip and 'role="status"' in strip
    assert "analysis queued #2" in strip
    assert "hidden" not in strip.split(">")[0]


def test_queue_strip_shows_running_stage_and_minutes(tmp_path, monkeypatch):
    job = {"kind": "analysis", "state": "running", "stage": "model", "started": _ago(120),
           "est_seconds": 200, "created": _ago(130)}
    strip = _queue_strip_of(_html(tmp_path, monkeypatch, latest_job=job, busy=True))
    assert "analysis running: selecting the model" in strip
    assert "2 min elapsed" in strip and "about 2 min left" in strip


def test_queue_strip_reads_starting_when_no_stage_yet(tmp_path, monkeypatch):
    job = {"kind": "letter", "state": "running", "stage": "", "started": _ago(5)}
    strip = _queue_strip_of(_html(tmp_path, monkeypatch, latest_job=job, busy=True))
    assert "letter running: starting" in strip


def test_queue_strip_reports_done_and_failed_jobs(tmp_path, monkeypatch):
    done = _queue_strip_of(_html(tmp_path, monkeypatch, latest_job={
        "kind": "analysis", "state": "done", "message": "analysis.md written"}))
    assert "last analysis done" in done and "analysis.md written" in done
    failed = _queue_strip_of(_html(tmp_path, monkeypatch, latest_job={
        "kind": "analysis", "state": "failed", "error": "stage_failed: boom"}))
    assert "last analysis failed" in failed and "stage_failed: boom" in failed


def test_queue_strip_is_empty_and_hidden_with_no_job(tmp_path, monkeypatch):
    strip = _queue_strip_of(_html(tmp_path, monkeypatch))
    assert " hidden" in strip.split(">")[0]
    assert '<span class="q-main"></span>' in strip and '<span class="q-note"></span>' in strip


def test_queue_strip_text_is_escaped(tmp_path, monkeypatch):
    job = {"kind": "analysis", "state": "failed", "error": "<img src=x>"}
    html = _html(tmp_path, monkeypatch, latest_job=job)
    assert "<img src=x>" not in html and "&lt;img src=x&gt;" in html


def test_seniority_is_a_blocker_only_when_the_title_is_above_the_operator(tmp_path, monkeypatch):
    out = _ctx(tmp_path, monkeypatch, title="Director of Engineering")
    assert [b["kind"] for b in out["blockers"]] == ["seniority"]
    assert out["blockers"][0]["title"] == "Out of reach"
    html = workspace.render_workspace(out)
    assert 'data-blocker="seniority"' in html and "director-level title" in html

    stretch = _ctx(tmp_path, monkeypatch, title="Senior Software Engineer")
    assert [b["kind"] for b in stretch["blockers"]] == ["seniority"]
    assert stretch["blockers"][0]["title"] == "A stretch"

    calm = _html(tmp_path, monkeypatch)
    assert "data-blocker=" not in calm.split("data-blockers")[1].split("</section>")[0]


def test_gated_analysis_never_carries_force(tmp_path, monkeypatch):
    reach = {"verdict": "out_of_reach", "why": "manager-level title"}
    gated = _html(tmp_path, monkeypatch, reach=reach)
    assert 'name="force"' not in gated
    assert "Analysis is gated" not in gated
    assert "Analysis is gated" not in _html(tmp_path, monkeypatch)


OOR_REACH = {"verdict": "out_of_reach", "why": "principal-level title"}
STAGE_TABLE = {
    "context": "uploading the source", "submit": "committing the task",
    "device": "finding the phone", "start": "starting",
    "relaunch retry": "retrying the app relaunch", "model": "selecting the model",
    "navigate": "opening the task surface", "composer": "opening the task surface",
    "focus": "typing the task", "inject": "typing the task", "send": "sending",
    "confirm": "waiting on the app", "trigger": "committing the task",
    "await": "waiting on the app", "record": "saving the result",
}


def _analysis_form_of(html: str) -> str:
    start = html.index('name="kind" value="analysis"')
    return html[html.rindex("<form", 0, start):html.index("</form>", start) + len("</form>")]


def test_out_of_reach_analysis_is_one_unforced_click_labelled_analyze_fit(tmp_path, monkeypatch):
    html = _html(tmp_path, monkeypatch, reach=OOR_REACH)
    form = _analysis_form_of(html)
    assert ">Analyze fit<" in form or "Analyze fit" in form
    assert "Analyze anyway" not in html
    assert 'name="force"' not in html and 'name="force"' not in form
    assert ('<span class="muted">out of reach: the call is gated, click once and confirm</span>'
            in form)
    assert "Analysis is gated" not in html


def test_out_of_reach_letter_form_is_unforced_and_gated(tmp_path, monkeypatch):
    html = _html(tmp_path, monkeypatch, reach=OOR_REACH, step="letter")
    start = html.index('name="kind" value="letter"')
    form = html[html.rindex("<form", 0, start):html.index("</form>", start) + len("</form>")]
    assert 'name="force"' not in form
    assert "out of reach: the call is gated, click once and confirm" in form
    assert "two to three minutes" not in form


def test_in_reach_analysis_has_no_gate_note(tmp_path, monkeypatch):
    form = _analysis_form_of(_html(tmp_path, monkeypatch))
    assert "Analyze fit" in form and "gated" not in form and 'name="force"' not in form


@pytest.mark.parametrize("stage,phrase", sorted(STAGE_TABLE.items()) + [("", "starting")])
def test_every_stage_name_maps_to_its_phrase(stage, phrase):
    assert workspace._stage_label(stage) == phrase


def test_unknown_stage_stays_visible_rather_than_blank():
    assert workspace._stage_label("probe-x") == "probe-x"


def test_running_strip_shows_the_phrase_for_each_real_stage(tmp_path, monkeypatch):
    for stage, phrase in STAGE_TABLE.items():
        job = {"kind": "analysis", "state": "running", "stage": stage, "started": _ago(5)}
        strip = _queue_strip_of(_html(tmp_path, monkeypatch, latest_job=job, busy=True))
        assert f"analysis running: {phrase}" in strip, stage


def test_queue_strip_prefers_server_eta_over_est_seconds(tmp_path, monkeypatch):
    job = {"kind": "analysis", "state": "running", "stage": "model", "started": _ago(120),
           "est_seconds": 200, "eta_s": 61, "created": _ago(130)}
    strip = _queue_strip_of(_html(tmp_path, monkeypatch, latest_job=job, busy=True))
    assert "about 2 min left" in strip and "2 min elapsed" in strip
    fresh = dict(job, eta_s=60)
    assert "about 1 min left" in _queue_strip_of(
        _html(tmp_path, monkeypatch, latest_job=fresh, busy=True))


def test_queue_strip_omits_place_when_position_is_zero_or_missing(tmp_path, monkeypatch):
    zero = _queue_strip_of(_html(tmp_path, monkeypatch, busy=True, latest_job={
        "kind": "analysis", "state": "queued", "position": 0, "created": _ago(5)}))
    assert "analysis queued" in zero and "queued #" not in zero
    missing = _queue_strip_of(_html(tmp_path, monkeypatch, busy=True, latest_job={
        "kind": "analysis", "state": "queued", "created": _ago(5)}))
    assert "analysis queued" in missing and "queued #" not in missing


def _posting_js_phrase_keys() -> set[str]:
    js = serve.POSTING_JS
    segment = js[js.index("var STAGE_PHRASES"):js.index("};", js.index("var STAGE_PHRASES"))]
    return set(re.findall(r'(?:^|[\s{,])"?([a-z][a-z ]*?)"?\s*:', segment))


def test_posting_js_phrase_map_has_the_same_stages_as_workspace():
    # The JS phrase map and workspace.STAGE_LABELS must name the same stages. Any gap
    # (either direction) fails here rather than being pinned as a known exception.
    assert _posting_js_phrase_keys() == set(workspace.STAGE_LABELS)


# ---------- packet card and prep row before and after a finished prep job ----------

def _files(*names):
    labels = {"resume.pdf": "resume.pdf", "cover_letter.pdf": "letter.pdf", "posting.md": "posting.md"}
    return [{"name": n, "label": labels[n], "age": "just now"} for n in names]


def test_packet_step_shows_the_variant_and_the_six_way_to_change_it(tmp_path, monkeypatch):
    html = workspace.render_workspace(_ctx(tmp_path, monkeypatch, step="packet"))
    at = html.index('data-packet>')
    panel = html[at:html.index("</section>", at)]
    assert "Resume variant" in panel and ">ai-platform<" in panel
    assert "4 of 6" in panel          # general, ai-security, sec-content, ai-platform, ...
    assert '<form method="post" action="/api/postings/greenhouse%3Aacme%2F1/variant"' in panel
    assert panel.count("<option") == 6
    assert '<option value="ai-platform" selected>' in panel
    assert 'name="return" value="/posting/greenhouse%3Aacme%2F1?step=packet"' in panel


def test_packet_step_says_when_the_file_on_disk_used_another_variant(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path, monkeypatch, step="packet")
    ctx["variant"] = "general"
    ctx["packet"]["resume_variant"] = "ai-platform"
    html = workspace.render_workspace(ctx)
    assert "built with ai-platform" in html


def test_packet_step_has_one_primary_build_then_continue(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path, monkeypatch, step="packet")
    panel = workspace.render_workspace(ctx)
    at = panel.index("data-packet>")
    panel = panel[at:panel.index("</section>", at)]
    assert panel.count("btn btn-primary") == 1
    assert "Build packet" in panel and "takes seconds" in panel
    assert "Continue to Letter" not in panel

    ctx["packet"]["files"] = _files("resume.pdf", "posting.md")
    panel = workspace.render_workspace(ctx)
    at = panel.index("data-packet>")
    panel = panel[at:panel.index("</section>", at)]
    assert panel.count("btn btn-primary") == 1
    assert "Continue to Letter" in panel and 'href="?step=letter' in panel
    assert "Build packet" not in panel


def test_packet_before_a_prep_job_offers_build_and_says_nothing_built(tmp_path, monkeypatch):
    html = workspace.render_workspace(_ctx(tmp_path, monkeypatch, step="packet"))
    assert 'class="prep-row"' in html
    assert "data-packet-file" not in html
    assert 'data-packet-empty>Nothing built yet' in html   # visible, not hidden


def test_packet_after_a_done_prep_job_lists_the_resume_and_drops_the_prep_row(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path, monkeypatch, step="packet")
    ctx["packet"]["files"] = _files("resume.pdf", "posting.md")
    html = workspace.render_workspace(ctx)
    assert 'data-packet-file="resume.pdf"' in html
    assert 'href="/packets/' in html and "/resume.pdf" in html
    assert 'class="prep-row"' not in html          # the packet has a resume
    assert 'data-packet-empty hidden' in html      # the empty note is hidden by the server too
    assert "no resume yet" not in html and 'data-packet-missing' in html  # only the letter is missing
    assert "no letter yet" in html


def test_prep_row_stays_while_the_packet_has_no_resume(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path, monkeypatch, step="packet")
    ctx["packet"]["files"] = _files("posting.md")
    assert 'class="prep-row"' in workspace.render_workspace(ctx)


def test_letter_done_adds_a_letter_link_without_removing_the_resume(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path, monkeypatch, step="packet")
    ctx["packet"]["files"] = _files("resume.pdf", "cover_letter.pdf")
    html = workspace.render_workspace(ctx)
    assert 'data-packet-file="cover_letter.pdf"' in html
    assert "no letter yet" not in html and "no resume yet" not in html
    assert "no posting.md yet" in html  # the tail still names what is really missing


def _analyze_form_of(html: str) -> str:
    start = html.index('name="kind" value="analysis"')
    return html[start:html.index("</form>", start)]


def test_analysis_button_reads_analyze_fit_without_analysis_text(tmp_path, monkeypatch):
    html = _html(tmp_path, monkeypatch)
    form = _analyze_form_of(html)
    assert "Analyze fit" in form and "Re-run analysis" not in html


def test_analysis_button_reads_re_run_with_analysis_text(tmp_path, monkeypatch):
    html = _html(tmp_path, monkeypatch, analysis_text="Fit: good")
    form = _analyze_form_of(html)
    assert "Re-run" in form and "Analyze fit" not in form
    assert 'name="force"' not in form


def test_stage_label_names_the_packet_build_for_an_unstaged_prep_job():
    assert workspace._stage_label("", "prep") == "building the packet"
    assert workspace._stage_label(None, "analysis") == "starting"
    assert workspace._stage_label("send", "prep") == "sending"


def test_submit_panel_shows_do_not_apply_banner_but_keeps_override_link(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path, monkeypatch, step="submit", location="Toronto, Ontario", remote=False)
    ctx["packet"]["files"] = _files("resume.pdf")
    assert ctx["apply_gate"]["decision"] == "blocked"
    html = workspace.render_workspace(ctx)
    at = html.index("data-submit>")
    panel = html[at:html.index("</section>", at)]
    # One banner for the page, in the rail card; the submit step states the decision in
    # words rather than repeating it.
    assert 'data-apply-gate="blocked"' in html
    assert html.count('data-apply-gate=') == 1
    assert "Do not apply: 1 hard blocker." in panel
    assert "still works if you decide to apply anyway" in panel
    # The override is never removed: the external link is still a plain anchor that works
    # with JavaScript off. Draft answers is the other primary button in this panel.
    assert panel.count("btn btn-primary") == 1
    assert ('<a class="btn btn-primary" href="https://example.invalid/1" target="_blank" '
            'rel="noopener">Open real posting to submit</a>') in panel

    # A clear posting shows no banner at all.
    clear = _ctx(tmp_path, monkeypatch, step="submit", location="Winnipeg, Manitoba", remote=False)
    clear["packet"]["files"] = _files("resume.pdf")
    html = workspace.render_workspace(clear)
    at = html.index("data-submit>")
    assert "data-apply-gate" not in html[at:html.index("</section>", at)]


def test_packet_step_shows_coverage_and_hides_it_without_a_resume(tmp_path, monkeypatch):
    posting = {"description": "Build Docker and Python pipelines."}
    monkeypatch.setattr(serve, "_resume_text", lambda slug: "Python services at scale.")
    ctx = _ctx(tmp_path, monkeypatch, step="packet", description=posting["description"])
    assert ctx["packet"]["coverage"]["available"] is True
    panel = workspace.render_workspace(ctx)
    assert "data-resume-coverage" in panel
    assert "Coverage: 1 of 2 posting keywords are on this resume" in panel
    assert '<span class="tag tag-gap">docker</span>' in panel
    assert '<span class="tag tag-gap">python</span>' not in panel

    monkeypatch.setattr(serve, "_resume_text", lambda slug: None)
    ctx = _ctx(tmp_path, monkeypatch, step="packet", description=posting["description"])
    assert ctx["packet"]["coverage"]["available"] is False
    panel = workspace.render_workspace(ctx)
    assert "data-resume-coverage" not in panel and "Coverage:" not in panel


def test_packet_coverage_is_silent_when_the_posting_asks_for_nothing(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path, monkeypatch, step="packet")
    ctx["packet"]["coverage"] = {"covered": [], "missing": [], "ratio": 1.0, "available": True}
    html = workspace.render_workspace(ctx)
    assert "data-resume-coverage" not in html and "Coverage:" not in html


def test_submit_step_shows_the_questions_form_and_the_answers_when_present(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path, monkeypatch, step="submit")
    slug = ctx["packet"]["slug"]
    uid_q = quote(ctx["posting"]["uid"], safe="")

    def panel_of(ctx):
        html = workspace.render_workspace(ctx)
        at = html.index("data-submit>")
        return html[at:html.index("</section>", at)]

    panel = panel_of(ctx)
    block = panel[panel.index("data-questions"):panel.index("Before you go")]
    assert f'action="/api/postings/{uid_q}/action" data-action-form' in block
    assert 'method="post"' in block
    assert '<input type="hidden" name="kind" value="questions">' in block
    assert 'name="questions"' in block and "required" in block
    assert "<label" in block and 'for="questions-text"' in block
    assert 'id="questions-text"' in block
    assert "Draft answers · 2-3 min" in block and "btn btn-primary" not in block
    assert "disabled" not in block and "data-answers" not in panel
    ctx["busy"] = True
    assert "disabled" in panel_of(ctx)[panel_of(ctx).index("data-questions"):]
    # Once answers.md exists, it shows in a collapsed block with a link to the file.
    (tmp_path / "packets" / slug).mkdir(parents=True, exist_ok=True)
    (tmp_path / "packets" / slug / "answers.md").write_text(
        "**Why Acme?**\nBecause of the SOC work.\n", encoding="utf-8")
    shown = panel_of(_ctx(tmp_path, monkeypatch, step="submit"))
    assert "data-answers" in shown and "<summary>Drafted answers</summary>" in shown
    assert "Because of the SOC work." in shown
    assert f'href="/packets/{slug}/answers.md"' in shown
    assert shown.index("data-questions") < shown.index("Before you go")


def test_submit_step_shows_the_follow_up_slot(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path, monkeypatch, step="submit")
    html = workspace.render_workspace(ctx)
    panel = html[html.index("data-submit>"):]
    panel = panel[:panel.index("</section>")]
    slot = panel[panel.index("data-followup"):panel.index('<div class="panel-foot"')]
    week = (date.today() + timedelta(days=7)).isoformat()
    assert 'type="date" id="follow-up-date" name="date" value="' + week + '"' in slot
    assert ">Set follow-up</button>" in slot
    assert 'action="/api/postings/greenhouse%3Aacme%2F1/followup"' in slot
    assert 'name="return" value="/posting/greenhouse%3Aacme%2F1?step=submit"' in slot
    assert "followup-clear" not in slot and "No follow-up date set." in slot
    assert "<form" in slot and "onclick" not in slot  # plain POST forms, no JavaScript

    ctx = _ctx(tmp_path, monkeypatch, step="submit", follow_up_at="2026-12-01")
    html = workspace.render_workspace(ctx)
    panel = html[html.index("data-submit>"):]
    slot = panel[panel.index("data-followup"):panel.index('<div class="panel-foot"')]
    assert "Follow up on 2026-12-01" in slot
    assert 'class="followup-clear"' in slot and 'name="clear" value="1"' in slot
    assert ">Clear</button>" in slot
