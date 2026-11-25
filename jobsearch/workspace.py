"""The posting workspace: posting text, letter editor, checks, packet and activity.

Renders the inner body fragment only; the caller wraps it in ui.page() and appends its
own inline script. Every action on this screen is a plain form POST or a real link, so
the page works with JavaScript disabled. Nothing here sends anything to an employer.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote

from jobsearch.analysis_findings import parse_analysis
from jobsearch.checks import apply_gate, posted_phrase
from jobsearch.copilot import QUESTIONS_MAX_CHARS
from jobsearch.ui import btn, card, esc, rel_phrase

__all__ = ["render_workspace"]

# How the last posting-URL check is worded on the Submit step. "Could not tell" is
# deliberately not "closed": a bot wall says nothing about the job.
_LIVE_LABELS = {"live": "Still open", "gone": "Looks closed", "unknown": "Could not tell"}
# Where the liveness form sends a browser back: the Submit step of this posting.
POSTING_STEP_PATH = "/posting/{}?step=submit"

# Statuses at or past "applied" in the workflow, plus the terminal states. The
# "Mark as applied" shortcut is hidden for these.
_APPLIED_OR_LATER = ("applied", "interview", "offer", "rejected", "closed", "skipped")

_KIND_LABELS = {
    "letter": ("Redraft with AI", "letter"),
    "analysis": ("Analyze fit", "analysis"),
    "critique": ("Critique fit", "critique"),  # legacy: old jobs and packets only
    "gaps": ("Find gaps", "gaps"),  # legacy: old jobs and packets only
    "questions": ("Draft answers", "questions"),
}

# The dispatcher's stage names, in the order it runs them, plus the two markers
# ("start", "relaunch retry"). Keep in step with the dispatcher's stage list.
STAGE_LABELS = {
    "context": "uploading the source",
    "submit": "committing the task",
    "device": "finding the phone",
    "start": "starting",
    "relaunch retry": "retrying the app relaunch",
    "model": "selecting the model",
    "navigate": "opening the task surface",
    "composer": "opening the task surface",
    "focus": "typing the task",
    "inject": "typing the task",
    "send": "sending",
    "confirm": "waiting on the app",
    "trigger": "committing the task",
    "await": "waiting on the app",
    "record": "saving the result",
}


def render_workspace(ctx: dict) -> str:
    """Return the workspace body fragment for one posting.

    The centre column renders one step at a time, and which step is decided on the
    server from ?step=..., so a tab is a plain link and the page works without JS.
    """
    posting = ctx.get("posting") or {}
    uid = str(posting.get("uid") or "")
    quid = quote(uid, safe="")
    status = posting.get("status") or ""
    url = posting.get("url") or ""
    busy = bool(ctx.get("busy"))
    latest = ctx.get("latest_job")
    kinds = {k.get("kind"): k for k in (ctx.get("kinds") or [])}
    step = ctx.get("step") or "review"

    return "".join([
        f'<div class="ws" data-uid="{esc(uid)}" data-step="{esc(step)}">',
        _topbar(ctx, posting, quid, status, step),
        '<div class="cols">',
        '<section class="col col-l" aria-label="Posting">' + _posting_col(ctx) + "</section>",
        f'<section class="col col-c" data-panel="{esc(step)}">'
        + _panel(ctx, quid, step, busy, latest, kinds) + "</section>",
        '<section class="col col-r" aria-label="Context">' + _rail(ctx, quid, step) + "</section>",
        "</div>",
        _bottombar(ctx, quid, status),
        "</div>",
    ])


def step_href(step: str, ctx: dict) -> str:
    """The URL for one step. A query string, so a tab never needs JavaScript.

    The backlog filter that led here travels along as `back`, so returning to the
    list from any step lands on the same filtered view.
    """
    back = str(ctx.get("back_url") or "")
    href = f"?step={quote(str(step), safe='')}"
    return href + (f"&back={quote(back, safe='')}" if back else "")


def _tabs(ctx: dict, step: str) -> str:
    """Review / Packet / Letter / Submit as links, each carrying its own done check."""
    out = ['<nav class="tabs" aria-label="Workspace steps">']
    for row in ctx.get("steps") or []:
        key = str(row.get("key") or "")
        here = key == step
        cls = "tab" + (" done" if row.get("done") else "") + (" on" if here else "")
        mark = '<span class="tk" aria-hidden="true">&#10003;</span>' if row.get("done") else ""
        aria = ' aria-current="step"' if here else ""
        out.append(
            f'<a class="{cls}" href="{esc(step_href(key, ctx))}"{aria}>'
            f'<span class="tn">{esc(row.get("label") or key)}</span>{mark}</a>'
        )
    out.append("</nav>")
    return "".join(out)


def _panel(ctx: dict, quid: str, step: str, busy: bool, latest: dict | None,
           kinds: dict) -> str:
    """The one step's content for the centre column."""
    if step == "packet":
        return _packet_panel(ctx, quid, busy)
    if step == "letter":
        return _letter_panel(ctx, quid, busy, latest)
    if step == "submit":
        return _submit_panel(ctx)
    return _review_panel(ctx, quid, busy, latest)


def _posting_col(ctx: dict) -> str:
    """The posting in short sections with a chip row to jump to each.

    The chips are plain anchors, so they work without JavaScript, and the column
    scrolls on its own: the text beside a step never moves the step.
    """
    sections = ctx.get("posting_sections") or []
    if len(sections) <= 1:
        return f'<div class="post-text">{ctx.get("highlighted_html") or ""}</div>'

    nav = "".join(
        f'<a class="chip" href="#sec-{esc(s.get("key") or "")}">{esc(s.get("label") or "")}</a>'
        for s in sections
    )
    body = "".join(
        f'<section class="post-sec" id="sec-{esc(s.get("key") or "")}">'
        f'<h3 class="post-h">{esc(s.get("label") or "")}</h3>'
        f'<div class="post-text">{s.get("html") or ""}</div></section>'
        for s in sections
    )
    return f'<nav class="post-nav" aria-label="Posting sections">{nav}</nav>' \
           f'<div class="post-sections">{body}</div>'



def _rail(ctx: dict, quid: str, step: str) -> str:
    """The right rail: the decision card, the step's own context, then activity.

    A card with nothing to say is not rendered at all, so the rail never carries an
    empty panel saying 'nothing yet'.
    """
    parts = [_blockers_card(ctx)]
    if step == "letter" and (ctx.get("letter_checks") or []):
        parts.append(_checks_card(ctx))
    if step == "packet":
        built = _packet_source_card(ctx)
        if built:
            parts.append(built)
    activity = _activity_card(ctx.get("events") or [])
    if activity:
        parts.append(activity)
    return "".join(parts)


def _packet_source_card(ctx: dict) -> str:
    """Which build produced the resume on disk, in one line. Nothing when never built."""
    for event in reversed(ctx.get("events") or []):
        detail = str(event.get("detail") or "")
        if "resume" not in detail or "(" not in detail:
            continue
        variant = detail.rsplit("(", 1)[-1].rstrip(")").strip()
        if variant.endswith(" resume"):
            variant = variant[: -len(" resume")].strip()
        return card("Packet source",
                    f'<div class="meta">{esc(_ts(event.get("ts")))} &middot; built with '
                    f'<strong class="mono">{esc(variant)}</strong></div>',
                    card_id="packet-built")
    return ""


_BLOCKER_TAGS = {"language": ("Language", "tag-gap"), "location": ("Location", "tag-gap"),
                 "seniority": ("Seniority", "tag-gap"), "liveness": ("Closed", "tag-gap"),
                 "verdict": ("Verdict", "tag-gap"), "sponsorship": ("Visa", "tag-gap")}

_GATE_TITLES = {"blocked": "Do not apply", "caution": "Proceed with caution"}


def _gate(ctx: dict) -> dict:
    """The gate the page was built with, or one computed from the blocker rows."""
    return ctx.get("apply_gate") or apply_gate(ctx.get("blockers") or [])


def _gate_banner(gate: dict, detail: str = "") -> str:
    """The one-line gate banner. Blocked and caution are the only decisions that show one."""
    decision = str(gate.get("decision") or "clear")
    title = _GATE_TITLES.get(decision, "")
    text = f'<strong>{esc(title)}.</strong>' if title else ""
    if detail:
        text += f" {esc(detail)}"
    return (f'<div class="gate-banner gate-{esc(decision)}" data-apply-gate="{esc(decision)}">'
            f"{text}</div>")


def _blockers_card(ctx: dict) -> str:
    """Every blocker in one place: language, location, seniority, liveness and verdict."""
    rows = ctx.get("blockers") or []
    if not rows:
        return ('<section class="card blockers calm" data-blockers>'
                '<div class="card-t">Decide before applying</div>'
                '<p class="bl-calm">Nothing blocking. No language, location or seniority flag.</p>'
                "</section>")
    gate = _gate(ctx)
    counts = f"{gate.get('hard_count', 0)} hard, {gate.get('soft_count', 0)} soft."
    out = ['<section class="card blockers" data-blockers>',
           '<div class="card-t">Decide before applying</div>',
           _gate_banner(gate, counts)]
    for row in rows:
        kind = str(row.get("kind") or "")
        label, cls = _BLOCKER_TAGS.get(kind, (kind.title() or "Flag", "tag-gap"))
        body = ""
        if row.get("posting"):
            body += (f'<div class="f-row"><span class="fk">Posting says</span>'
                     f'<span class="fv">{esc(row["posting"])}</span></div>')
        if row.get("facts"):
            body += (f'<div class="f-row"><span class="fk">Your facts say</span>'
                     f'<span class="fv">{esc(row["facts"])}</span></div>')
        out.append(
            f'<div class="bl" data-blocker="{esc(kind)}">'
            f'<div class="bl-hd"><span class="tag {cls}">{esc(label)}</span>'
            f'<span class="bl-t">{esc(row.get("title") or "")}</span></div>'
            f'<div class="f-rows">{body}</div></div>'
        )
    out.append("</section>")
    return "".join(out)


def _submit_files_block(ctx: dict) -> str:
    """Slot 1 of the submit panel: what is in the packet, each file with view and download."""
    packet = ctx.get("packet") or {}
    files = packet.get("files") or []
    slug = packet.get("slug")
    if files:
        rows = []
        for f in files:
            name = str(f.get("name") or "")
            href = f"/packets/{quote(str(slug or ''), safe='')}/{quote(name, safe='')}"
            rows.append(
                f'<div class="li" data-packet-file="{esc(name)}">'
                f'<span class="pf-name">{esc(f.get("label") or name)}'
                f'<span class="meta"> {esc(f.get("size_h") or "")} &middot; '
                f'built {esc(f.get("age") or "")}</span></span>'
                f'<span class="pf-acts">'
                f'<a class="link-quiet" href="{esc(href)}">View</a>'
                f'<a class="link-quiet" href="{esc(href)}?download=1" download>Download</a>'
                "</span></div>"
            )
        listing = f'<div class="pkt-files">{"".join(rows)}</div>'
    else:
        listing = ('<p class="bl-calm">Nothing built yet. The packet step builds the resume '
                   "and the posting text.</p>")
    return ('<div class="submit-block"><div class="card-t">In the packet</div>'
            + listing + "</div>")


def _submit_liveness_block(ctx: dict) -> str:
    """Whether the posting URL still answers as open, and a plain form to re-check it.

    The form is a normal POST that the server answers with a 303 back here, so it works
    with no JavaScript. The label says what the last check saw, not a verdict on the job.
    """
    posting = ctx.get("posting") or {}
    uid = str(posting.get("uid") or "")
    quid = quote(uid, safe="")
    status = posting.get("live_status") or ""
    checked = posting.get("live_checked_at") or ""
    label = _LIVE_LABELS.get(status, "Not checked yet")
    age = rel_phrase(checked) if checked else ""
    last = f"{label}, checked {age}" if age else label
    return (
        '<div class="submit-block" data-liveness>'
        '<div class="card-t">Still open?</div>'
        f'<p class="meta" data-live-status="{esc(status or "unchecked")}">{esc(last)}</p>'
        f'<form method="post" action="/api/postings/{quid}/liveness" class="liveness-form">'
        f'<input type="hidden" name="return" value="{esc(POSTING_STEP_PATH.format(quid))}">'
        '<button class="btn" type="submit">Check if still open</button>'
        "</form></div>"
    )


def _submit_questions_block(ctx: dict) -> str:
    """Slot 2: paste the employer's application questions and draft the answers.

    A plain POST to the action route (kind=questions, questions=...), so it works with no
    JavaScript; the page script queues it and reuses the queue strip when JS is on. The
    drafted answers.md, when it exists, shows in a collapsible block below the form.
    """
    posting = ctx.get("posting") or {}
    quid = quote(str(posting.get("uid") or ""), safe="")
    busy = bool(ctx.get("busy"))
    form = (
        f'<form method="post" action="/api/postings/{quid}/action" data-action-form '
        'class="questions-form" data-questions-form>'
        '<input type="hidden" name="kind" value="questions">'
        '<label class="meta" for="questions-text">Paste the application questions, '
        "as the employer wrote them</label>"
        '<textarea class="q-text" id="questions-text" name="questions" rows="8" required '
        f'maxlength="{QUESTIONS_MAX_CHARS}"></textarea>'
        + _ai_button("questions", busy, "Draft answers · 2-3 min")
        + "</form>"
    )
    shown = ""
    answers = ctx.get("answers_text")
    if answers is not None:
        href = f"/packets/{quote(str(ctx.get('packet', {}).get('slug') or ''), safe='')}"
        href += "/answers.md"
        shown = (
            '<details class="answers" data-answers><summary>Drafted answers</summary>'
            f'<pre class="q-answers">{esc(answers)}</pre>'
            f'<a class="link-quiet" href="{esc(href)}">Open answers.md</a></details>'
        )
    return ('<div class="submit-block" data-questions><div class="card-t">'
            "Application questions</div>" + form + shown + "</div>")


def _submit_before_block(ctx: dict) -> str:
    """Slot 3: the Before-you-go heading with the gate and the blocker summary, then liveness."""
    blockers = ctx.get("blockers") or []
    if blockers:
        names = ", ".join(str(b.get("title") or b.get("kind") or "") for b in blockers)
        lead = _gate(ctx).get("summary") or f"{len(blockers)} unresolved."
        blockers_line = (f'<p class="sub-warn"><strong>{esc(lead)}</strong> '
                         f"{esc(names)}. The posting link below still works if you decide "
                         "to apply anyway.</p>")
    else:
        blockers_line = '<p class="bl-calm">No unresolved blockers.</p>'
    return ('<div class="submit-block"><div class="card-t">Before you go</div>'
            + blockers_line + "</div>"
            + _submit_liveness_block(ctx))


def _submit_followup_block(ctx: dict) -> str:
    """Slot 4: a follow-up reminder date, set or cleared with plain POSTs (no JavaScript).

    The date input defaults to a week out. The Clear form only appears once a date is set,
    and both forms come back to this step through the 'return' field.
    """
    posting = ctx.get("posting") or {}
    quid = quote(str(posting.get("uid") or ""), safe="")
    back = esc(POSTING_STEP_PATH.format(quid))
    due = str(posting.get("follow_up_at") or "")
    default = (date.today() + timedelta(days=7)).isoformat()
    action = f"/api/postings/{quid}/followup"
    now_line = (f'<p class="meta">Follow up on {esc(due)}</p>' if due
                else '<p class="meta">No follow-up date set.</p>')
    set_form = (
        f'<form method="post" action="{action}" class="followup-form" data-followup-form>'
        f'<input type="hidden" name="return" value="{back}">'
        '<label class="meta" for="follow-up-date">Follow up on</label> '
        f'<input type="date" id="follow-up-date" name="date" value="{esc(default)}">'
        '<button class="btn btn-sm" type="submit">Set follow-up</button></form>')
    clear_form = ""
    if due:
        clear_form = (
            f'<form method="post" action="{action}" class="followup-clear" data-followup-clear>'
            '<input type="hidden" name="clear" value="1">'
            f'<input type="hidden" name="return" value="{back}">'
            '<button class="btn btn-sm" type="submit">Clear</button></form>')
    return ('<div class="submit-block" data-followup>'
            '<div class="card-t">Follow-up reminder</div>'
            + now_line + set_form + clear_form + "</div>")


def _submit_panel(ctx: dict) -> str:
    """The submit step: what is in the packet, what is still unresolved, and the way out.

    Fixed slots in order: files, questions, before-you-go (gate, blockers, liveness),
    follow-up reminder, footer.
    Each slot is its own helper so a later block can be inserted without touching the others.
    """
    files = (ctx.get("packet") or {}).get("files") or []
    url = (ctx.get("posting") or {}).get("url") or ""

    if url.startswith("https://") or url.startswith("http://"):
        out = (f'<a class="btn btn-primary" href="{esc(url)}" target="_blank" '
               'rel="noopener">Open real posting to submit</a>')
    else:
        out = '<span class="meta">No usable posting link.</span>'

    return (
        '<div class="panel" data-submit>'
        '<header class="panel-hd"><div class="ph-l">'
        '<h2 class="panel-t">Ready to submit</h2>'
        f'<span class="meta">{len(files)} file{"s" if len(files) != 1 else ""} in the packet'
        "</span></div></header>"
        + _submit_files_block(ctx)
        + _submit_questions_block(ctx)
        + _submit_before_block(ctx)
        + _submit_followup_block(ctx)
        + f'<div class="panel-foot">{out}</div>'
        "</div>"
    )


def _coverage_block(cov: dict | None) -> str:
    """Posting keywords on the staged resume, with the missing ones as chips.

    Renders nothing when there is no resume to compare against, so the Packet step
    stays as it was until a resume.pdf exists.
    """
    if not cov or not cov.get("available"):
        return ""
    covered = list(cov.get("covered") or [])
    missing = list(cov.get("missing") or [])
    total = len(covered) + len(missing)
    if not total:
        return ""   # the posting asked for nothing on the profile; nothing to report
    chips = "".join(f'<span class="tag tag-gap">{esc(t)}</span>' for t in missing)
    return (
        '<div class="card" data-resume-coverage>'
        f'<div class="meta">Coverage: {len(covered)} of {total} posting keywords '
        "are on this resume</div>"
        + (f'<div class="kw-row">{chips}</div>' if chips else "")
        + "</div>"
    )


def _packet_panel(ctx: dict, quid: str, busy: bool) -> str:
    """The packet step: the variant that will be built, the files, one primary action.

    The variant is the row's stored value (the scorer's pick unless someone changed
    it), so changing it here changes what the next build compiles. The change form
    is a plain POST, and the file rows keep the data-packet-* hooks the page script
    looks for after a build.
    """
    packet = ctx.get("packet") or {}
    files = packet.get("files") or []
    slug = packet.get("slug")
    names = [str(f.get("name") or "") for f in files]
    built = "resume.pdf" in names
    variant = str(ctx.get("variant") or "") or "not scored yet"
    variants = [str(v) for v in (ctx.get("variants") or [])]
    position = f"{variants.index(variant) + 1} of {len(variants)}" if variant in variants else ""
    recorded = str(packet.get("resume_variant") or "")
    uid = str((ctx.get("posting") or {}).get("uid") or "")

    options = "".join(
        f'<option value="{esc(v)}"{" selected" if v == variant else ""}>{esc(v)}</option>'
        for v in variants
    )
    build_note = ""
    if recorded and variant in variants and recorded != variant:
        build_note = (f'<div class="meta">The packet on disk was built with {esc(recorded)}; '
                      "build again to use this one.</div>")

    change = ""
    if variants:
        change = (
            '<details class="chip-menu" data-variant-menu>'
            '<summary class="chip">Change<span class="x">&#9662;</span></summary>'
            f'<form method="post" action="/api/postings/{quid}/variant" class="variant-form" '
            "data-variant-form>"
            '<label class="sr" for="ws-variant">Resume variant</label>'
            f'<select id="ws-variant" name="variant">{options}</select>'
            f'<input type="hidden" name="return" value="/posting/{esc(quote(uid, safe=""))}?step=packet">'
            '<button class="btn btn-sm" type="submit">Use this variant</button>'
            "</form></details>"
        )

    rows = []
    for f in files:
        name = str(f.get("name") or "")
        label = f.get("label") or name
        href = f"/packets/{quote(str(slug or ''), safe='')}/{quote(name, safe='')}"
        rows.append(
            f'<div class="li" data-packet-file="{esc(name)}">'
            f'<a href="{esc(href)}">{esc(label)}</a>'
            f'<span class="mono">{esc(f.get("age") or "")}</span></div>'
        )

    if files:
        files_html = '<div class="pkt-files" data-packet-list>' + "".join(rows) + "</div>"
        missing = []
        if "resume.pdf" not in names:
            missing.append("no resume yet")
        if "cover_letter.pdf" not in names:
            missing.append("no letter yet")
        if "posting.md" not in names:
            missing.append("no posting.md yet")
        if missing:
            files_html += (f'<div class="meta" data-packet-missing>'
                           f'{" &middot; ".join(missing)}</div>')
        files_html += '<div class="meta" data-packet-empty hidden></div>'
    else:
        files_html = ('<div class="pkt-files" data-packet-list></div>'
                      '<div class="meta" data-packet-empty>Nothing built yet. The build '
                      "compiles the resume variant above and copies the posting text.</div>")

    if built:
        foot = _panel_foot(ctx, "letter", "Continue to Letter")
    else:
        foot = (
            '<div class="panel-foot"><div class="prep-row">'
            f'<form method="post" action="/api/postings/{quid}/action" data-action-form>'
            '<input type="hidden" name="kind" value="prep">'
            f'<button class="btn btn-primary" type="submit"{" disabled" if busy else ""}>'
            "Build packet &middot; seconds</button></form>"
            '<span class="ai-note">takes seconds</span></div></div>'
        )

    stale_html = ""
    stale = [str(r) for r in (ctx.get("stale") or []) if str(r)]
    if stale:
        stale_html = ('<div class="card card-warn" data-stale-packet>'
                      '<div class="card-t">This packet is out of date</div>'
                      + "".join(f'<p class="meta">{esc(r)}</p>' for r in stale)
                      + '<p class="meta">Build again before you send it.</p></div>')

    return (
        '<div class="panel" data-packet>'
        '<header class="panel-hd"><div class="ph-l">'
        '<h2 class="panel-t">Packet</h2>'
        f'<span class="meta">{"built" if built else "not built yet"}</span></div></header>'
        '<div class="variant-row">'
        '<span class="vr-k">Resume variant</span>'
        f'<strong class="mono">{esc(variant)}</strong>'
        + (f'<span class="meta">{esc(position)}</span>' if position else "")
        + change + "</div>"
        + build_note
        + stale_html
        + files_html
        + _coverage_block(packet.get("coverage"))
        + foot
        + "</div>"
    )


# ---------- top bar ----------

def _topbar(ctx: dict, posting: dict, quid: str, status: str, step: str = "review") -> str:
    """Title, one meta line, the status pill and the two ways off this page.

    The status menu is a <details> so it opens without JavaScript; the form inside
    is a plain POST to /status, and the script submits it on selection when it can.
    """
    title = posting.get("title") or "posting"
    company = posting.get("company") or ""
    location = posting.get("location") or ""
    score = _score_text(posting.get("score"))
    posted = posted_phrase(posting.get("posted_at"))
    allowed = tuple(ctx.get("allowed_next") or ())
    statuses = tuple(ctx.get("statuses") or ())
    url = posting.get("url") or ""
    uid = str(posting.get("uid") or "")

    next_opts = "".join(
        f'<option value="{esc(s)}"{" selected" if s == status else ""}>{esc(s)} (next)</option>'
        for s in statuses if s in allowed
    )
    any_opts = "".join(
        f'<option value="{esc(s)}"{" selected" if s == status else ""}>{esc(s)}</option>'
        for s in statuses if s not in allowed
    )
    select = (
        '<select name="status" required aria-label="new status" id="ws-status">'
        + (f'<optgroup label="Next move">{next_opts}</optgroup>' if next_opts else "")
        + (f'<optgroup label="Any status">{any_opts}</optgroup>' if any_opts else "")
        + "</select>"
    )
    back = f"/posting/{quote(uid, safe='')}?step={quote(step, safe='')}"
    form = (
        f'<form method="post" action="/api/postings/{quid}/status" class="status-form" '
        'data-status-form>'
        '<label class="sr" for="ws-status">Status</label>'
        + select
        + '<input name="note" placeholder="note (optional)" aria-label="optional note">'
        f'<input type="hidden" name="return" value="{esc(back)}">'
        '<button class="btn btn-sm" type="submit">Apply</button>'
        "</form>"
    )

    meta = [p for p in (esc(company), esc(location)) if p]
    meta.append(f'<span class="mono">score {esc(score)}</span>')
    if posted:
        meta.append(esc(posted))
    meta_line = " &middot; ".join(meta)

    orig = ""
    if url.startswith("https://") or url.startswith("http://"):
        orig = (f'<a class="orig" href="{esc(url)}" target="_blank" rel="noopener">'
                "Open original</a>")

    return (
        '<header class="topbar">'
        f'<a class="tb-back" href="{esc(ctx.get("back_url") or "/")}">&lt;- Inbox</a>'
        '<div class="tb-id">'
        f'<div class="tb-title">{esc(title)}</div>'
        f'<div class="tb-meta">{meta_line}</div>'
        "</div>"
        '<div class="tb-actions">'
        f"{orig}"
        '<details class="status-menu" data-status-menu>'
        '<summary class="status-pill" title="Change status">'
        '<span class="sp-k">status</span>'
        f'<strong class="mono" id="status-text">{esc(status or "none")}</strong>'
        '<span class="caret" aria-hidden="true">&#9662;</span>'
        "</summary>"
        f"{form}"
        "</details>"
        "</div>"
        "</header>"
        + _tabs(ctx, step)
    )


# ---------- left column ----------

def _gaps_card(ctx: dict, quid: str, kinds: dict) -> str:
    """Legacy gap-analysis card for old packets. Never shown alongside the analysis card."""
    text = ctx.get("gaps_text")
    if ctx.get("analysis_text") or not text:
        return ""
    return card("Find gaps", f'<pre class="pre">{esc(text)}</pre>', card_id="gaps")


# ---------- centre column ----------

def _letter_panel(ctx: dict, quid: str, busy: bool, latest: dict | None) -> str:
    """The letter step. Two ways in with no letter; the editor alone once there is one."""
    body = ctx.get("body_text")
    words = int(ctx.get("word_count") or 0)
    head = (
        '<header class="panel-hd"><div class="ph-l">'
        '<h2 class="panel-t">Cover letter</h2>'
        + (f'<span class="meta"><span class="mono" data-word-count>{words}</span> words '
           "&middot; target 240-320</span>" if body is not None else "")
        + "</div></header>"
    )

    if body is None:
        draft = _ai_form(quid, "letter", busy, "Draft with AI · 2-3 min",
                         gated=_is_out_of_reach(ctx), note="", primary=True)
        return "".join([
            '<div class="panel" data-letter>', head,
            '<div class="empty-state">',
            '<p class="es-t">No letter yet.</p>',
            '<p class="meta">Draft one with the model, or write your own. Both land in the '
            "same file, and nothing is sent from here.</p>",
            '<div class="es-actions">' + draft + "</div>",
            '<details class="own-letter"><summary class="btn">Write or paste your own'
            "</summary>" + _body_form(ctx, quid, body, words) + "</details>",
            "</div>",
            _queue_strip(latest),
            "</div>",
        ])

    redraft = ('<div class="letter-actions">'
               + _ai_form(quid, "letter", busy, "Redraft with AI",
                          gated=_is_out_of_reach(ctx))
               + '<span class="ai-note">two to three minutes; it replaces the text below</span>'
               "</div>")
    return "".join([
        '<div class="panel" data-letter>', head,
        _body_form(ctx, quid, body, words),
        redraft,
        _queue_strip(latest),
        "</div>",
    ])


def _body_form(ctx: dict, quid: str, body: str | None, words: int) -> str:
    """The editor: sized to its text, with the word count and one primary action."""
    text = body or ""
    lines = len(text.splitlines()) if text else 0
    rows = max(12, min(40, lines + 1))
    disabled = "" if text.strip() else ' disabled aria-disabled="true"'
    return (
        f'<form method="post" action="/api/postings/{quid}/body" class="body-form" '
        "data-body-form>"
        '<label class="sr" for="ws-body">Cover letter body</label>'
        f'<textarea id="ws-body" class="ta grow" name="body" rows="{rows}" data-body '
        'placeholder="Paste or type the letter body. Plain text, no markdown.">'
        + esc(text) + "</textarea>"
        '<div class="ta-bar">'
        f'<span class="meta"><span class="mono" data-word-count>{words}</span> words</span>'
        '<span class="meta">target 240-320</span>'
        '<span class="grow"></span>'
        '<button class="btn btn-sm" type="submit" data-save-body>Save text</button>'
        f'<button class="btn btn-primary" type="submit" data-recompile{disabled}>'
        "Recompile PDF</button>"
        '<span class="ai-note">Recompiles locally in about 2s</span>'
        "</div></form>"
    )


def _review_panel(ctx: dict, quid: str, busy: bool, latest: dict | None) -> str:
    """The review step: findings parsed out of analysis.md, never a raw dump.

    The parse is best effort. Anything the parser cannot shape into findings falls
    back to the whole file in a collapsible block, so a new model output shape costs
    a fold-out, not a broken step.
    """
    analysis = ctx.get("analysis_text") or ""
    parsed = parse_analysis(analysis)
    parts = ['<div class="panel" data-review>']

    if not analysis:
        legacy = ""
        if ctx.get("critique_text") or ctx.get("gaps_text"):
            blobs = ""
            if ctx.get("critique_text"):
                blobs += f'<div class="meta">Critique (older run)</div><pre class="pre">{esc(ctx["critique_text"])}</pre>'
            if ctx.get("gaps_text"):
                blobs += f'<div class="meta">Find gaps (older run)</div><pre class="pre">{esc(ctx["gaps_text"])}</pre>'
            legacy = (f'<details class="raw-analysis"><summary>Older run output</summary>'
                      f"{blobs}</details>")
        parts.append(
            '<div class="empty-state">'
            '<p class="es-t">No fit analysis yet.</p>'
            '<p class="meta">One call, two to three minutes. It queues behind any running job.</p>'
            + _analysis_form(ctx, quid, busy, label="Analyze fit", primary=True)
            + legacy
            + "</div>"
        )
        parts.append(_queue_strip(latest))
        parts.append(_panel_foot(ctx, "packet", "Continue to Packet", primary=False))
        return "".join(parts) + "</div>"

    when = rel_phrase(ctx.get("analysis_when") or "")
    parts.append(
        '<header class="panel-hd"><div class="ph-l">'
        '<h2 class="panel-t">Fit analysis</h2>'
        f'<span class="meta">{("finished " + esc(when)) if when else "finished earlier"}</span>'
        '</div><div class="ph-r">'
        + _analysis_form(ctx, quid, busy, label="Re-run", primary=False)
        + "</div></header>"
    )
    parts.append(_queue_strip(latest))

    if parsed["ok"]:
        parts.append('<div class="findings">'
                     + "".join(_finding_card(f) for f in parsed["findings"]) + "</div>")
        perfect = parsed.get("perfect")
        if perfect:
            est = str(perfect.get("estimate") or "").strip()
            summary = ('<span class="card-t">To be perfect</span>'
                       + (f'<span class="meta">{esc(est)}</span>' if est else ""))
            body = "".join(f"<p>{esc(p)}</p>" for p in _paragraphs(perfect.get("text") or ""))
            parts.append(f'<details class="card perfect"><summary>{summary}</summary>'
                         f'<div class="perfect-body">{body}</div></details>')
    else:
        parts.append(
            '<details class="raw-analysis"><summary>Raw analysis '
            "(could not be split into findings)</summary>"
            f'<pre class="pre">{esc(analysis)}</pre></details>'
        )

    parts.append(_panel_foot(ctx, "packet", "Continue to Packet", primary=True))
    return "".join(parts) + "</div>"


def _paragraphs(text: str) -> list[str]:
    return [" ".join(p.split()) for p in (text or "").split("\n\n") if p.strip()]


_FINDING_TAGS = {"gap": ("Gap", "tag-gap"), "strength": ("Strength", "tag-str"),
                 "decide": ("Decide", "tag-dec")}


def _finding_card(finding: dict) -> str:
    """One finding: a type tag, a title, and the posting/facts contrast when present."""
    kind = str(finding.get("type") or "gap")
    label, cls = _FINDING_TAGS.get(kind, ("Gap", "tag-gap"))
    title = finding.get("title") or label
    rows = ""
    if finding.get("posting"):
        rows += (f'<div class="f-row"><span class="fk">Posting says</span>'
                 f'<span class="fv">{esc(finding["posting"])}</span></div>')
    if finding.get("facts"):
        rows += (f'<div class="f-row"><span class="fk">Your facts say</span>'
                 f'<span class="fv">{esc(finding["facts"])}</span></div>')
    body = (f'<div class="f-rows">{rows}</div>' if rows
            else f'<p class="f-body">{esc(finding.get("body") or "")}</p>')
    return (f'<article class="finding f-{esc(kind)}">'
            f'<div class="f-hd"><span class="tag {cls}">{esc(label)}</span>'
            f'<h3 class="f-title">{esc(title)}</h3></div>{body}</article>')


def _panel_foot(ctx: dict, next_step: str, label: str, primary: bool = True) -> str:
    """The one primary action of a step: a real link, so it works without JS."""
    cls = "btn btn-primary" if primary else "btn"
    return (f'<div class="panel-foot"><a class="{cls}" href="{esc(step_href(next_step, ctx))}">'
            f"{esc(label)}</a></div>")


def _verdict(reach: dict) -> str:
    return reach.get("verdict") or ("in_reach" if reach.get("in_reach") else "")


GATED_NOTE = "out of reach: the call is gated, click once and confirm"


def _is_out_of_reach(ctx: dict) -> bool:
    return _verdict(ctx.get("reach") or {}) == "out_of_reach"


def _analysis_form(ctx: dict, quid: str, busy: bool, label: str | None = None,
                   primary: bool = True) -> str:
    """The single Analyze fit action. It never carries force=1: an out-of-reach posting is
    gated by the server (409), and the override is the second, deliberate click the page
    builds from the 409 reply (POSTING_JS "Run anyway"). Without JS the form posts unforced
    and the server answers 409 HTML with its own Run anyway form."""
    gate = '<span class="ai-note">two to three minutes</span>'
    if _is_out_of_reach(ctx):
        gate = f'<span class="muted">{GATED_NOTE}</span>'
    # A posting with analysis.md already has the analysis: the click spends a second call.
    if label is None:
        label = "Re-run analysis" if ctx.get("analysis_text") else None
    return (
        f'<form method="post" action="/api/postings/{quid}/action" data-action-form>'
        '<input type="hidden" name="kind" value="analysis">'
        f"{_ai_button('analysis', busy, label, primary=primary)}{gate}</form>"
    )


def _ai_form(quid: str, kind: str, busy: bool, label: str | None = None,
             gated: bool = False, note: str = "two to three minutes",
             primary: bool = False) -> str:
    """A paid AI action form. gated marks an out-of-reach posting: same unforced post, gated note.

    `note` is suppressed by passing an empty string when the button label already
    carries the estimate, so the same timing is not printed twice.
    """
    if gated:
        note_html = f'<span class="muted">{GATED_NOTE}</span>'
    else:
        note_html = f'<span class="ai-note">{esc(note)}</span>' if note else ""
    return (
        f'<form method="post" action="/api/postings/{quid}/action" data-action-form>'
        f'<input type="hidden" name="kind" value="{esc(kind)}">'
        f'{_ai_button(kind, busy, label, primary=primary)}{note_html}</form>'
    )


def _ai_button(kind: str, busy: object, label: str | None = None, primary: bool = False) -> str:
    text = label or _KIND_LABELS[kind][0]
    dis = " disabled" if busy else ""
    return btn(text, primary=primary, small=not primary, attrs=dis.strip())


def _queue_strip(latest: dict | None) -> str:
    """Where the latest AI job stands: queued, running with its stage, or the last result."""
    hidden = "" if latest else " hidden"
    main = note = ""
    if latest:
        kind = latest.get("kind") or "job"
        state = latest.get("state") or ""
        if state in ("queued", "running"):
            if state == "queued":
                pos = latest.get("position")
                main = f"{kind} queued #{pos}" if pos else f"{kind} queued"
            else:
                main = f"{kind} running: {_stage_label(latest.get('stage'), kind)}"
            elapsed = _secs_since(latest.get("started") or latest.get("created"))
            est = latest.get("est_seconds")
            eta = latest.get("eta_s")
            parts = []
            if elapsed is not None:
                parts.append(f"{elapsed // 60} min elapsed")
            left = None
            if eta is not None:
                left = int(eta)  # server-computed seconds remaining
            elif est:
                left = int(est) - (elapsed or 0)
            if left is not None:
                parts.append(f"about {max(0, -(-left // 60))} min left")
            note = ", ".join(parts)
        elif latest.get("blocker") == "french":
            main = f"{kind} rejected: the posting asks for French"
            note = latest.get("error") or ""
        else:
            main = f"last {kind} {state}"
            note = latest.get("error") or latest.get("message") or ""
    return (
        f'<div class="queue-strip" data-queue-strip role="status"{hidden}>'
        f'<span class="q-main">{esc(main)}</span> '
        f'<span class="q-note">{esc(note)}</span></div>'
    )


def _stage_label(stage: object, kind: object = None) -> str:
    """Plain words for the dispatcher's stage names. Unknown names stay visible.

    Empty reads "starting", except a prep job: it runs the local CLI and never prints a stage,
    so its whole run reads "building the packet".
    """
    name = str(stage or "").strip()
    if not name:
        return "building the packet" if str(kind or "") == "prep" else "starting"  # copilot.PREP_KIND
    return STAGE_LABELS.get(name, name)


def _secs_since(iso: object) -> int | None:
    if not iso:
        return None
    try:
        when = datetime.fromisoformat(str(iso))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0, int((datetime.now(timezone.utc) - when).total_seconds()))


def _when(kinds: dict, kind: str) -> str:
    entry = kinds.get(kind) or {}
    return entry.get("when") or "two to three minutes"


# ---------- right column ----------

def _checks_card(ctx: dict) -> str:
    """The live letter checks. The page script repaints the rows from POST /checks."""
    checks = ctx.get("letter_checks") or []
    rows = ('<div class="checks" data-letter-checks>'
            + "".join(_check_row(c) for c in checks) + "</div>")
    return card("Letter checks", rows, card_id="checks")


def _check_row(check: dict) -> str:
    ok = bool(check.get("ok"))
    cls = "check ok" if ok else "check warn"
    mark = "&#10003;" if ok else "FAIL"
    detail = check.get("detail") or ""
    return (
        f'<div class="{cls}"><span class="mk">{mark}</span><span>'
        f'{esc(check.get("label") or "")}'
        + (f' <span class="muted">{esc(detail)}</span>' if detail else "")
        + "</span></div>"
    )


ACTIVITY_SHOWN = 6  # the rail is a glance, not an audit log


def _activity_card(events: list[dict]) -> str:
    """The last few events, newest first. Empty string when there is nothing to show."""
    if not events:
        return ""
    shown = list(reversed(events))[:ACTIVITY_SHOWN]  # store events arrive oldest first
    items = "".join(
        '<li class="li">'
        f'<time class="mono">{esc(_ts(e.get("ts")))}</time>'
        f'<span><span class="k">{esc(e.get("kind") or "")}</span> '
        f'{esc(e.get("detail") or "")}</span></li>'
        for e in shown
    )
    more = ""
    if len(events) > ACTIVITY_SHOWN:
        more = (f'<div class="meta act-more">{len(events) - ACTIVITY_SHOWN} older '
                "events in the store</div>")
    return card("Activity", f'<ul class="timeline">{items}</ul>{more}', card_id="activity")


# ---------- bottom bar ----------

def _bottombar(ctx: dict, quid: str, status: str) -> str:
    parts = ['<footer class="bottombar">',
             '<span class="say">Nothing is sent from here. Every file is a draft you can edit.</span>']
    if status not in _APPLIED_OR_LATER:
        back = esc(ctx.get("return_url") or ctx.get("back_url") or "/")
        parts.append(
            f'<form method="post" action="/api/postings/{quid}/status" data-status-form '
            'data-advance>'
            '<input type="hidden" name="status" value="skipped">'
            '<input type="hidden" name="note" value="">'
            f'<input type="hidden" name="return" value="{back}">'
            '<button class="btn btn-sm" type="submit">Skip</button></form>'
        )
        parts.append(
            f'<form method="post" action="/api/postings/{quid}/status" data-status-form '
            'data-advance>'
            '<input type="hidden" name="status" value="applied">'
            '<input type="hidden" name="note" value="">'
            f'<input type="hidden" name="return" value="{back}">'
            '<button class="btn btn-sm btn-primary" type="submit">Mark as applied</button>'
            "</form>"
        )
    parts.append("</footer>")
    return "".join(parts)


# ---------- helpers ----------

def _score_text(score: object) -> str:
    if score is None or score == "":
        return "—"
    try:
        return f"{float(score):g}"
    except (TypeError, ValueError):
        return str(score)


def _ts(iso: object) -> str:
    text = "" if iso is None else str(iso)
    try:
        return datetime.fromisoformat(text).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return text[:16]
