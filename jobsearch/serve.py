"""Local web server for the jobsearch dashboard, packets and triage UI (stdlib only).

Serves:
  GET /                              the triage screen (sidebar, list, preview), live from the store
  GET /dashboard                     the older static dashboard.html (still written by the CLI)
  GET /healthz                       JSON: ok, dashboard_mtime, age_s
  GET /packets/                      HTML index of packet dirs and their files
  GET /packets/PACKET_DIR/FILENAME   one allowed file from a packet dir
  GET /api/postings/UID              JSON for one posting, its packet and events
  GET /preview/UID                   the preview pane fragment the triage screen fetches
  GET /posting/UID                   the posting workspace: text, letter editor, checks, activity
  GET /api/scan                      JSON: whether scanning is enabled and the current job
  POST /api/postings/UID/status      set status (form or JSON); 303 or JSON reply
  POST /api/postings/UID/action      queue a job, kind=prep|analysis|letter (critique and gaps
                                     queue analysis); an out-of-reach AI kind is 409 unless
                                     force=1 (prep runs the no-AI packet build);
                                     303 to the posting page for a browser form, else JSON
  POST /api/postings/UID/body        recompile a hand-edited letter body (urlencoded field
                                     body) locally, no model; 303 for a browser form, else
                                     JSON {ok, path}; 400 on empty or non-ASCII text
  POST /api/scan                     start a scan; only when JOBSEARCH_SERVE_SCAN=1
  GET /api/jobs                      JSON: ok, jobs (newest 20, with company, title, stage_label, kind_label, elapsed_s, eta_s, position),
                                     running (job or null), queued (count)

Every request needs HTTP Basic auth. The server refuses to start unless both
JOBSEARCH_SERVE_USER and JOBSEARCH_SERVE_PASSWORD are set and non-empty, because
it may be bound to a LAN address. The writes are the status POST (through Store.set_status)
and the action POST (queues a copilot job; nothing is sent or submitted). Both are
refused (403) when a cross-origin Origin header is sent. PUT, DELETE and PATCH return 405,
and a path the server does not serve returns 404. The server reaches the network in three places, all of them on this machine: the
liveness POST probes one posting URL, the copilot worker it starts runs the local model
CLI, and the scan POST (off unless JOBSEARCH_SERVE_SCAN=1) runs the CLI scan as a child
process.

Run: python -m jobsearch.serve [--host HOST] [--port PORT] [--db PATH]
"""

from __future__ import annotations

import argparse
import base64
import difflib
import binascii
import html
import hmac
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit

from jobsearch import cli, copilot, reach, sources, staleness, triage, ui, workspace
from jobsearch.analysis_findings import extract_verdict_level
from jobsearch.checks import (apply_gate, french_quote, highlight, looks_french,
                              matched_skills, resume_coverage, seniority_flag)
from jobsearch.copilot import ACTION_KINDS, PREP_KIND, ArtifactError
from jobsearch.copilot import KINDS as AI_KINDS
from jobsearch.cli import VARIANTS
from jobsearch.copilot import QUESTIONS_MAX_CHARS, save_questions
from jobsearch.copilot import Copilot, validate as validate_answer
from jobsearch.copilot import _pdf_text as _copilot_pdf_text
from jobsearch.dashboard import ALLOWED_NEXT, _status_form, render_dashboard
from jobsearch import match as _match
from jobsearch.match import SCORE_SCALE, load_profile, score_parts
from jobsearch.letter import contact_location
from jobsearch.store import STATUSES, Store, default_db_path

logger = logging.getLogger("jobsearch.serve")

DEFAULT_HOST = "127.0.0.1"  # loopback; pass --host to bind a LAN address
DEFAULT_PORT = 8770
EX_CONFIG = 78  # sysexits.h: configuration error

USER_ENV = "JOBSEARCH_SERVE_USER"
PASSWORD_ENV = "JOBSEARCH_SERVE_PASSWORD"
REALM = "jobsearch"

HOME_JOBSEARCH = Path.home() / "jobsearch"
REPO_ROOT = Path(__file__).resolve().parents[1]  # the checkout the scan child runs in
DASHBOARD_PATH = HOME_JOBSEARCH / "dashboard.html"
PACKETS_ROOT = HOME_JOBSEARCH / "packets"

# The only files a packet directory may expose, with their Content-Types.
ALLOWED_FILES = {
    "resume.pdf": "application/pdf",
    "cover_letter.pdf": "application/pdf",
    "cover_letter.tex": "text/x-tex",
    "cover_letter_body.txt": "text/plain; charset=utf-8",
    "posting.md": "text/plain; charset=utf-8",
    "critique.md": "text/plain; charset=utf-8",
    "gaps.md": "text/plain; charset=utf-8",
    "analysis.md": "text/plain; charset=utf-8",
    "answers.md": "text/plain; charset=utf-8",
}

# One path segment naming a packet dir: no slashes, no leading dot (so no "." or "..").
_DIR_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

PACKETS_PREFIX = "/packets/"
API_PREFIX = "/api/postings/"
POSTING_PREFIX = "/posting/"
MAX_BODY = 64 * 1024  # bytes accepted for a status POST
FOLLOW_UP_MAX_DAYS = 120
_FOLLOW_UP_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
MAX_NOTE = 2000  # characters accepted for a status note
PACKET_FILE_RESUME = "resume.pdf"
PACKET_FILE_LETTER = "cover_letter.pdf"
LAST_SCAN_NAME = "last_scan.json"  # sits beside dashboard.html


def packet_dir_name(uid: str) -> str:
    """Directory name for a posting's packet: every '/' and ':' becomes '_'."""
    return triage.packet_dir_name(uid)


POSTING_CSS = (
    "*{box-sizing:border-box}"
    "body{margin:0;background:#181c25;color:#e6e8ee;padding:14px;max-width:760px;"
    "font:16px/1.45 system-ui,-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;"
    "overflow-wrap:anywhere}"
    "a{color:#7dd3fc}"
    "h1{font-size:1.5rem;margin:0 0 6px}"
    "h2{font-size:1.1rem;margin:26px 0 10px;padding-bottom:4px;border-bottom:1px solid #2a2f3a}"
    ".meta{color:#9aa3b2;font-size:.9rem;margin:4px 0}"
    ".card{background:#181c25;border:1px solid #2a2f3a;border-radius:10px;padding:12px;margin:10px 0}"
    ".sform{display:flex;flex-wrap:wrap;gap:6px;margin:8px 0}"
    ".sform select,.sform input,.sform button{font:inherit;font-size:.9rem;min-height:36px;"
    "padding:4px 8px;min-width:0;border:1px solid #2a2f3a;border-radius:6px;"
    "background:#181c25;color:#e6e8ee}"
    ".timeline{list-style:none;padding:0;margin:0}"
    ".timeline li{border-left:2px solid #2a2f3a;padding:4px 0 4px 10px;margin:0 0 8px}"
    ".ai-text{white-space:pre-wrap;font-size:.9rem;background:#12151c;padding:8px;"
    "border-radius:6px;margin:6px 0}"
    ".prep-row{border-bottom:1px solid #2a2f3a;padding-bottom:10px;margin-bottom:10px}"
    ".body-edit{display:block;width:100%;min-height:220px;font:inherit;font-size:.92rem;"
    "background:#12151c;color:#e6e8ee;border:1px solid #2a2f3a;border-radius:6px;"
    "padding:8px;margin:0 0 8px;box-sizing:border-box}"
    ".body-count.ok{color:#8fd19e}.body-count.warn{color:#e0b060}"
    ".queue-strip.q-err{color:#fca5a5}"
    ".job-err{color:#fca5a5}"
    "button:disabled{opacity:.5;cursor:not-allowed}"
)
AI_LABELS = {"analysis": "Analyze fit", "letter": "Draft cover letter", "critique": "Critique fit",
             "gaps": "Find gaps", "questions": "Draft answers", "facts_update": "Update facts/preferences"}  # legacy labels stay
ANALYZE_KIND = "analysis"
JOBS_PATH = "/api/jobs"
JOBS_LIMIT = 20  # newest jobs returned by GET /api/jobs
FORCE_VALUES = ("1", "on", "true")  # truthy values of the action POST's force field
STAGE_PHRASES = {
    "device": "starting the device", "model": "loading the model", "navigate": "opening the app",
    "composer": "opening the composer", "focus": "focusing the composer",
    "inject": "injecting the bundle", "send": "sending the prompt", "await": "waiting on the app",
    "trigger": "triggering the reply", "submit": "committing the task",
    "context": "reading the context", "relaunch retry": "relaunching the app",
    "start": "starting the run",
}
PREP_LABEL = "Build packet"
REFRESH_META = '<meta http-equiv="refresh" content="15">'
# Only for the no-JS case: inside <noscript> a browser with scripting on never acts on it.
NOSCRIPT_REFRESH = f"<noscript>{REFRESH_META}</noscript>"

PREVIEW_PREFIX = "/preview/"
SCAN_PATH = "/api/scan"
SCAN_ENV = "JOBSEARCH_SERVE_SCAN"
SCAN_TIMEOUT_S = 1800
NO_STORE = {"Cache-Control": "no-store"}


class ScanJob:
    """At most one scan at a time, run as a child process.

    The liveness POST already reaches the network on request, so a full scan is opt-in:
    with JOBSEARCH_SERVE_SCAN unset the start is refused and the button is disabled.
    A scan runs `python -m jobsearch.cli scan` in the repo, writes the same store the
    requests read, and its state lives in memory only - a restart starts idle.
    """

    def __init__(self, cwd: Path, db_path: str):
        self.cwd = Path(cwd)
        self.db_path = db_path
        self._lock = threading.Lock()
        self._state = "idle"
        self._started: float | None = None
        self._finished: float | None = None
        self._error = ""
        self._proc: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None

    @property
    def enabled(self) -> bool:
        return os.environ.get(SCAN_ENV, "").strip() not in ("", "0", "false", "no")

    def status(self) -> dict:
        with self._lock:
            return {
                "state": self._state,
                "enabled": self.enabled,
                "elapsed_s": (int(time.time() - self._started) if self._started else 0),
                "error": self._error,
            }

    def start(self) -> tuple[bool, str]:
        """Begin a scan. Returns (started, reason)."""
        if not self.enabled:
            return False, f"scanning is disabled; set {SCAN_ENV}=1 to allow it"
        with self._lock:
            if self._state in ("queued", "running"):
                return False, "a scan is already running"
            self._state = "queued"
            self._error = ""
            self._started = time.time()
            self._finished = None
        self._thread = threading.Thread(target=self._run, name="jobsearch-scan", daemon=True)
        self._thread.start()
        return True, ""

    def _run(self) -> None:
        cmd = [sys.executable, "-m", "jobsearch.cli", "scan", "--db", str(self.db_path)]
        try:
            self._proc = subprocess.Popen(cmd, cwd=str(self.cwd), stdout=subprocess.PIPE,
                                          stderr=subprocess.STDOUT, text=True)
            with self._lock:
                self._state = "running"
            out, _ = self._proc.communicate(timeout=SCAN_TIMEOUT_S)
            rc = self._proc.returncode
            if rc == 0:
                self._finish("done", "")
            else:
                tail = (out or "").strip().splitlines()[-3:]
                self._finish("failed", f"scan exited {rc}: {' / '.join(tail) or 'no output'}")
        except subprocess.TimeoutExpired:
            if self._proc is not None:
                self._proc.kill()
            self._finish("failed", f"scan ran longer than {SCAN_TIMEOUT_S // 60} minutes and was stopped")
        except Exception as exc:  # pragma: no cover - a missing interpreter or cwd
            self._finish("failed", f"could not start the scan: {exc}")

    def _finish(self, state: str, error: str) -> None:
        with self._lock:
            self._state = state
            self._error = error
            self._finished = time.time()
        if state == "done":  # new postings from the scan need their reach levels
            _backfill_levels(self.db_path)


def _fresh(iso: str | None, limit_hours: float = 36.0) -> bool:
    """Whether a timestamp is recent enough for the sidebar dot to stay calm."""
    if not iso:
        return False
    try:
        when = datetime.fromisoformat(str(iso))
    except ValueError:
        return False
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - when).total_seconds() <= limit_hours * 3600


def _ics_text(value: object) -> str:
    """Escape a TEXT value per RFC 5545 section 3.3.11."""
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
    return text.replace("\n", "\\n")


def _ics_fold(line: str) -> list[str]:
    """Split a content line into parts of at most 75 octets (RFC 5545 section 3.1)."""
    parts: list[str] = []
    cur, size = "", 0
    for ch in line:
        width = len(ch.encode("utf-8"))
        if cur and size + width > (75 if not parts else 74):
            parts.append(cur)
            cur, size = "", 0
        cur += ch
        size += width
    parts.append(cur)
    return parts


def _render_followups_ics(rows: list[dict], now: datetime) -> str:
    """One all-day VEVENT per row, CRLF line endings, ending with END:VCALENDAR."""
    stamp = now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//jobsearch//follow-ups//EN",
             "CALSCALE:GREGORIAN"]
    for row in rows:
        day = str(row.get("follow_up_at") or "").replace("-", "")
        if len(day) != 8 or not day.isdigit():
            continue
        summary = f"Follow up: {row.get('company') or ''} - {row.get('title') or ''}"
        lines += ["BEGIN:VEVENT", f"UID:{_ics_text(row.get('uid'))}", f"DTSTAMP:{stamp}",
                  f"DTSTART;VALUE=DATE:{day}", f"SUMMARY:{_ics_text(summary)}"]
        url = str(row.get("url") or "").replace("\r", "").replace("\n", "")
        if url.startswith(("https://", "http://")):
            lines.append(f"URL:{url}")
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    physical: list[str] = []
    for line in lines:
        first, *rest = _ics_fold(line)
        physical.append(first)
        physical.extend(" " + part for part in rest)
    return "\r\n".join(physical) + "\r\n"


_STATS_HEAD = (
    "<style>.stats-tbl{width:100%;border-collapse:collapse;font-size:13px}"
    ".stats-tbl th,.stats-tbl td{text-align:left;padding:6px 8px;"
    "border-bottom:1px solid rgba(128,128,128,.3)}"
    ".stats-tbl td.num{text-align:right;font-variant-numeric:tabular-nums}"
    ".stats-page{max-width:880px;margin:0 auto;padding:16px}</style>")


def _stats_table(headers: list[str], rows: list[list[str]]) -> str:
    """A plain table, or the one-line 'nothing yet' when there are no rows."""
    if not rows:
        return '<p class="meta">nothing yet</p>'
    head = "".join(f'<th scope="col">{ui.esc(h)}</th>' for h in headers)
    body = "".join(
        "<tr>" + "".join(
            f'<td class="num">{ui.esc(c)}</td>' if i else f"<td>{ui.esc(c)}</td>"
            for i, c in enumerate(row)) + "</tr>"
        for row in rows)
    return ('<table class="stats-tbl"><thead><tr>' + head + "</tr></thead><tbody>"
            + body + "</tbody></table>")


def _render_stats(funnel: dict, variants: list[dict], sessions: dict) -> str:
    """The /stats page: funnel by status, variant performance, sessions per kind."""
    statuses = list(STATUSES) + [s for s in funnel if s not in STATUSES]
    total = sum(funnel.values())
    funnel_rows = [[s, str(funnel.get(s, 0))] for s in statuses] if total else []
    variant_rows = [[v["variant"] or "none", str(v["total"]), str(v["applied"]),
                     str(v["responded"])] for v in variants]
    session_rows = [[k, str(n)] for k, n in sorted(sessions.items())]
    body = (
        '<main class="stats-page"><h1>Stats</h1>'
        + ui.card("Funnel by status", _stats_table(["Status", "Postings"], funnel_rows))
        + ui.card("Variant performance", _stats_table(
            ["Variant", "Total", "Applied", "Responded"], variant_rows))
        + ui.card("Sessions per kind", _stats_table(["Kind", "Sessions"], session_rows))
        + '<p class="meta"><a href="/">Triage</a> · '
        '<a href="/stats.ics">Follow-ups (.ics)</a></p></main>')
    return ui.page("Stats", body, head=_STATS_HEAD)


FACTS_PATH = "/facts"
FACTS_API = "/api/facts/"  # POST /api/facts/propose, /api/facts/<jid>/accept, /api/facts/<jid>/reject
FACTS_INSTRUCTION_LIMIT = copilot.FACTS_INSTRUCTION_MAX  # one cap, shared with copilot.submit
FACTS_HISTORY_MAX = 20
# Fallback location when the profile names none; the location tiers are city based.
DEFAULT_CITY = "Winnipeg, MB"

FACTS_BUSY = ("queued", "running")
_FACTS_HEAD = (
    "<style>.facts-pre{white-space:pre-wrap;word-break:break-word;overflow-x:auto;font-size:12.5px;"
    "margin:0;border:1px solid rgba(128,128,128,.35);border-radius:6px;padding:8px}"
    ".facts-ta{display:block;width:100%;min-height:96px;font:inherit;box-sizing:border-box;padding:8px}"
    ".facts-card{margin-top:12px}.facts-card summary{cursor:pointer}"
    ".facts-act{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin:8px 0}"
    ".facts-tbl{width:100%;border-collapse:collapse;font-size:13px}"
    ".facts-tbl th,.facts-tbl td{text-align:left;padding:6px 8px;vertical-align:top;"
    "border-bottom:1px solid rgba(128,128,128,.3)}</style>"
)


def _facts_outcome(job: dict) -> str:
    state = job.get("state") or ""
    if state == "pending_review":
        return "Waiting for your review"
    if state == "applied":
        return "Applied"
    if state == "rejected":
        return "Rejected; nothing written"
    if state == "failed":
        return "Failed: " + (job.get("error") or "no detail")
    if state == "done":
        bits = [str(job.get("proposal_state") or "done")]
        if job.get("question"):
            bits.append(f"question: {job['question']}")
        if job.get("reason"):
            bits.append(f"reason: {job['reason']}")
        return "; ".join(bits)
    return {"queued": "Queued", "running": "Working", "applying": "Applying"}.get(state, state)


def _facts_current_html(job: dict | None, now: float) -> str:
    if job is None:
        return "<p>No change has been proposed yet.</p>"
    state = job.get("state") or ""
    target = ui.esc(job.get("target") or "")
    if state in FACTS_BUSY:
        created = _epoch(job.get("created"))
        secs = max(0, int(now - created)) if created is not None else 0
        return (f"<p><strong>Working...</strong> ({state}, {secs} seconds elapsed). "
                f'<a href="{FACTS_PATH}">Refresh now</a> to check progress.</p>')
    if state == "pending_review":
        jidq = quote(str(job.get("id") or ""), safe="")
        base = ui.esc(job.get("base_sha256") or "")
        return (
            f"<p>{ui.esc(job.get('summary') or '')}</p>"
            f"<p>Target file: <code>{target}</code></p>"
            f'<pre class="facts-pre">{ui.esc(job.get("diff") or "")}</pre>'
            f'<form method="post" action="/api/facts/{ui.esc(jidq)}/accept" class="facts-act">'
            f'<input type="hidden" name="seen_sha256" value="{base}">'
            '<button class="btn btn-primary" type="submit">Accept</button>'
            f"<span>writes <code>{target}</code></span></form>"
            f'<form method="post" action="/api/facts/{ui.esc(jidq)}/reject" class="facts-act">'
            '<button class="btn" type="submit">Reject</button>'
            f"<span>leaves <code>{target}</code> unchanged</span></form>"
        )
    if state == "applied":
        sha = job.get("commit_sha") or ""
        return (f"<p>Applied to <code>{target}</code>"
                + (f" in commit <code>{ui.esc(sha[:7])}</code>" if sha else "") + ".</p>")
    return f"<p>{ui.esc(_facts_outcome(job))}</p>"


def _facts_file_html(name: str, path: Path | None) -> str:
    if path is None:
        text = "<p>Not available: the AI copilot is not running on this server.</p>"
    elif not path.exists():
        text = f"<p>{ui.esc(name)} does not exist yet.</p>"
    else:
        try:
            text = f'<pre class="facts-pre">{ui.esc(path.read_text(encoding="utf-8"))}</pre>'
        except (OSError, UnicodeDecodeError):
            text = f"<p>{ui.esc(name)} could not be read.</p>"
    return f'<details class="facts-card"><summary>{ui.esc(name)}</summary>{text}</details>'


def _render_facts(cop: "Copilot | None", now: float) -> str:
    """The /facts page: request form, current proposal, the two files, recent history."""
    jobs = ([j for j in cop.jobs() if j.get("kind") == copilot.FACTS_KIND]
            if cop is not None else [])
    current = jobs[-1] if jobs else None
    busy = current is not None and current.get("state") in FACTS_BUSY
    disabled = " disabled" if busy else ""
    form = (
        '<form method="post" action="/api/facts/propose" class="facts-form">'
        '<label for="facts-instruction">Tell the assistant what changed</label>'
        '<textarea id="facts-instruction" class="facts-ta" name="instruction" '
        f'maxlength="{FACTS_INSTRUCTION_LIMIT}" '
        'placeholder="the cities you would relocate to, and the countries you need a work permit '
        'for." required' + disabled + "></textarea>"
        f'<input type="hidden" name="return" value="{FACTS_PATH}">'
        f'<p><button class="btn btn-primary" type="submit"{disabled}>Propose change</button></p>'
        "</form>"
    )
    if busy:
        form += "<p>A change is already being worked on; the form reopens when it settles.</p>"
    history = [j for j in reversed(jobs)][:FACTS_HISTORY_MAX]
    rows = "".join(
        "<tr><td>" + ui.esc(str(j.get("created") or "")[:16].replace("T", " ")) + "</td>"
        "<td>&ldquo;" + ui.esc(j.get("instruction") or "") + "&rdquo;</td>"
        "<td>" + ui.esc(_facts_outcome(j)) + "</td>"
        "<td>" + ui.esc((j.get("commit_sha") or "")[:7]) + "</td></tr>"
        for j in history)
    table = (
        '<table class="facts-tbl"><tr><th>When</th><th>Instruction</th><th>Outcome</th>'
        f"<th>Commit</th></tr>{rows}</table>" if rows else "<p>No changes proposed yet.</p>")
    facts_path = cop.facts_path if cop is not None else None
    prefs_path = cop.prefs_path if cop is not None else None
    body = (
        '<main class="facts-page"><p><a class="link-quiet" href="/">Back to the board</a></p>'
        "<h1>Facts and preferences</h1>"
        "<section><h2>Ask for a change</h2>" + form + "</section>"
        "<section><h2>Current change</h2>" + _facts_current_html(current, now) + "</section>"
        "<section><h2>Current files</h2>"
        + _facts_file_html("preferences.yaml", prefs_path)
        + _facts_file_html("facts.yaml", facts_path) + "</section>"
        "<section><h2>History</h2>" + table + "</section></main>"
    )
    head = _FACTS_HEAD + (NOSCRIPT_REFRESH if busy else "")
    return ui.page("Facts and preferences", body, head=head)


def _safe_return(value: object) -> str:
    """A same-site relative path from a form field, or '' for anything else.

    Only paths that start with a single '/' are accepted: '//evil' and '\\evil' would
    be a redirect off the host, so they are dropped rather than fixed.
    """
    text = str(value or "").strip()
    if not text.startswith("/") or text.startswith("//") or text.startswith("/\\"):
        return ""
    if "\x00" in text or "\n" in text or "\r" in text:
        return ""
    return text[:600]


def last_scan_info() -> dict:
    """Age, source count and stored total for the sidebar scan card.

    Reads last_scan.json beside the dashboard, tolerating a missing or broken file: the
    card then says so instead of the page failing.
    """
    path = Path(DASHBOARD_PATH).with_name(LAST_SCAN_NAME)
    info = {"scanned_at": None, "sources": 0, "problems": 0}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return info
    if isinstance(data, dict):
        info["scanned_at"] = data.get("scanned_at")
        sources = data.get("sources")
        info["sources"] = len(sources) if isinstance(sources, list) else 0
        problems = data.get("problems")
        info["problems"] = len(problems) if isinstance(problems, list) else 0
    return info
# Live polling of a queued or running job stops here. Above copilot.CALL_TIMEOUT_S (30 min)
# plus staging, so a long job is never cut off while it is still working. The page script
# repeats this value as CEILING_MS; the tests check the two agree.
POLL_CEILING_S = 40 * 60

POSTING_JS = r"""<script>
(function () {
  "use strict";
  // Progressive enhancement for the posting page. Everything inserted from API data uses
  // textContent / createTextNode. The IIFE creates no globals.
  var body = document.body;
  var uid = body ? body.getAttribute("data-uid") : "";
  if (!uid || !window.fetch || !window.URLSearchParams || !window.FormData) { return; }

  // Take over progress from the no-JS meta refresh (it sits in <noscript>; drop any live copy too).
  var refreshes = document.querySelectorAll('meta[http-equiv="refresh"]');
  Array.prototype.forEach.call(refreshes, function (m) { m.parentNode.removeChild(m); });

  var API = "/api/postings/" + encodeURIComponent(uid);
  var FAST_MS = 4000, SLOW_MS = 20000, ACTIVE_MS = 2500;
  var WINDOW_MS = 10 * 60 * 1000;    // idle watch after a submit
  var CEILING_MS = 40 * 60 * 1000;   // hard stop for a queued or running job (above CALL_TIMEOUT_S plus staging)
  var ACTIVE = { queued: true, running: true };

  var actionForms = document.querySelectorAll('form[action$="/action"]');
  var actionButtons = document.querySelectorAll('form[action$="/action"] button');
  var statusForms = document.querySelectorAll('form[action$="/status"]');
  var statusText = document.getElementById("status-text");
  var timeline = document.querySelector("ul.timeline");
  var bodyForm = document.querySelector('form[action$="/body"]');

  var st = { job: null, packet: null, pollTimer: null, pollUntil: 0, tickTimer: null,
             submitting: false, queue: {}, view: { main: "", note: "" }, note: null,
             gate: null, ceilingId: null, ceilingAt: 0,
             fetchedAt: Date.now(), analysisDoneId: null, packetDoneId: null, firstRead: true };

  function each(list, fn) { Array.prototype.forEach.call(list, fn); }

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) { n.className = cls; }
    if (text != null) { n.textContent = String(text); }
    return n;
  }

  function isActive(j) { return !!j && ACTIVE[j.state] === true; }

  function startedMs(j) {
    var t = j.started || j.created;
    // Trim sub-millisecond digits so every browser parses the server's ISO timestamp.
    var ms = t ? Date.parse(String(t).replace(/(\.\d{3})\d+/, "$1")) : NaN;
    return isNaN(ms) ? Date.now() : ms;
  }

  function elapsedSecs(j) { return Math.max(0, Math.floor((Date.now() - startedMs(j)) / 1000)); }

  // Same-origin URL with any userinfo removed. A bookmark like http://user:pass@host/ carries
  // credentials that Chromium refuses to fetch with; the page's own origin is what we post to.
  function cleanUrl(href) {
    var u = new URL(href, location.href);
    u.username = "";
    u.password = "";
    return u.href;
  }

  // The one place that calls fetch. Resolves with {status, ok, data} for any answer, so a
  // 409 (which carries a reason) is told apart from a plain failure.
  function call(href, method, body, contentType) {
    var init = { method: method, headers: { Accept: "application/json" }, credentials: "same-origin" };
    if (body !== undefined) { init.body = body; }
    if (contentType) { init.headers["Content-Type"] = contentType; }
    if (method === "GET") { init.cache = "no-store"; }
    return fetch(cleanUrl(href), init).then(function (res) {
      return res.json().catch(function () { return {}; }).then(function (payload) {
        return { status: res.status, ok: res.ok, data: payload || {} };
      });
    });
  }

  // The payload, or a throw carrying the server's error text.
  function ok(r) {
    if (!r.ok || r.data.ok === false || r.data.error) {
      throw new Error(r.data.error || ("HTTP " + r.status));
    }
    return r.data;
  }

  function formObject(form, extra) {
    var out = {};
    new FormData(form).forEach(function (v, k) { out[k] = v; });
    if (extra) { Object.keys(extra).forEach(function (k) { out[k] = extra[k]; }); }
    return out;
  }

  function postForm(form) {
    return call(form.getAttribute("action"), "POST", new URLSearchParams(new FormData(form))).then(ok);
  }

  // Status and action forms post as JSON; the reply is returned whatever its status.
  function sendJson(form, extra) {
    return call(form.getAttribute("action"), "POST", JSON.stringify(formObject(form, extra)),
                "application/json");
  }

  function fetchPosting() {
    return call(API, "GET").then(ok);
  }

  function setActionsOff(off) {
    each(actionButtons, function (btn) { btn.disabled = off; });
  }

  // -- queue strip: the one progress surface -------------------------------------------
  // Wording mirrors workspace._queue_strip. Each strip gets the same two spans; the
  // [data-job-line] element gets the same summary as text, so the two never disagree.
  var STAGE_PHRASES = {
    device: "starting the device", model: "loading the model", navigate: "opening the app",
    composer: "opening the composer", focus: "focusing the composer",
    inject: "injecting the bundle", send: "sending the prompt", await: "waiting on the app",
    trigger: "triggering the reply", submit: "committing the task",
    context: "reading the context", "relaunch retry": "relaunching the app",
    start: "starting the run", confirm: "waiting on the app", record: "saving the result"
  };
  var strips = document.querySelectorAll("[data-queue-strip]");
  var jobLines = document.querySelectorAll("[data-job-line]");

  function mins(secs) { return Math.max(0, Math.ceil(secs / 60)); }
  function mmss(secs) {
    var s = Math.max(0, Math.floor(secs));
    return ("0" + Math.floor(s / 60)).slice(-2) + ":" + ("0" + (s % 60)).slice(-2);
  }
  // Never "about 0 min": anything under a minute reads as such.
  function leftText(secs, suffix) {
    return secs < 60 ? "under a minute " + suffix : "about " + mins(secs) + " min " + suffix;
  }

  function viewFor(j, q) {
    if (!j) { return { main: "", note: "" }; }
    var kind = j.kind || "job";
    var state = j.state || "";
    if (state === "queued") {
      var pos = j.position || q.position;
      return {
        main: pos ? kind + " queued #" + pos : kind + " queued",
        note: typeof q.est_wait_s === "number" ? leftText(q.est_wait_s, "to go") : ""
      };
    }
    if (state === "running") {
      var stage = j.stage ? String(j.stage).trim() : "";
      var phrase = !stage ? (kind === "prep" ? "building the packet" : "starting")
        : (Object.prototype.hasOwnProperty.call(STAGE_PHRASES, stage) ? STAGE_PHRASES[stage] : stage);
      var elapsed = elapsedSecs(j);
      var left = null;
      if (j.eta_s != null && j.eta_s !== "") {
        // eta_s was computed at st.fetchedAt; subtract what has passed since.
        left = Number(j.eta_s) - (Date.now() - st.fetchedAt) / 1000;
      } else if (Number(j.est_seconds) > 0) {
        left = Number(j.est_seconds) - elapsed;
      }
      var note = mmss(elapsed) + " elapsed";
      if (left !== null && !isNaN(left)) { note += " \u00b7 " + leftText(left, "left"); }
      return { main: kind + " running: " + phrase, note: note };
    }
    // Settled: keep it on the strip. Errors win over the message, as on the server.
    return { main: "last " + kind + " " + state, note: j.error || j.message || "" };
  }

  function paint() {
    var v = st.view;
    var note = st.note ? st.note.text : (st.gate ? st.gate.text : v.note);
    var err = !!(st.note && st.note.err);
    var gated = !st.note && !!st.gate;
    each(strips, function (s) {
      var m = s.querySelector(".q-main"), n = s.querySelector(".q-note");
      if (m) { m.textContent = v.main; }
      if (n) {
        n.textContent = note;
        if (gated) { n.appendChild(document.createTextNode(" ")); n.appendChild(runAnywayButton()); }
      }
      s.classList.toggle("q-err", err);
      s.hidden = !st.job && !st.note && !st.gate;   // hidden only when there is nothing to show
    });
  }

  // The 409 gate's control: reposts the same kind with force=1. Built, never parsed from HTML.
  function runAnywayButton() {
    var b = el("button", "btn btn-sm", "Run anyway");
    b.type = "button";
    b.addEventListener("click", function () {
      if (st.gate && st.gate.form) { queueFrom(st.gate.form, true); }
    });
    return b;
  }

  function syncJobLines() {
    if (!st.job) { return; }
    var v = st.view;
    var line = v.main + (v.note ? " \u00b7 " + v.note : "");
    each(jobLines, function (node) { node.textContent = line; });
  }

  function stopTick() {
    if (st.tickTimer) { clearInterval(st.tickTimer); st.tickTimer = null; }
  }

  // Recompute the view from st.job, paint it, and run the 1s clock only while a job runs.
  function refresh() {
    st.view = viewFor(st.job, st.queue);
    paint();
    syncJobLines();
    if (st.job && st.job.state === "running") {
      if (!st.tickTimer) { st.tickTimer = setInterval(refresh, 1000); }
    } else {
      stopTick();
    }
  }

  // Transient text (errors, poll failures, recompile results) goes in the strip's note.
  function setNote(text, isError) {
    st.note = text ? { text: String(text), err: !!isError } : null;
    paint();
  }

  // -- analysis card ----------------------------------------------------------------

  // The findings are parsed on the server, so a finished job re-renders this step
  // rather than patching a pre. Unsaved letter text is never thrown away.
  function editorDirty() {
    var ta = document.querySelector('textarea[name="body"]');
    return !!ta && ta.value !== ta.defaultValue;
  }

  var reloading = false;

  function reloadStep(why) {
    if (reloading) { return; }   // one reload per page load, never a loop
    if (editorDirty()) {
      setNote(why + "; your unsaved letter text is still here, reload when ready.", false);
      return;
    }
    reloading = true;
    location.replace(location.pathname + location.search);
  }

  // When an analysis job reaches done, re-read the step so its findings appear.
  function injectAnalysisWhenDone(j) {
    if (!j || j.state !== "done" || j.kind !== "analysis") { return; }
    if (st.analysisDoneId === j.id) { return; }
    st.analysisDoneId = j.id;
    reloadStep("Analysis ready");
  }

  // -- polling ----------------------------------------------------------------------

  // A done prep or letter job rewrites files on disk, so the step is re-rendered.
  function packetWhenDone(j) {
    if (!j || j.state !== "done" || (j.kind !== "prep" && j.kind !== "letter")) { return; }
    if (st.packetDoneId === j.id) { return; }
    st.packetDoneId = j.id;
    reloadStep(j.kind === "letter" ? "Letter ready" : "Packet updated");
  }

  function applyPosting(data) {
    st.packet = data.packet || null;
    if (data.status && statusText) { statusText.textContent = data.status; }
    st.queue = data.queue || {};
    st.job = data.latest_job || null;
    st.fetchedAt = Date.now();
    st.note = null;   // a good read clears transient notes (errors from the last failed poll)
    armCeiling(st.job);
    refresh();
    // The first read only paints: a job that finished before this page loaded must not
    // trigger a reload, or a posting with an old done job reloads itself forever.
    var first = st.firstRead;
    st.firstRead = false;
    if (!first) {
      injectAnalysisWhenDone(st.job);
      packetWhenDone(st.job);
    }
    setActionsOff(isActive(st.job) || st.submitting);
    return data;
  }

  function schedule(ms) {
    if (st.pollTimer) { clearTimeout(st.pollTimer); }
    st.pollTimer = setTimeout(poll, ms);
  }

  // A queued or running job is polled until its own 40-minute ceiling; the idle watch after a
  // submit still stops at WINDOW_MS. Hitting the ceiling is said in the strip, not silent.
  function poll() {
    st.pollTimer = null;
    var active = isActive(st.job);
    if (active && Date.now() > st.ceilingAt) {
      setNote("Stopped live updates after 40 minutes; the " + (st.job.kind || "job") + " is still " +
              st.job.state + ". Reload to check.", false);
      return;
    }
    if (!active && Date.now() > st.pollUntil) { return; }
    fetchPosting().then(applyPosting).then(function () {
      schedule(isActive(st.job) ? ACTIVE_MS : SLOW_MS);
    }, function (err) {
      setNote("Live update failed, retrying: " + err.message, true);
      schedule(SLOW_MS);
    });
  }

  // Each job gets its own ceiling, counted from the first moment this page sees it active.
  function armCeiling(j) {
    if (!isActive(j)) { st.ceilingId = null; return; }
    if (st.ceilingId !== j.id) { st.ceilingId = j.id; st.ceilingAt = Date.now() + CEILING_MS; }
  }

  function watch() {
    st.pollUntil = Date.now() + WINDOW_MS;
    armCeiling(st.job);
    schedule(FAST_MS);
  }

  // -- actions --------------------------------------------------------------

  function whyText(reach) {
    var why = reach ? reach.why : "";
    if (Array.isArray(why)) { why = why.join("; "); }
    return why ? String(why) : "out of reach";
  }

  // Queue an analysis, letter or prep job. A 409 "out of reach" is not an error: the reason
  // goes in the strip with a Run anyway control that reposts the same form with force=1.
  function queueFrom(form, force) {
    if (st.submitting || isActive(st.job)) { return; }
    st.submitting = true;
    st.gate = null;
    setActionsOff(true);
    sendJson(form, force ? { force: "1" } : null).then(function (r) {
      st.submitting = false;
      if (r.status === 409 && r.data.reach) {
        st.gate = { text: "Out of reach: " + whyText(r.data.reach) + ". Run anyway queues it regardless.",
                    form: form };
        setActionsOff(isActive(st.job));
        paint();
        return;
      }
      var data = ok(r);
      if (!data.job) { throw new Error("no job in reply"); }
      st.job = data.job;
      st.queue = data.queue || {};   // place and estimate now, before the first poll
      st.fetchedAt = Date.now();
      st.note = null;
      st.gate = null;
      armCeiling(st.job);
      refresh();
      setActionsOff(isActive(st.job));
      watch();
    }).catch(function (err) {
      st.submitting = false;
      setActionsOff(isActive(st.job));
      setNote("Could not queue the job: " + err.message, true);
    });
  }

  each(actionForms, function (form) {
    form.addEventListener("submit", function (ev) {
      ev.preventDefault();
      queueFrom(form, false);
    });
  });

  // -- cover-letter body editor: live word count, non-ASCII check, copy, unload guard ----
  // Built here, not in the server HTML, so the page without JS has none of these nodes.

  var bodyEdit = bodyForm ? bodyForm.querySelector("textarea") : null;
  var bodyBase = bodyEdit ? bodyEdit.value : "";   // text as last saved (or as loaded)
  var BODY_MIN = 240, BODY_MAX = 320;              // the pipeline's letter length rule, in words
  var copyFlashNode = null, copyTimer = null, checksTimer = null, checksSeq = 0;

  function wordCount(text) {
    var t = text.trim();
    return t ? t.split(/\s+/).length : 0;
  }

  // The textarea grows with its text (min 12 rows from the server), so a long letter is
  // readable without an inner scrollbar fighting the page.
  function autosize() {
    if (!bodyEdit) { return; }
    bodyEdit.style.height = "auto";
    bodyEdit.style.height = Math.max(bodyEdit.scrollHeight, 12 * 22) + "px";
  }

  // The one number in the panel's header; the server renders the same value on load.
  function paintCount() {
    var n = wordCount(bodyEdit.value);
    each(document.querySelectorAll("[data-word-count]"), function (node) {
      node.textContent = String(n);
    });
    var recompile = document.querySelector("[data-recompile]");
    if (recompile) { recompile.disabled = !bodyEdit.value.trim(); }
  }

  // Checks come from the server, so the live rows use the same validator as the saved rows.
  function paintChecks(data) {
    var box = document.querySelector("[data-letter-checks]");
    if (!box || !data || !Array.isArray(data.checks)) { return; }
    while (box.firstChild) { box.removeChild(box.firstChild); }
    data.checks.forEach(function (c) {
      var row = el("div", "check " + (c.ok ? "ok" : "warn"));
      row.appendChild(el("span", "mk", c.ok ? "\u2713" : "FAIL"));
      var body = el("span", null, c.label || "");
      if (c.detail) {
        body.appendChild(document.createTextNode(" "));
        body.appendChild(el("span", "muted", c.detail));
      }
      row.appendChild(body);
      box.appendChild(row);
    });
  }

  function liveChecks() {
    if (!bodyEdit) { return; }
    var seq = ++checksSeq;
    var payload = JSON.stringify({ body: bodyEdit.value });
    call(API + "/checks", "POST", payload, "application/json").then(function (r) {
      if (seq === checksSeq && r.ok) { paintChecks(r.data); }
    }, function () { /* the server-rendered rows stay; no banner for a keystroke */ });
  }

  if (bodyEdit && bodyForm) {
    var taBar = bodyForm.querySelector(".ta-bar");
    copyFlashNode = el("span", "meta copy-note");
    copyFlashNode.hidden = true;
    if (taBar) { taBar.appendChild(copyFlashNode); }

    var copyBtn = el("button", "btn btn-sm", "Copy plain text");
    copyBtn.type = "button";
    if (taBar) { taBar.appendChild(copyBtn); }

    // Muted line feedback for a copy attempt; clears itself after a moment.
    function flashCopy(msg) {
      if (!copyFlashNode) { return; }
      copyFlashNode.textContent = msg;
      copyFlashNode.hidden = false;
      if (copyTimer) { clearTimeout(copyTimer); }
      copyTimer = setTimeout(function () {
        copyFlashNode.hidden = true; copyTimer = null;
      }, 1500);
    }

    // Legacy path for non-secure contexts (plain HTTP), where navigator.clipboard is absent.
    // Returns true only when execCommand reports success. Never throws.
    function legacyCopy(text) {
      var ta = null, ok = false;
      try {
        ta = document.createElement("textarea");
        ta.value = text;
        ta.setAttribute("aria-hidden", "true");
        ta.style.position = "fixed";
        ta.style.left = "-9999px";
        ta.style.top = "0";
        ta.style.opacity = "0";
        document.body.appendChild(ta);
        ta.focus();
        ta.select();
        ta.setSelectionRange(0, text.length);
        ok = document.execCommand("copy") === true;
      } catch (e) {
        ok = false;
      } finally {
        if (ta && ta.parentNode) { ta.parentNode.removeChild(ta); }
      }
      return ok;
    }

    // Both clipboard paths failed (plain HTTP, or a refused execCommand). Select the textarea's
    // contents so the user's own Ctrl+C copies them. Never throws.
    function copyFailed() {
      try {
        bodyEdit.focus();
        bodyEdit.select();
        flashCopy("select-all done, press Ctrl+C");
      } catch (e) {
        flashCopy("copy failed");
      }
    }

    copyBtn.addEventListener("click", function () {
      var text = bodyEdit.value;
      try {
        var clip = navigator.clipboard;
        if (clip && typeof clip.writeText === "function") {
          clip.writeText(text).then(function () {
            flashCopy("copied");
          }, function () {
            if (legacyCopy(text)) { flashCopy("copied"); } else { copyFailed(); }
          });
          return;
        }
      } catch (e) {
        // fall through to the legacy path
      }
      if (legacyCopy(text)) { flashCopy("copied"); } else { copyFailed(); }
    });

    bodyEdit.addEventListener("input", function () {
      paintCount();
      autosize();
      if (checksTimer) { clearTimeout(checksTimer); }
      checksTimer = setTimeout(liveChecks, 500);
    });
    paintCount();
    autosize();

    // Warn before leaving with unsaved edits, but not while a job is running.
    window.addEventListener("beforeunload", function (ev) {
      if (bodyEdit.value === bodyBase || isActive(st.job)) { return; }
      ev.preventDefault();
      ev.returnValue = "";
    });
  }

  if (bodyForm) {
    bodyForm.addEventListener("submit", function (ev) {
      ev.preventDefault();
      var btn = bodyForm.querySelector("button");
      if (btn) { btn.disabled = true; }
      var sent = bodyEdit ? bodyEdit.value : "";
      postForm(bodyForm).then(function () {
        bodyBase = sent;
        setNote("saved the text and recompiled the PDF", false);
      }, function (err) {
        setNote("Recompile failed: " + err.message, true);
      }).then(function () {
        if (btn) { btn.disabled = false; }
        paintCount();
      });
    });
  }

  // Every status form on the page: the select form and the one-click "Mark as applied".
  var STATUS_ALL = [];   // the statuses the select offers, read from the page once

  function prependEvents(events) {
    if (!timeline) { return; }
    var shown = timeline.querySelectorAll("li:not(.meta)").length;
    var fresh = events.slice(shown);  // Store events are oldest first
    if (!fresh.length) { return; }
    var placeholder = timeline.querySelector("li.meta");
    if (placeholder) { timeline.removeChild(placeholder); }
    fresh.forEach(function (e) {
      var li = el("li");
      li.appendChild(el("span", "meta", e.ts || ""));
      li.appendChild(document.createTextNode(" "));
      li.appendChild(el("strong", null, e.kind || ""));
      li.appendChild(document.createTextNode(" " + (e.detail || "")));
      timeline.insertBefore(li, timeline.firstChild);
    });
  }

  function groupFor(sel, label) {
    var groups = sel.querySelectorAll("optgroup");
    for (var i = 0; i < groups.length; i += 1) {
      if (groups[i].getAttribute("label") === label) { return groups[i]; }
    }
    var g = el("optgroup");
    g.setAttribute("label", label);
    sel.appendChild(g);
    return g;
  }

  function optionFor(value) {
    var o = el("option", null, value);
    o.value = value;
    return o;
  }

  function bindStatusForm(form) {
    var sel = form.querySelector("select");
    if (sel && !STATUS_ALL.length) {
      each(sel.options, function (o) { if (o.value) { STATUS_ALL.push(o.value); } });
    }
    // In the pill menu a choice applies straight away; the button is there for no-JS.
    if (sel && form.closest(".status-menu")) {
      sel.addEventListener("change", function () {
        if (typeof form.requestSubmit === "function") { form.requestSubmit(); }
        else { form.dispatchEvent(new Event("submit", { cancelable: true })); }
      });
    }
    var errBox = el("div", "meta job-err");
    errBox.hidden = true;
    var endBox = el("div", "meta");   // the terminal-state line
    endBox.hidden = true;
    form.insertAdjacentElement("afterend", endBox);
    form.insertAdjacentElement("afterend", errBox);
    var locked = false;

    function showErr(msg) {
      errBox.textContent = msg;
      errBox.hidden = false;
    }

    // Terminal: the controls are disabled and the line says why; the form stays in place.
    function setLocked(on) {
      locked = on;
      each(form.querySelectorAll("select, input, button"), function (c) { c.disabled = on; });
      endBox.textContent = on ? "No further moves for this status." : "";
      endBox.hidden = !on;
    }

    // Rebuild the options from the reply inside the existing optgroups; the form itself,
    // its name and its required select are never recreated.
    function rebuildNext(next, status) {
      if (!sel) { form.hidden = true; return; }   // the one-click form's move is spent
      if (!Array.isArray(next) || !STATUS_ALL.length) { return; }
      each(sel.querySelectorAll("option"), function (o) { o.parentNode.removeChild(o); });
      var nextGroup = groupFor(sel, "Next move"), anyGroup = groupFor(sel, "Any status");
      next.forEach(function (s) { nextGroup.appendChild(optionFor(s)); });
      STATUS_ALL.forEach(function (s) { if (next.indexOf(s) < 0) { anyGroup.appendChild(optionFor(s)); } });
      sel.value = status;
      setLocked(!next.length);
    }

    form.addEventListener("submit", function (ev) {
      ev.preventDefault();
      var btn = form.querySelector("button");
      if (btn) { btn.disabled = true; }
      errBox.hidden = true;
      sendJson(form).then(ok).then(function (data) {
        // A one-click decision advances to the next posting in the same list.
        if (form.hasAttribute("data-advance")) {
          var nav = form.querySelector('input[name="return"]');
          if (nav && nav.value) { location.assign(nav.value); return; }
        }
        if (statusText) { statusText.textContent = data.status; }
        var menu = form.closest("details");
        if (menu) { menu.open = false; }   // the pill shows the new status now
        var noteIn = form.querySelector('input[name="note"]');
        if (noteIn) { noteIn.value = ""; }
        rebuildNext(data.allowed_next, data.status);
        return fetchPosting().then(function (p) {
          st.packet = p.packet || st.packet;
          prependEvents(p.events || []);
        }, function (e) {
          showErr("Status saved, but the timeline did not refresh: " + e.message);
        });
      }).catch(function (e) {
        showErr(e.message);
      }).then(function () {
        if (btn) { btn.disabled = locked; }
      });
    });
  }

  each(statusForms, bindStatusForm);

  // The variant menu applies on choice when JavaScript is on; the button covers no-JS.
  each(document.querySelectorAll("form[data-variant-form]"), function (form) {
    var sel = form.querySelector("select[name=\"variant\"]");
    if (!sel) { return; }
    sel.addEventListener("change", function () {
      if (typeof form.requestSubmit === "function") { form.requestSubmit(); }
      else { form.dispatchEvent(new Event("submit", { cancelable: true })); }
    });
  });

  // A build replaces files on disk, so packetWhenDone re-renders the step instead of
  // patching rows here; nothing in the packet panel is edited client-side.

  // -- start: one read; watch only if a job is already in flight ------------

  fetchPosting().then(applyPosting).then(function () {
    if (isActive(st.job)) { watch(); }
  }, function () { /* no JS-side state; the server-rendered page still works */ });
})();
</script>"""




def _esc(value: object) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _posting_packet_html(uid: str) -> str:
    """Packet links for the posting page; a missing file shows as plain text."""
    slug = packet_dir_name(uid)

    def present(name: str) -> bool:
        return _resolve_packet_file(slug, name) is not None

    def link(name: str) -> str:
        return f'<a href="{_esc("/packets/" + quote(slug) + "/" + quote(name))}">{_esc(name)}</a>'

    parts = [
        link(PACKET_FILE_RESUME) if present(PACKET_FILE_RESUME) else "no resume yet",
        link(PACKET_FILE_LETTER) if present(PACKET_FILE_LETTER) else "no letter yet",
        link("posting.md") if present("posting.md") else "no posting.md yet",
    ]
    return " · ".join(parts)


def _job_elapsed_s(job: dict) -> int:
    started = job.get("started")
    if not started:
        return 0
    try:
        began = datetime.fromisoformat(started)
    except ValueError:
        return 0
    return max(0, int((datetime.now(timezone.utc) - began).total_seconds()))


def _epoch(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso).timestamp()
    except (TypeError, ValueError):
        return None


def _est(job: dict) -> float:
    try:
        return float(job.get("est_seconds") or 0)
    except (TypeError, ValueError):
        return 0.0


def _mins(seconds: float) -> int:
    return max(0, -(-int(seconds) // 60))  # ceiling minutes


def derive_queue(jobs: list[dict], now: float | None = None) -> tuple[list[dict], dict | None, int]:
    """Creation-ordered job copies with elapsed_s, eta_s and position; the running job; queued count.

    The running job is place 1 and queued jobs follow in creation order; done and failed get 0.
    """
    now = time.time() if now is None else now
    items, queued = [], 0
    for job in jobs:
        item = dict(job)
        state = job.get("state")
        created, started = _epoch(job.get("created")), _epoch(job.get("started"))
        est = _est(job)
        if state == "running":
            base = started if started is not None else (created if created is not None else now)
            item["eta_s"] = max(0, int(base + est - now))
        elif state == "queued":
            base = created if created is not None else now
            item["eta_s"] = max(0, int(base + est - now))
            queued += 1
        else:
            item["eta_s"] = 0
        since = started if started is not None else created
        if state in ("done", "failed"):
            # Settled jobs report their real duration, not time since they settled.
            end = _epoch(job.get("finished"))
            item["elapsed_s"] = max(0, int(end - since)) if end is not None and since is not None else 0
        else:
            item["elapsed_s"] = max(0, int(now - since)) if since is not None else 0
        item["position"] = 0
        items.append(item)
    running = next((i for i in items if i.get("state") == "running"), None)
    place = 1
    if running is not None:
        running["position"] = place
        place += 1
    for item in items:
        if item.get("state") == "queued":
            item["position"] = place
            place += 1
    return items, running, queued


def queue_state(uid: str, copilot: "Copilot | None", now: float | None = None) -> dict:
    """The queue object for one posting's latest job, plus the strip text the page shows."""
    if copilot is None:
        view = {"position": 0, "running": None, "queued": 0, "est_wait_s": 0}
        return {"view": view, "job": None, "main": "AI copilot is not running", "note": ""}
    items, running, queued = derive_queue(copilot.jobs(), now)
    mine = next((i for i in reversed(items) if i.get("uid") == uid), None)
    est_wait = 0
    if mine is not None and mine.get("state") == "queued":
        for other in items:
            if other is mine or other.get("position", 0) >= mine["position"]:
                continue
            if other.get("state") == "running":
                est_wait += other["eta_s"]
            elif other.get("state") == "queued":
                est_wait += _est(other)
    elif mine is not None and mine.get("state") == "running":
        est_wait = mine["eta_s"]
    view = {
        "position": mine["position"] if mine is not None else 0,
        "running": ({"uid": running["uid"], "kind": running.get("kind"),
                     "stage": running.get("stage") or ""} if running is not None else None),
        "queued": queued,
        "est_wait_s": int(est_wait),
    }
    if mine is None:
        main, note = "no AI job yet for this posting", ""
    elif mine["state"] == "queued":
        main = f"Queued: position {mine['position']} in line"
        note = f"about {_mins(est_wait)} min to go"
    elif mine["state"] == "running":
        phrase = (workspace._stage_label(mine.get("stage"), mine.get("kind"))
                  if not mine.get("stage") and mine.get("kind") == "prep"
                  else STAGE_PHRASES.get(mine.get("stage") or "", "working"))
        elapsed = mine["elapsed_s"]
        main = f"{AI_LABELS.get(mine.get('kind'), mine.get('kind'))}: {phrase}"
        note = (f"{elapsed // 60:02d}:{elapsed % 60:02d} elapsed · "
                f"about {_mins(mine['eta_s'])} min left")
    elif mine["state"] == "failed":
        main, note = "Last AI run failed", str(mine.get("error") or "unknown error")
    else:
        main, note = "Last AI run finished", ""
    return {"view": view, "job": mine, "main": main, "note": note}


def _names(store: Store | None, uids) -> dict:
    """uid -> (company, title) for the given uids, in one query. Unknown uids are absent."""
    wanted = sorted({u for u in uids if u})
    if store is None or not wanted:
        return {}
    marks = ",".join("?" * len(wanted))
    # Store has no multi-uid read; this one query is the only reader of the postings table here.
    rows = store._conn.execute(
        f"SELECT uid, company, title FROM postings WHERE uid IN ({marks})", wanted).fetchall()
    return {r["uid"]: (r["company"], r["title"]) for r in rows}


def _annotate(item: dict, names: dict) -> dict:
    """A job copy with company, title, kind_label and stage_label, as /api/jobs and the panel show it."""
    company, title = names.get(item.get("uid"), (None, None))
    out = dict(item)
    out["company"] = company
    out["title"] = title
    out["kind_label"] = AI_LABELS.get(item.get("kind"), item.get("kind") or "")
    out["stage_label"] = ("waiting" if item.get("state") == "queued"
                          else workspace._stage_label(item.get("stage"), item.get("kind")))
    return out


def queue_panel(copilot: "Copilot | None", now: float | None = None, store: Store | None = None) -> dict:
    """The triage queue panel: the running job, the queued jobs in line order, and the active count.

    Built from derive_queue, the same derivation /api/jobs uses, so the two cannot disagree.
    count is queued + running, exactly the number /api/jobs reports.
    """
    if copilot is None:
        return {"running": None, "queued": [], "count": 0}
    items, running, queued = derive_queue(copilot.jobs(), now)
    waiting = [i for i in items if i.get("state") == "queued"]
    uids = [i.get("uid") for i in waiting] + ([running.get("uid")] if running else [])
    names = _names(store, uids)
    return {"running": _annotate(running, names) if running else None,
            "queued": [_annotate(i, names) for i in waiting],
            "count": queued + (1 if running else 0)}


def _reach_for(posting: dict) -> dict:
    return reach.classify(posting.get("title") or "", posting.get("description") or "")


def _with_queue_place(latest: dict | None, uid: str, copilot: "Copilot | None") -> dict | None:
    """A copy of the latest job with position and eta_s, both from derive_queue (as /api/jobs).

    The two keys are always present when there is a job; they read 0 when the queue does not
    place this job (for example, the copilot is gone or the job was pruned from the list).
    """
    if latest is None:
        return None
    mine = queue_state(uid, copilot)["job"] if copilot is not None else None
    if mine is None or mine.get("id") != latest.get("id"):
        return {**latest, "position": 0, "eta_s": 0}
    return {**latest, "position": mine.get("position", 0), "eta_s": mine.get("eta_s", 0)}


def _why_text(verdict: dict) -> str:
    why = verdict.get("why")
    if isinstance(why, (list, tuple)):
        return ", ".join(str(w) for w in why)
    return str(why or "")


def _truthy_force(value: object) -> bool:
    return value is True or str(value).strip().lower() in FORCE_VALUES


def _backfill_levels(db_path: str) -> int:
    """Fill level and years on stored rows. Never fails the caller; never creates a DB file."""
    if not Path(db_path).is_file():
        return 0
    try:
        store = Store(str(db_path))
        try:
            return int(store.backfill_levels(reach.classify))
        finally:
            store.close()
    except Exception:  # the server must still boot and serve
        logger.exception("reach level backfill failed; serving anyway")
        return 0


def _job_line_html(job: dict, uid: str) -> str:
    """One line describing a job: state, error or blocker, prep message, and artifact link."""
    kind = _esc(job.get("kind"))
    state = job.get("state")
    if state == "queued":
        text = f"queued ({kind})"
    elif state == "running":
        text = f"running ({kind}) for {_job_elapsed_s(job)} s"
    elif state == "done":
        text = f"done ({kind})"
        if job.get("message"):
            text += f": {_esc(job.get('message'))}"
    elif job.get("blocker") == "french":
        text = f"rejected: French blocker ({kind}): {_esc(job.get('error'))}"
    else:
        text = f"failed ({kind}): {_esc(job.get('error') or 'unknown error')}"
    name = os.path.basename(job.get("artifact_path") or "")
    slug = packet_dir_name(uid)
    if name in ALLOWED_FILES and _resolve_packet_file(slug, name) is not None:
        text += (f' · <a href="/packets/{_esc(quote(slug))}/{_esc(quote(name))}">'
                 f"{_esc(name)}</a>")
    # data-job-line: the page script rewrites this element to match the queue strip.
    return f'<div class="meta job-line" data-job-line>latest AI job: {text}</div>'


def _body_editor_html(uid: str, copilot: "Copilot | None") -> str:
    """Compact editor for the accepted letter body; empty when there is no accepted body."""
    if copilot is None:
        return ""
    path = copilot.bodies_dir / f"{packet_dir_name(uid)}.txt"
    if not path.is_file():
        return ""
    text = path.read_text(encoding="utf-8", errors="replace")
    action = f"/api/postings/{quote(uid, safe='')}/body"
    return (
        "<h2>Edit cover letter body</h2><div class=\"card\">"
        f'<form method="post" action="{_esc(action)}">'
        f'<textarea class="body-edit" name="body" rows="12">{_esc(text)}</textarea>'
        '<div class="sform"><button type="submit">Recompile PDF</button></div></form>'
        '<div class="meta">Recompiles locally in seconds and calls no model.</div></div>'
    )


def _ai_block_html(uid: str, copilot: "Copilot | None", posting: dict | None = None,
                   redraft: bool = False) -> str:
    """The AI card: one analysis button, reach line, queue strip, prep and letter as needed."""
    if copilot is None:
        return ('<h2>AI actions</h2><div class="card"><div class="meta">'
                "AI copilot is not running on this server.</div></div>")
    uid_q = quote(uid, safe="")
    action = f"/api/postings/{uid_q}/action"
    slug = packet_dir_name(uid)

    def action_form(kind: str, label: str, extra: str = "") -> str:
        return (f'<form class="sform" method="post" action="{_esc(action)}" data-action-form>'
                f'<input type="hidden" name="kind" value="{_esc(kind)}">{extra}'
                f'<button type="submit">{_esc(label)}</button></form>')

    parts = []
    if _resolve_packet_file(slug, PACKET_FILE_RESUME) is None:
        parts.append(f'<div class="prep-row">{action_form(PREP_KIND, PREP_LABEL)}'
                     '<div class="meta">Build packet takes seconds; the analysis takes two to '
                     "three minutes.</div></div>")
    parts.append(action_form(ANALYZE_KIND, AI_LABELS[ANALYZE_KIND]))
    if posting is not None:
        verdict = _reach_for(posting)
        if verdict["verdict"] == "out_of_reach":
            parts.append(
                f'<div class="meta reach-line" data-reach="out_of_reach">Out of reach: '
                f"{_esc(_why_text(verdict))}. "
                f'<form class="sform" method="post" action="{_esc(action)}" data-force-form>'
                f'<input type="hidden" name="kind" value="{_esc(ANALYZE_KIND)}">'
                '<input type="hidden" name="force" value="1">'
                '<button type="submit">Run anyway</button></form></div>')
    state = queue_state(uid, copilot)
    parts.append(
        '<div class="queue-strip" data-queue-strip role="status">'
        f'<span class="q-main">{_esc(state["main"])}</span> '
        f'<span class="q-note">{_esc(state["note"])}</span></div>')
    letter_path = copilot.bodies_dir / f"{slug}.txt"
    if redraft or not letter_path.is_file():
        label = "Redraft with AI" if letter_path.is_file() else AI_LABELS["letter"]
        parts.append(action_form("letter", label))
    latest = copilot.latest(uid)
    parts.append(_job_line_html(latest, uid) if latest
                 else '<div class="meta">no AI job yet for this posting</div>')
    analysis = _read_text(_resolve_packet_file(slug, "analysis.md"))
    parts.append('<h3>analysis.md</h3><pre class="ai-text" data-analysis>'
                 f'{_esc(analysis if analysis is not None else "No analysis yet.")}</pre>')
    for name in ("critique.md", "gaps.md"):  # legacy packets only
        text = _read_text(_resolve_packet_file(slug, name))
        if text is not None:
            parts.append(f"<h3>{_esc(name)}</h3><pre class=\"ai-text\">{_esc(text)}</pre>")
    return f'<h2>AI actions</h2><div class="card" id="ai-actions">{"".join(parts)}</div>'


def _posting_page_html(posting: dict, events: list[dict], copilot: "Copilot | None" = None) -> str:
    """Self-contained HTML page for one posting. Every interpolated value is escaped."""
    uid = posting.get("uid") or ""
    busy = copilot is not None and any(
        j["uid"] == uid and j["state"] in ("queued", "running") for j in copilot.jobs())
    url = posting.get("url")
    if url and (url.startswith("https://") or url.startswith("http://")):
        url_html = f'<a href="{_esc(url)}">open the real posting — you submit there</a>'
    else:
        url_html = f"{_esc(url) or '—'} (open the real posting — you submit there)"
    score = posting.get("score")
    score_text = "—" if score is None else f"{score:g}"
    if events:
        items = "".join(
            '<li><span class="meta">' + _esc(e.get("ts")) + "</span> "
            "<strong>" + _esc(e.get("kind")) + "</strong> " + _esc(e.get("detail")) + "</li>"
            for e in reversed(events)  # Store.events is oldest first; show newest first
        )
    else:
        items = '<li class="meta">no events yet</li>'
    form = _status_form(posting) or '<div class="meta">no further moves from here</div>'
    title = posting.get("company") or "posting"
    return (
        "<!DOCTYPE html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        + (NOSCRIPT_REFRESH if busy else "")
        + f"<title>{_esc(title)} — jobsearch</title>"
        f"<style>{POSTING_CSS}</style></head><body data-uid=\"{_esc(uid)}\">"
        '<p><a href="/">&larr; dashboard</a></p>'
        f"<h1>{_esc(posting.get('company') or '—')} — {_esc(posting.get('title') or '—')}</h1>"
        f'<div class="meta">location: {_esc(posting.get("location") or "—")}</div>'
        f'<div class="meta">score: {_esc(score_text)} · variant: {_esc(posting.get("variant") or "—")}</div>'
        f'<div class="meta">status: <strong id="status-text">{_esc(posting.get("status") or "—")}</strong></div>'
        f'<div class="card">{url_html}</div>'
        f'<div class="card"><div class="meta">packet: {_posting_packet_html(posting.get("uid") or "")}</div></div>'
        f"{_body_editor_html(uid, copilot)}"
        f"{_ai_block_html(uid, copilot, posting=posting)}"
        f"<h2>Update status</h2>{form}"
        f'<h2>Timeline</h2><ul class="timeline">{items}</ul>'
        + POSTING_JS + "</body></html>\n"
    )


# ---------- workspace context ----------

WORKSPACE_ORDER = ("applied", "interview", "offer")  # statuses at or past "submitted by hand"


def _mtime_iso(path: Path | None) -> str:
    """The file's mtime as an ISO timestamp, or '' when it does not exist."""
    try:
        if path is None or not path.is_file():
            return ""
        return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(
            timespec="seconds")
    except OSError:
        return ""


def human_bytes(n: int) -> str:
    """A short size a person can read: 812 B, 41 kB, 2.1 MB."""
    size = float(max(0, int(n)))
    for unit in ("B", "kB", "MB", "GB"):
        if size < 1000 or unit == "GB":
            return f"{int(size)} {unit}" if unit == "B" else f"{size:.1f} {unit}".replace(".0 ", " ")
        size /= 1000
    return f"{size:.1f} GB"


def _read_text(path: Path | None) -> str | None:
    if path is None or not path.is_file():
        return None
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _facts_text() -> str:
    try:
        from jobsearch.letter import DEFAULT_FACTS
    except ImportError:  # pragma: no cover - letter is a sibling module
        return ""
    return _read_text(Path(DEFAULT_FACTS).expanduser()) or ""


# Stock phrases a hiring manager has read a thousand times. Matched case-insensitively
# at a word start, so "leverage" also catches "leveraged" and "leveraging".
CLICHES = (
    "passionate about", "leverage", "synergy", "team player", "hit the ground running",
    "excited to", "dynamic environment", "fast-paced environment", "wear many hats",
    "self-starter", "results-driven", "go-getter", "think outside the box",
    "proven track record", "rockstar", "ninja", "guru", "detail-oriented",
)
SHINGLE = 8  # words; a run this long copied from the resume reads as pasted
_LEGAL_SUFFIXES = {"inc", "llc", "ltd", "limited", "corp", "corporation", "co", "company",
                   "gmbh", "plc", "sa", "ag"}


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


def _company_mentioned(body: str, company: str) -> bool:
    """True when the company's words appear as a whole-word run in the body.

    Punctuation and case are ignored on both sides; a trailing legal suffix ("Inc.")
    may be left off the letter.
    """
    body_words = _words(body)
    names = [_words(company)]
    if len(names[0]) > 1 and names[0][-1] in _LEGAL_SUFFIXES:
        names.append(names[0][:-1])
    haystack = " " + " ".join(body_words) + " "
    return any(name and " " + " ".join(name) + " " in haystack for name in names)


def _longest_resume_run(body: str, resume_text: str) -> tuple[int, str]:
    """Longest run of words the body shares with the resume, and that run as text."""
    a, b = _words(body), _words(resume_text)
    match = difflib.SequenceMatcher(None, a, b, autojunk=False).find_longest_match(
        0, len(a), 0, len(b))
    return match.size, " ".join(a[match.a:match.a + match.size])


def _resume_text(dir_name: str) -> str | None:
    """Text layer of the packet's compiled resume, or None when there is none to compare."""
    pdf = _resolve_packet_file(dir_name, PACKET_FILE_RESUME)
    if pdf is None:
        return None
    try:
        return _copilot_pdf_text(pdf)
    except (ArtifactError, OSError):
        return None


def _letter_checks(body: str | None, posting_text: str, *, company: str | None = None,
                   resume_text: str | None = None) -> list[dict]:
    """The existing validators as six rows, plus three about the letter's voice.

    The first six are decided by copilot.validate(), so the checklist cannot drift from
    the gate the AI draft has to pass. The last three (company, resume reuse, cliches)
    are missing-safe: no company or no resume never makes a letter look wrong.
    With no body there is nothing to check.
    """
    if not body:
        return []
    _ok, problems = validate_answer("letter", body, _facts_text(), posting_text or "")
    words = len(body.split())
    has = lambda prefix: [p for p in problems if p.startswith(prefix)]  # noqa: E731
    unverified = [p.split("=", 1)[-1] for p in has("unverified_metric=")]
    projects = [p.split("=", 1)[-1] for p in has("project_proper_noun=")]
    french_blocked = any("french" in p.lower() for p in problems)
    rows = [
        ("240-320 words", 240 <= words <= 320, f"{words} words"),
        ("ASCII only", "non_ascii" not in problems,
         "no smart quotes or accented characters"),
        ("No project names", not projects,
         "names: " + ", ".join(projects) if projects else "nothing named from facts.yaml"),
        ("No tech-list parentheses", not has("tech_list_parenthetical="),
         "comma-heavy parentheticals are rejected"),
        ("Every number traceable", not unverified,
         ("not in facts.yaml: " + ", ".join(unverified)) if unverified
         else "each figure appears in facts.yaml"),
        ("No French requirement", not french_blocked,
         "the posting does not ask for French"),
    ]
    rows.append(_company_row(body, company))
    rows.append(_reuse_row(body, resume_text))
    rows.append(_cliche_row(body))
    return [{"label": label, "ok": pass_, "detail": detail} for label, pass_, detail in rows]


def _company_row(body: str, company: str | None) -> tuple[str, bool, str]:
    if not _words(company or ""):
        return ("Names the company", True, "no company on the posting")
    if _company_mentioned(body, company or ""):
        return ("Names the company", True, "")
    return ("Names the company", False, "not mentioned")


def _reuse_row(body: str, resume_text: str | None) -> tuple[str, bool, str]:
    if not (resume_text or "").strip():
        return ("No resume sentences reused", True, "no resume to compare")
    run, text = _longest_resume_run(body, resume_text or "")
    if run < SHINGLE:
        return ("No resume sentences reused", True, "")
    shown = text if len(text) <= 60 else text[:57].rstrip() + "..."
    return ("No resume sentences reused", False, f'{run} words shared: "{shown}"')


def _cliche_row(body: str) -> tuple[str, bool, str]:
    lowered = body.lower()
    hits = [c for c in CLICHES if re.search(r"\b" + re.escape(c), lowered)]
    if not hits:
        return ("No cliches", True, "")
    return ("No cliches", False, ", ".join(hits[:2]))


def _packet_variant(events: list[dict]) -> str | None:
    """Which of the six resume builds the packet used, from the prep event note."""
    for event in reversed(events):
        detail = str(event.get("detail") or "")
        if "resume" not in detail or "(" not in detail:
            continue
        inner = detail.rsplit("(", 1)[-1].rstrip(")").strip()
        # cmd_prep writes "packet staged (<variant> resume)"; the variant is the first word.
        return inner[: -len(" resume")].strip() if inner.endswith(" resume") else inner
    return None


def _stale_packet(packet: dict, variant: str, *, facts_path=None, resume_out=None) -> list[str]:
    """Reasons the built packet is older than the facts file or its resume PDF.

    The built files are the packet's resume and letter PDFs. Paths are looked up on the
    module at call time so a test can point DEFAULT_FACTS_PATH / RESUME_OUT elsewhere.
    """
    slug = packet.get("slug") or ""
    built = []
    for f in packet.get("files") or []:
        if str(f.get("name") or "") in (PACKET_FILE_RESUME, PACKET_FILE_LETTER):
            real = _resolve_packet_file(slug, str(f["name"]))
            if real is not None:
                built.append(real)
    sources = [("facts.yaml", Path(facts_path or copilot.DEFAULT_FACTS_PATH))]
    if variant in cli.PDF_NAMES:
        out = Path(resume_out or cli.RESUME_OUT)
        sources.append((f"the {variant} resume", out / cli.PDF_NAMES[variant]))
    return staleness.stale_reasons(built, sources)


def _packet_ctx(uid: str, events: list[dict]) -> dict:
    slug = packet_dir_name(uid)
    files = []
    for name, label in ((PACKET_FILE_RESUME, "resume.pdf"), (PACKET_FILE_LETTER, "letter.pdf"),
                        ("posting.md", "posting.md")):
        real = _resolve_packet_file(slug, name)
        if real is None:
            continue
        try:
            age = ui.rel_age(datetime.fromtimestamp(real.stat().st_mtime, timezone.utc)
                             .isoformat(timespec="seconds"))
        except OSError:
            age = "—"
        try:
            size = real.stat().st_size
        except OSError:
            size = 0
        files.append({"name": name, "label": label, "age": age, "size": size,
                      "size_h": human_bytes(size)})
    return {"slug": slug, "files": files, "resume_variant": _packet_variant(events)}


# Heading words that decide a posting section, most specific first. A line only counts
# as a heading when it is short and reads like a label, so a sentence mentioning
# "requirements" mid-paragraph never starts a section.
SECTION_KEYWORDS = (
    ("Perks", ("perks", "benefits", "what we offer", "why join", "why you'll love",
               "compensation", "we offer")),
    ("Location", ("location", "where you'll work", "where you will work", "work location",
                  "remote", "hybrid", "office")),
    ("Requirements", ("requirements", "qualifications", "what you'll need",
                      "what you will need", "must have", "you have",
                      "what we're looking for", "what we are looking for", "skills",
                      "experience")),
    ("Role", ("about the role", "the role", "role", "responsibilities", "what you'll do",
              "what you will do", "about the job", "description", "about us", "the team")),
)


def _section_label(line: str) -> str | None:
    """The section a heading line names, or None when the line is body text."""
    text = " ".join(line.split()).strip().rstrip(":").strip()
    if not text or len(text) > 64 or len(text.split()) > 8:
        return None
    if text.endswith((".", "!", "?")):
        return None
    low = text.lower()
    for label, words in SECTION_KEYWORDS:
        if any(w in low for w in words):
            return label
    return None


def _posting_sections(text: str, tokens) -> list[dict]:
    """Split a posting into Role / Location / Requirements / Perks where the text says so.

    Everything before the first heading is the Role block; a posting with no headings
    is one section, and the chip row is simply not rendered. Highlights are applied
    per section so a <mark> can never straddle a split.
    """
    blocks: list[tuple[str, list[str]]] = []
    label = "Role"
    lines: list[str] = []
    for line in (text or "").splitlines():
        found = _section_label(line)
        if found is not None and (lines or blocks):
            if "".join(lines).strip():
                blocks.append((label, lines))
            label, lines = found, []   # the heading names the section; it is not body text
        else:
            lines.append(line)
    if "".join(lines).strip():
        blocks.append((label, lines))

    seen: dict[str, int] = {}
    out = []
    for name, block in blocks:
        seen[name] = seen.get(name, 0) + 1
        shown = name if seen[name] == 1 else f"{name} {seen[name]}"
        key = f"{name.lower().replace(' ', '-')}-{seen[name]}"
        out.append({"key": key, "label": shown,
                    "html": highlight("\n".join(block).strip(), tokens, ui.esc)})
    if not out:
        out = [{"key": "posting-1", "label": "Posting",
                "html": highlight(text or "", tokens, ui.esc)}]
    return out


STEP_ORDER = (("review", "Review"), ("packet", "Packet"), ("letter", "Letter"),
              ("submit", "Submit"))


def _location_blocker(posting: dict, prefs: dict | None = None) -> dict | None:
    """The posting's location against the location in facts.yaml, when it is elsewhere.

    A remote flag or a 'remote' in the location line clears it, as does any mention of
    Winnipeg/Manitoba; a list of cities that includes Winnipeg still counts as reachable.
    Nothing is inferred: no location line means no verdict.

    Relocation: with "CA" in prefs countries, a Canadian posting
    clears unless its province is in exclude_regions; an excluded province keeps the hard
    blocker and the facts line names the region. prefs=None loads preferences.yaml once.
    """
    where = str(posting.get("location") or "").strip()
    if not where:
        return None
    home = contact_location() or DEFAULT_CITY
    low = where.lower()
    if posting.get("remote") or "remote" in low or "anywhere" in low:
        return None
    city = home.split(",")[0].strip().lower()
    if city and city in low:
        return None
    # The example profile is in Manitoba; change these two terms with your own province.
    if "manitoba" in low or re.search(r"\bmb\b", low):
        return None
    if prefs is None:
        prefs = _match.load_preferences()
    facts = f"facts.yaml lists {home}"
    if _is_canadian_location(where):
        excluded = _match._excluded_region(where, prefs)
        if excluded:
            facts = f"preferences.yaml excludes {excluded} (facts.yaml lists {home})"
        elif "CA" in (prefs.get("countries") or []):
            return None
    return {"kind": "location", "title": f"Location is not {home.split(",")[0].strip()}",
            "posting": where, "facts": facts}


# Canadian province and territory codes, for the relocation rule in _location_blocker.
_CA_PROVINCE_CODES = frozenset({"AB", "BC", "MB", "NB", "NL", "NS", "NT", "NU", "ON", "PE", "QC",
                                "SK", "YT"})


def _is_canadian_location(where: str) -> bool:
    """A Canadian city or country term in the location, or a Canadian province code."""
    if _match._first_hit(_match._fold(where), _match._CANADA_TERMS):
        return True
    return any(code in _CA_PROVINCE_CODES for code in _match._REGION_CODE.findall(where))


def _sponsorship_blocker(posting: dict, prefs: dict | None) -> dict | None:
    """A US posting against the recorded US work authorization.

    Only when the US is recorded as requires_sponsorship: a posting that declines sponsorship
    is a hard row quoting the phrase; one silent on it is a soft row to confirm. Any other
    case (no preference, or an authorized status) gives no row.
    """
    us = ((prefs or {}).get("work_authorization") or {}).get("US") or {}
    if us.get("status") != "requires_sponsorship":
        return None
    where = str(posting.get("location") or "").strip()
    if not _match._is_us_location(_match._fold(where), where):
        return None
    facts = "preferences.yaml records US work authorization as requires_sponsorship"
    declined = _match._NO_SPONSORSHIP.search(_match._norm(posting.get("description")))
    if declined:
        return {"kind": "sponsorship", "severity": "hard",
                "title": "US visa sponsorship not offered",
                "posting": declined.group(0), "facts": facts}
    return {"kind": "sponsorship", "severity": "soft",
            "title": "US role: confirm visa sponsorship",
            "posting": "the posting does not mention sponsorship", "facts": facts}


def _blockers(posting: dict, text: str, reach_verdict: dict,
              analysis_text: str | None = None, prefs: dict | None = None) -> list[dict]:
    """Everything that should stop a submit: language, location, seniority, liveness, verdict.

    One list, rendered once, so a blocker can never sit in only one of the places it used
    to be mentioned. Every row carries a ``severity``: "hard" stops the submit, "soft"
    asks for a decision first. apply_gate() turns the list into one decision.
    prefs=None loads preferences.yaml once for the whole list; the sponsorship row is last.
    """
    if prefs is None:
        prefs = _match.load_preferences()
    out: list[dict] = []
    quote = french_quote(text)
    if quote:
        out.append({"kind": "language", "severity": "hard", "title": "French is required",
                    "posting": quote,
                    "facts": "the profile records no French proficiency"})
    elif looks_french(text):
        out.append({"kind": "language", "severity": "hard",
                    "title": "The posting is written in French",
                    "posting": "the posting body reads as French",
                    "facts": "the profile records no French proficiency"})
    located = _location_blocker(posting, prefs)
    if located:
        out.append({**located, "severity": "hard"})
    if reach_verdict.get("verdict") in ("out_of_reach", "stretch"):
        out_of_reach = reach_verdict["verdict"] == "out_of_reach"
        out.append({
            "kind": "seniority",
            "severity": "hard" if out_of_reach else "soft",
            "title": "Out of reach" if out_of_reach else "A stretch",
            "posting": reach_verdict.get("why") or "the title sits above your level",
            "facts": f"the record shows about {reach.PROFILE_YEARS} year"
                     f"{'' if reach.PROFILE_YEARS == 1 else 's'} of experience",
        })
    if posting.get("live_status") == "gone":
        out.append({"kind": "liveness", "severity": "hard",
                    "title": "Posting appears closed",
                    "posting": "source URL returned gone/closed",
                    "facts": "checked " + (posting.get("live_checked_at") or "earlier")})
    level, verdict_line = extract_verdict_level(analysis_text)
    if level == "no_chance":
        out.append({"kind": "verdict", "severity": "hard",
                    "title": "Analysis verdict: no chance",
                    "posting": verdict_line, "facts": "from analysis.md VERDICT"})
    elif level == "stretch":
        out.append({"kind": "verdict", "severity": "soft",
                    "title": "Analysis verdict: stretch",
                    "posting": verdict_line, "facts": "from analysis.md VERDICT"})
    sponsorship = _sponsorship_blocker(posting, prefs)
    if sponsorship:
        out.append(sponsorship)
    return out


def _progress_steps(packet: dict, status: str, analysis_present: bool,
                    wanted: str = "") -> tuple[list[dict], str]:
    """The four steps and the one to show, both derived from what exists.

    Review is done when analysis.md exists, Packet when resume.pdf does, Letter when
    letter.pdf does, Submit when the status is at or past applied. A ?step= naming a
    real step wins; otherwise the page opens on the first incomplete one.
    """
    done = {
        "review": bool(analysis_present),
        "packet": any(f["name"] == PACKET_FILE_RESUME for f in packet["files"]),
        "letter": any(f["name"] == PACKET_FILE_LETTER for f in packet["files"]),
        "submit": status in WORKSPACE_ORDER,
    }
    rows = [{"key": key, "label": label, "done": done[key]} for key, label in STEP_ORDER]
    wanted = (wanted or "").strip().lower()
    if wanted in done:
        return rows, wanted
    for key, _label in STEP_ORDER:
        if not done[key]:
            return rows, key
    return rows, "submit"


def workspace_context(posting: dict, events: list[dict], copilot: "Copilot | None",
                      back_url: str = "/", step: str = "") -> dict:
    """Everything the workspace template needs, assembled in one place."""
    uid = posting.get("uid") or ""
    profile = load_profile()
    prefs = _match.load_preferences()
    parts = score_parts(posting, profile, prefs=prefs)
    text = posting.get("description") or ""
    tokens = sorted(profile.get("tokens", set()))
    body = None
    if copilot is not None:
        body = _read_text(copilot.bodies_dir / f"{packet_dir_name(uid)}.txt")
    packet = _packet_ctx(uid, events)
    resume = _resume_text(packet["slug"])
    packet["coverage"] = resume_coverage(text, resume, tokens)
    stale = _stale_packet(packet, posting.get("variant") or "")
    latest = copilot.latest(uid) if copilot is not None else None
    busy = bool(latest and latest.get("state") in ("queued", "running"))
    quote = french_quote(text)
    french_body = looks_french(text)
    gaps = _read_text(_resolve_packet_file(packet["slug"], "gaps.md"))
    critique = _read_text(_resolve_packet_file(packet["slug"], "critique.md"))
    words = len(body.split()) if body else 0
    verdict = _reach_for(posting)
    latest = _with_queue_place(latest, uid, copilot)
    analysis_path = _resolve_packet_file(packet["slug"], "analysis.md")
    analysis = _read_text(analysis_path)
    answers = _read_text(_resolve_packet_file(packet["slug"], "answers.md"))
    steps, current = _progress_steps(packet, posting.get("status") or "", bool(analysis), step)
    blockers = _blockers(posting, text, verdict, analysis, prefs)
    return {
        "posting": posting,
        "events": events,
        "parts": parts,
        "matched": matched_skills(text, tokens),
        "highlighted_html": highlight(text, tokens, ui.esc),
        "posting_sections": _posting_sections(text, tokens),
        "body_text": body,
        "word_count": words,
        "word_ok": 240 <= words <= 320,
        "letter_checks": _letter_checks(body, text, company=posting.get("company"),
                                        resume_text=resume),
        "packet": packet,
        "stale": stale,
        "variant": posting.get("variant") or "",
        "variants": list(VARIANTS),
        "analysis_text": analysis,
        "analysis_when": _mtime_iso(analysis_path),
        "answers_text": answers,
        "step": current,
        "steps": steps,
        "reach": verdict,
        "latest_job": latest,
        "busy": busy,
        "gaps_text": gaps,
        "critique_text": critique,
        "blockers": blockers,
        "apply_gate": apply_gate(blockers),
        "back_url": back_url,
        "allowed_next": ALLOWED_NEXT.get(posting.get("status") or "", ()),
        "statuses": STATUSES,
        "kinds": [
            {"kind": PREP_KIND, "label": PREP_LABEL, "when": "takes seconds"},
            {"kind": "letter", "label": "Redraft with AI", "when": "two to three minutes"},
            {"kind": "critique", "label": AI_LABELS["critique"], "when": "two to three minutes"},
            {"kind": "gaps", "label": AI_LABELS["gaps"], "when": "two to three minutes"},
        ],
    }


# ---------- auth ----------

def _credentials_from_env() -> tuple[str, str] | None:
    """Return (user, password) or None if either is missing or empty."""
    user = os.environ.get(USER_ENV, "")
    password = os.environ.get(PASSWORD_ENV, "")
    if not user.strip() or not password.strip():
        return None
    return user, password


def _check_basic_auth(header: str | None, user: str, password: str) -> bool:
    """Constant-time check of an 'Authorization: Basic ...' header."""
    if not header or not header.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(header[len("Basic "):].strip(), validate=True)
    except (binascii.Error, ValueError):
        return False
    given_user, sep, given_pass = decoded.partition(b":")
    if not sep:
        return False
    ok_user = hmac.compare_digest(given_user, user.encode("utf-8"))
    ok_pass = hmac.compare_digest(given_pass, password.encode("utf-8"))
    return ok_user and ok_pass


# ---------- path resolution ----------

def _resolve_packet_file(dir_name: str, file_name: str) -> Path | None:
    """Return the real path of an allowed packet file, or None.

    Checks: exact-match dir and file names, the realpath stays under the packets
    root, and the target is a regular file.
    """
    if not _DIR_RE.match(dir_name) or dir_name in (".", ".."):
        return None
    if file_name not in ALLOWED_FILES:
        return None
    root = os.path.realpath(PACKETS_ROOT)
    real = os.path.realpath(os.path.join(root, dir_name, file_name))
    if os.path.commonpath([root, real]) != root:
        return None
    if not os.path.isfile(real):
        return None
    return Path(real)


def _packet_index_html() -> str:
    root = os.path.realpath(PACKETS_ROOT)
    items = []
    if os.path.isdir(root):
        for name in sorted(os.listdir(root)):
            if not _DIR_RE.match(name):
                continue
            pdir = os.path.join(root, name)
            if not os.path.isdir(pdir):
                continue
            files = [f for f in ALLOWED_FILES if _resolve_packet_file(name, f)]
            links = "".join(
                f'<li><a href="/packets/{quote(name)}/{quote(f)}">{html.escape(f)}</a></li>'
                for f in sorted(files)
            )
            items.append(
                f"<li><strong>{html.escape(name)}</strong><ul>{links or '<li>(no files)</li>'}</ul></li>"
            )
    body = "".join(items) if items else "<li>No packets.</li>"
    return (
        "<!DOCTYPE html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        "<title>jobsearch packets</title></head><body>"
        f"<h1>Packets</h1><ul>{body}</ul></body></html>\n"
    )


# ---------- dashboard render (background, coalesced) ----------

# A status write must answer at once, so the re-render runs on a daemon thread.
# Renders are coalesced: while one is in flight, further writes only set
# _render_pending, and the worker runs exactly one follow-up pass when it ends.
_render_guard = threading.Lock()
_render_running = False  # a render worker thread exists and has not exited
_render_pending = False  # a write arrived during a render; one more pass is owed


def _render_worker(db_path: str, dashboard: str) -> None:
    """Render until no write is pending. Every failure is logged; nothing escapes."""
    global _render_running, _render_pending
    while True:
        try:
            dashboard_file = Path(dashboard)
            last_scan_path = dashboard_file.parent / LAST_SCAN_NAME
            last_scan = None
            if last_scan_path.is_file():
                last_scan = json.loads(last_scan_path.read_text(encoding="utf-8"))
            # render_dashboard opens and closes its own Store on db_path, so this
            # thread never shares a SQLite connection with the request thread.
            render_dashboard(db_path, dashboard, last_scan=last_scan)
        except Exception:
            logger.exception("dashboard re-render failed after status write; write kept")
        with _render_guard:
            if not _render_pending:
                _render_running = False
                return
            _render_pending = False


def schedule_dashboard_render(db_path: str, dashboard: str) -> None:
    """Start a background re-render, or mark one follow-up if a render is in flight.

    Never blocks the caller and never raises: the status write is already committed.
    """
    global _render_running, _render_pending
    with _render_guard:
        if _render_running:
            _render_pending = True
            return
        _render_running = True
    try:
        threading.Thread(target=_render_worker, args=(db_path, dashboard),
                         name="dashboard-render", daemon=True).start()
    except Exception:
        with _render_guard:
            _render_running = False
            _render_pending = False
        logger.exception("could not start dashboard render thread; write kept")


# ---------- handler ----------

def _out_of_reach_html(uid: str, kind: str, verdict: dict) -> str:
    """The 409 page for an out-of-reach AI action: why, and a form that reposts with force=1."""
    action = f"/api/postings/{quote(uid, safe='')}/action"
    return (
        "<!DOCTYPE html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<title>Out of reach — jobsearch</title>"
        f"<style>{POSTING_CSS}</style></head><body>"
        "<h1>Out of reach</h1>"
        f"<div class=\"card\"><p>This posting is out of reach: {_esc(_why_text(verdict))}.</p>"
        f'<form class="sform" method="post" action="{_esc(action)}">'
        f'<input type="hidden" name="kind" value="{_esc(kind)}">'
        '<input type="hidden" name="force" value="1">'
        '<button type="submit">Run anyway</button></form></div>'
        f'<p><a href="{_esc(POSTING_PREFIX + quote(uid, safe=""))}">&larr; back to the posting</a></p>'
        "</body></html>\n")


class _Handler(BaseHTTPRequestHandler):
    server_version = "jobsearch-serve"
    sys_version = ""

    def log_message(self, format: str, *args) -> None:  # route access log to logging
        logger.info("%s - %s", self.address_string(), format % args)

    def _send(self, status: int, body: bytes, content_type: str,
              extra: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_text(self, status: int, text: str) -> None:
        self._send(status, text.encode("utf-8"), "text/plain; charset=utf-8")

    def _require_auth(self) -> bool:
        if _check_basic_auth(self.headers.get("Authorization"),
                             self.server.username, self.server.password):
            return True
        self._send(401, b"Unauthorized\n", "text/plain; charset=utf-8",
                   {"WWW-Authenticate": f'Basic realm="{REALM}", charset="UTF-8"'})
        return False

    def _json(self, status: int, body: dict) -> None:
        self._send(status, json.dumps(body).encode("utf-8"), "application/json",
                   {"Cache-Control": "no-store"})

    def do_GET(self) -> None:
        if not self._require_auth():
            return
        split = urlsplit(self.path)
        raw = split.path
        if raw.startswith(API_PREFIX):
            self._api_get_posting(raw[len(API_PREFIX):])
            return
        if raw == SCAN_PATH:
            self._api_scan_status()
            return
        if raw == JOBS_PATH:
            self._api_jobs()
            return
        if raw.startswith(PREVIEW_PREFIX):
            self._serve_preview(raw[len(PREVIEW_PREFIX):], split.query)
            return
        if raw.startswith(POSTING_PREFIX):
            self._serve_posting_page(raw[len(POSTING_PREFIX):], split.query)
            return
        # Unquote once, then validate the decoded path. Encoded slashes or dots
        # become real separators or dots and fail the same checks below.
        path = unquote(raw)
        if "\x00" in path:
            self._send_text(404, "Not Found\n")
            return
        if path == "/":
            self._serve_triage(split.query)
        elif path == "/dashboard":
            self._serve_dashboard()
        elif path == "/stats":
            self._serve_stats()
        elif path == "/stats.ics":
            self._serve_stats_ics()
        elif path == FACTS_PATH:
            self._serve_facts()
        elif path == "/healthz":
            self._serve_healthz()
        elif path == "/packets/":
            self._send(200, _packet_index_html().encode("utf-8"),
                       "text/html; charset=utf-8", {"Cache-Control": "no-cache"})
        elif path.startswith(PACKETS_PREFIX):
            self._serve_packet(path[len(PACKETS_PREFIX):], split.query)
        else:
            self._send_text(404, "Not Found\n")

    # -- triage screen -------------------------------------------------------

    def _serve_triage(self, query: str) -> None:
        """The triage screen, rendered live from the store on every request.

        Counts, rows and the preview all come from the store, so a status write on the
        previous page is visible immediately; that is why this is not the static
        dashboard.html any more.
        """
        filters = triage.parse_filters(parse_qs(query, keep_blank_values=True))
        scan = last_scan_info()
        job = self.server.scan_job.status() if self.server.scan_job else {
            "state": "idle", "enabled": False, "elapsed_s": 0, "error": ""}
        store = Store(self.server.db_path)
        try:
            rows, total = triage.build_rows(store, filters, PACKETS_ROOT)
            counts = triage.build_counts(store, filters)
            hidden = triage.hidden_count(store, filters)
            sources = store.sources()
            stored = store.stats().get("total", 0)
            selected = filters["sel"]
            if not selected and rows:
                selected = rows[0].get("uid") or ""
            preview_ctx = (triage.build_preview(store, selected, filters, PACKETS_ROOT)
                           if selected else None)
            reach_hidden = triage.hidden_reach(store, filters) if filters.get("reach") else 0
            panel = queue_panel(self.server.copilot, store=store)
        finally:
            store.close()
        if preview_ctx is None:
            selected = ""

        busy = job.get("state") in ("queued", "running")
        scan_parts = []
        if scan["sources"]:
            scan_parts.append(f'{scan["sources"]} boards')
        scan_parts.append(f"{stored:,} stored")
        scan_card = {
            "age_phrase": ui.rel_phrase(scan["scanned_at"]) if scan["scanned_at"] else "",
            "sub": " · ".join(scan_parts),
            "stale": bool(scan["scanned_at"]) and not _fresh(scan["scanned_at"]),
            "enabled": bool(job.get("enabled")),
            "busy": busy,
            "progress": f'{job.get("state")} for {job.get("elapsed_s", 0)}s' if busy else "",
        }
        ctx = {
            "filters": {**filters, "sel": selected},
            "rows": rows,
            "total": total,
            "hidden": hidden,
            "reach_hidden": reach_hidden,
            "counts": counts,
            "sources": sources,
            "scan": scan_card,
            "selected": selected,
            "preview": triage.render_preview(preview_ctx) if preview_ctx else "",
            "queue_panel": panel,
        }
        page = ui.page("Job board — triage", triage.render_triage(ctx) + triage.TRIAGE_JS,
                       body_class="screen-triage")
        self._send(200, page.encode("utf-8"), "text/html; charset=utf-8", NO_STORE)

    def _serve_preview(self, rest: str, query: str) -> None:
        """The preview pane alone, for the in-place swap. Same markup as the page's pane."""
        if not rest or "/" in rest:  # one encoded segment only
            self._send_text(404, "Not Found\n")
            return
        uid = unquote(rest)
        if not uid or "\x00" in uid:
            self._send_text(404, "Not Found\n")
            return
        filters = triage.parse_filters(parse_qs(query, keep_blank_values=True))
        store = Store(self.server.db_path)
        try:
            ctx = triage.build_preview(store, uid, filters, PACKETS_ROOT)
        finally:
            store.close()
        if ctx is None:
            self._send_text(404, "Not Found\n")
            return
        self._send(200, triage.render_preview(ctx).encode("utf-8"),
                   "text/html; charset=utf-8", NO_STORE)

    def _api_scan_status(self) -> None:
        job = self.server.scan_job.status() if self.server.scan_job else {
            "state": "idle", "enabled": False, "elapsed_s": 0, "error": ""}
        self._json(200, {"ok": True, "job": job})

    def _api_scan_start(self) -> None:
        """Start a scan. Off unless JOBSEARCH_SERVE_SCAN=1: the scan reaches the network through the CLI child process, one of three
        places the server does."""
        origin = self.headers.get("Origin")
        if origin is not None:
            host = (self.headers.get("Host") or "").lower()
            if urlsplit(origin).netloc.lower() != host:
                self._json(403, {"error": "cross-origin POST refused"})
                return
        raw = self._read_body() or b""
        fields = {}
        try:
            fields = parse_qs(raw.decode("utf-8"), keep_blank_values=True)
        except UnicodeDecodeError:
            fields = {}
        job = self.server.scan_job
        if job is None:  # pragma: no cover - make_server always installs one
            self._json(503, {"error": "scanning is not available on this server"})
            return
        started, why = job.start()
        if not started:
            status = 409 if "already" in why else 503
            if fields.get("back", [""])[0] == "html" or (
                    "text/html" in (self.headers.get("Accept") or "") and
                    fields.get("back", [""])[0] != "json"):
                back = _safe_return(fields.get("return", [""])[0]) or "/"
                self._redirect_with_note(back, why)
                return
            self._json(status, {"ok": False, "error": why, "job": job.status()})
            return
        if fields.get("back", [""])[0] == "json" or not (
                "text/html" in (self.headers.get("Accept") or "").lower()):
            self._json(200, {"ok": True, "job": job.status()})
            return
        back = _safe_return(fields.get("return", [""])[0]) or "/"
        self._send(303, b"", "text/plain; charset=utf-8", {"Location": back})

    def _serve_facts(self) -> None:
        body = _render_facts(self.server.copilot, time.time())
        self._send(200, body.encode("utf-8"), "text/html; charset=utf-8", NO_STORE)

    def _facts_origin_ok(self) -> bool:
        """The same Origin rule as the other POSTs: a cross-origin browser POST is refused."""
        origin = self.headers.get("Origin")
        if origin is not None:
            host = (self.headers.get("Host") or "").lower()
            if urlsplit(origin).netloc.lower() != host:
                self._send_text(403, "cross-origin POST refused\n")
                return False
        return True

    def _facts_fields(self) -> dict | None:
        raw = self._read_body()
        if raw is None:
            return None
        try:
            return parse_qs(raw.decode("utf-8"), keep_blank_values=True)
        except UnicodeDecodeError:
            return {}

    def _facts_back(self, fields: dict) -> None:
        back = _safe_return(fields.get("return", [""])[0]) or FACTS_PATH
        self._send(303, b"", "text/plain; charset=utf-8", {"Location": back})

    def _api_facts_post(self, rest: str) -> None:
        if rest == "propose":
            self._api_facts_propose()
            return
        parts = rest.split("/")
        if len(parts) == 2 and parts[0] and parts[1] in ("accept", "reject"):
            if parts[1] == "accept":
                self._api_facts_accept(unquote(parts[0]))
            else:
                self._api_facts_reject(unquote(parts[0]))
            return
        self._send_text(404, "Not Found\n")

    def _api_facts_propose(self) -> None:
        if not self._facts_origin_ok():
            return
        fields = self._facts_fields()
        if fields is None:
            return
        instruction = fields.get("instruction", [""])[0].strip()
        if not instruction:
            self._send_text(400, "Say what should change, in a sentence or two.\n")
            return
        if len(instruction) > FACTS_INSTRUCTION_LIMIT:
            self._send_text(400, f"Keep the instruction to {FACTS_INSTRUCTION_LIMIT} characters or fewer.\n")
            return
        cop = self.server.copilot
        if cop is None:
            self._send_text(503, "AI copilot is not running on this server\n")
            return
        try:
            cop.submit(copilot.FACTS_UID, copilot.FACTS_KIND, instruction=instruction)
        except ValueError as exc:  # e.g. over copilot's own length cap; submit dedupes otherwise
            self._send_text(400, f"{exc}\n")
            return
        self._facts_back(fields)

    def _api_facts_accept(self, jid: str) -> None:
        if not self._facts_origin_ok():
            return
        fields = self._facts_fields()
        if fields is None:
            return
        cop = self.server.copilot
        if cop is None:
            self._send_text(503, "AI copilot is not running on this server\n")
            return
        if not jid or "\x00" in jid:
            self._send_text(404, "Not Found\n")
            return
        seen = fields.get("seen_sha256", [""])[0].strip()
        try:
            cop.accept_facts(jid, seen)
        except KeyError:
            self._send_text(404, "No such facts update.\n")
            return
        except (copilot.FactsStaleError, copilot.FactsStateError):
            self._send_text(409, "The facts or preferences file changed since this was proposed, "
                                 "so nothing was written. Propose the change again.\n")
            return
        except Exception:  # never leak a traceback to the browser
            logger.exception("facts accept failed for %s", jid)
            self._send_text(500, "The change could not be applied. The server log has details.\n")
            return
        self._facts_back(fields)

    def _api_facts_reject(self, jid: str) -> None:
        if not self._facts_origin_ok():
            return
        fields = self._facts_fields()
        if fields is None:
            return
        cop = self.server.copilot
        if cop is None:
            self._send_text(503, "AI copilot is not running on this server\n")
            return
        if not jid or "\x00" in jid:
            self._send_text(404, "Not Found\n")
            return
        try:
            cop.reject_facts(jid)
        except KeyError:
            self._send_text(404, "No such facts update.\n")
            return
        except copilot.FactsStateError:
            self._send_text(409, "That proposal was already decided. Nothing was changed.\n")
            return
        self._facts_back(fields)

    def _redirect_with_note(self, back: str, note: str) -> None:
        """303 back with the reason in the query, so a no-JS user still sees why."""
        sep = "&" if "?" in back else "?"
        location = f"{back}{sep}scan_error={quote(note[:120])}"
        self._send(303, b"", "text/plain; charset=utf-8", {"Location": location})

    def _serve_dashboard(self) -> None:
        path = Path(DASHBOARD_PATH)
        if not path.is_file():
            self._send_text(404, "Not Found\n")
            return
        self._send(200, path.read_bytes(), "text/html; charset=utf-8",
                   {"Cache-Control": "no-store"})

    def _serve_healthz(self) -> None:
        path = Path(DASHBOARD_PATH)
        if path.is_file():
            mtime = path.stat().st_mtime
            body = {"ok": True, "dashboard_mtime": int(mtime),
                    "age_s": max(0, int(time.time() - mtime))}
            status = 200
        else:
            body = {"ok": False, "dashboard_mtime": None, "age_s": None}
            status = 503
        self._send(status, json.dumps(body).encode("utf-8"),
                   "application/json", {"Cache-Control": "no-store"})

    def _serve_packet(self, rest: str, query: str = "") -> None:
        """One packet file: inline for a look, as an attachment for ?download=1."""
        parts = rest.split("/")
        if len(parts) != 2:
            self._send_text(404, "Not Found\n")
            return
        dir_name, file_name = parts
        real = _resolve_packet_file(dir_name, file_name)
        if real is None:
            self._send_text(404, "Not Found\n")
            return
        headers = {"Cache-Control": "private, max-age=3600"}
        if parse_qs(query).get("download", [""])[0].lower() in ("1", "true", "yes"):
            headers["Content-Disposition"] = (
                f'attachment; filename="{file_name.replace(chr(34), "")}"')
        self._send(200, real.read_bytes(), ALLOWED_FILES[file_name], headers)

    # -- postings API --------------------------------------------------------

    def _api_get_posting(self, rest: str) -> None:
        parts = rest.split("/")
        uid = unquote(parts[0]) if len(parts) == 1 else ""
        if not uid or "\x00" in uid:
            self._json(404, {"error": "not found"})
            return
        store = Store(self.server.db_path)
        try:
            posting = store.get(uid)
            if posting is None:
                self._json(404, {"error": "unknown uid", "uid": uid})
                return
            events = store.events(uid)
        finally:
            store.close()
        packet_dir = Path(PACKETS_ROOT) / packet_dir_name(uid)
        name = packet_dir_name(uid)
        in_root = bool(_DIR_RE.match(name)) and name not in (".", "..")
        verdict = _reach_for(posting)
        analysis = _read_text(_resolve_packet_file(name, "analysis.md"))
        blockers = _blockers(posting, posting.get("description") or "", verdict, analysis,
                             _match.load_preferences())
        body = {
            "uid": uid,
            "company": posting.get("company"),
            "title": posting.get("title"),
            "location": posting.get("location"),
            "url": posting.get("url"),
            "status": posting.get("status"),
            "score": posting.get("score"),
            "variant": posting.get("variant"),
            "live_status": posting.get("live_status"),
            "live_checked_at": posting.get("live_checked_at"),
            "follow_up_at": posting.get("follow_up_at"),
            "packet": {
                "dir": name,
                "has_resume": in_root and (packet_dir / PACKET_FILE_RESUME).is_file(),
                "has_letter": in_root and (packet_dir / PACKET_FILE_LETTER).is_file(),
            },
            "events": events,
            "latest_job": _with_queue_place(
                self.server.copilot.latest(uid) if self.server.copilot is not None else None,
                uid, self.server.copilot),
            "analysis": analysis,
            "critique": _read_text(_resolve_packet_file(name, "critique.md")),
            "gaps": _read_text(_resolve_packet_file(name, "gaps.md")),
            "reach": verdict,
            "apply_gate": apply_gate(blockers),
            "queue": queue_state(uid, self.server.copilot)["view"],
        }
        self._json(200, body)

    def _api_jobs(self) -> None:
        copilot = self.server.copilot
        if copilot is None:
            self._json(200, {"ok": True, "jobs": [], "running": None, "queued": 0})
            return
        items, running, queued = derive_queue(copilot.jobs(), time.time())
        newest = list(reversed(items))[:JOBS_LIMIT]
        uids = [j.get("uid") for j in newest] + ([running.get("uid")] if running else [])
        store = Store(self.server.db_path)
        try:
            names = _names(store, uids)
        finally:
            store.close()
        jobs = [_annotate(j, names) for j in newest]
        self._json(200, {"ok": True, "jobs": jobs,
                        "running": _annotate(running, names) if running else None,
                        "queued": queued})

    def _serve_posting_page(self, rest: str, query: str = "") -> None:
        parts = rest.split("/")
        uid = unquote(parts[0]) if len(parts) == 1 else ""
        if not uid or "\x00" in uid:
            self._send_text(404, "Not Found\n")
            return
        fields = parse_qs(query, keep_blank_values=True)
        filters = triage.parse_filters(fields)
        back = _safe_return(filters.get("back") or "") or triage.list_url(
            {**filters, "sel": uid})
        step = str((fields.get("step") or [""])[0] or "")
        store = Store(self.server.db_path)
        try:
            posting = store.get(uid)
            if posting is None:
                self._send_text(404, "Not Found\n")
                return
            events = store.events(uid)
            nxt = triage.next_uid(store, uid, filters)
        finally:
            store.close()
        ctx = workspace_context(posting, events, self.server.copilot, back_url=back,
                                step=step)
        # Where a skip or an apply lands: the next row of the same filtered list, else the
        # list itself. The decision moves the queue instead of dropping the reader back into it.
        ctx["return_url"] = (POSTING_PREFIX + quote(nxt, safe="") + "?step=" + step
                             if nxt else back)
        body = workspace.render_workspace(ctx)
        page = ui.page(
            f"{posting.get('company') or 'posting'} — job board",
            body + POSTING_JS,
            body_class="screen-workspace",
            head=NOSCRIPT_REFRESH if ctx["busy"] else "",
            body_attrs=f'data-uid="{html.escape(uid, quote=True)}"',
        )
        self._send(200, page.encode("utf-8"), "text/html; charset=utf-8", NO_STORE)

    def _read_body(self) -> bytes | None:
        """Read a bounded request body, or send an error and return None."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._json(400, {"error": "invalid Content-Length"})
            return None
        if length < 0:
            self._json(400, {"error": "invalid Content-Length"})
            return None
        if length > MAX_BODY:
            self._json(413, {"error": "request body too large", "max_bytes": MAX_BODY})
            return None
        return self.rfile.read(length)

    def _parse_status_body(self, body: bytes) -> tuple[object, object, str] | None:
        """Return (status, note, back) from a form or JSON body, or send 400/415 and return None."""
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype == "application/x-www-form-urlencoded":
            try:
                fields = parse_qs(body.decode("utf-8"), keep_blank_values=True)
            except UnicodeDecodeError:
                self._json(400, {"error": "body is not valid UTF-8"})
                return None
            return (fields.get("status", [""])[0], fields.get("note", [""])[0],
                    _safe_return(fields.get("return", [""])[0]))
        if ctype == "application/json":
            try:
                data = json.loads(body.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                self._json(400, {"error": "body is not valid JSON"})
                return None
            if not isinstance(data, dict):
                self._json(400, {"error": "JSON body must be an object"})
                return None
            note = data.get("note", "")
            if note is None:
                note = ""
            return data.get("status"), note, _safe_return(data.get("return"))
        self._json(415, {"error": "Content-Type must be application/x-www-form-urlencoded or application/json"})
        return None

    def _api_set_variant(self, uid: str) -> None:
        """Set the resume variant for the next packet build (form -> 303, JSON -> 200)."""
        origin = self.headers.get("Origin")
        if origin is not None:
            host = (self.headers.get("Host") or "").lower()
            if urlsplit(origin).netloc.lower() != host:
                self._json(403, {"error": "cross-origin POST refused"})
                return
        body = self._read_body()
        if body is None:
            return
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        back = ""
        if ctype == "application/x-www-form-urlencoded":
            try:
                fields = parse_qs(body.decode("utf-8"), keep_blank_values=True)
            except UnicodeDecodeError:
                self._json(400, {"error": "body is not valid UTF-8"})
                return
            variant = fields.get("variant", [""])[0]
            back = _safe_return(fields.get("return", [""])[0])
        elif ctype == "application/json":
            try:
                data = json.loads(body.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                self._json(400, {"error": "body is not valid JSON"})
                return
            if not isinstance(data, dict):
                self._json(400, {"error": "JSON body must be an object"})
                return
            variant = data.get("variant")
            back = _safe_return(data.get("return"))
        else:
            self._json(415, {"error": "Content-Type must be application/x-www-form-urlencoded or application/json"})
            return
        if not isinstance(variant, str) or variant not in VARIANTS:
            self._json(400, {"error": "unknown variant", "variant": variant,
                             "allowed": list(VARIANTS)})
            return
        if not uid or "\x00" in uid:
            self._json(404, {"error": "not found"})
            return
        store = Store(self.server.db_path)
        try:
            try:
                store.set_variant(uid, variant)
            except KeyError:
                self._json(404, {"error": "unknown uid", "uid": uid})
                return
        finally:
            store.close()
        schedule_dashboard_render(self.server.db_path, str(DASHBOARD_PATH))
        if "text/html" in (self.headers.get("Accept") or "").lower():
            location = back or (POSTING_PREFIX + quote(uid, safe="") + "?step=packet")
            self._send(303, b"", "text/plain; charset=utf-8", {"Location": location})
        else:
            self._json(200, {"ok": True, "uid": uid, "variant": variant})

    def _api_set_status(self, uid: str) -> None:
        origin = self.headers.get("Origin")
        if origin is not None:
            host = (self.headers.get("Host") or "").lower()
            if urlsplit(origin).netloc.lower() != host:
                self._json(403, {"error": "cross-origin POST refused"})
                return
        body = self._read_body()
        if body is None:
            return
        parsed = self._parse_status_body(body)
        if parsed is None:
            return
        status, note, back = parsed
        if not isinstance(status, str) or status not in STATUSES:
            self._json(400, {"error": "unknown status", "status": status,
                             "allowed": list(STATUSES)})
            return
        if not isinstance(note, str):
            self._json(400, {"error": "note must be a string"})
            return
        if len(note) > MAX_NOTE:
            self._json(400, {"error": "note too long", "max_chars": MAX_NOTE})
            return
        if not uid or "\x00" in uid:
            self._json(404, {"error": "not found"})
            return
        store = Store(self.server.db_path)
        try:
            if store.get(uid) is None:
                self._json(404, {"error": "unknown uid", "uid": uid})
                return
            try:
                store.set_status(uid, status, note)
            except KeyError:
                self._json(404, {"error": "unknown uid", "uid": uid})
                return
            event_count = len(store.events(uid))
        finally:
            store.close()
        schedule_dashboard_render(self.server.db_path, str(DASHBOARD_PATH))
        if "text/html" in (self.headers.get("Accept") or "").lower():
            # Back where the form was: the triage list when it sent a return path,
            # otherwise the posting's own page, which reflects the write immediately.
            location = back or (POSTING_PREFIX + quote(uid, safe=""))
            self._send(303, b"", "text/plain; charset=utf-8", {"Location": location})
        else:
            self._json(200, {"ok": True, "uid": uid, "status": status,
                             "event_count": event_count,
                             "allowed_next": list(ALLOWED_NEXT.get(status, ()))})

    def _api_set_follow_up(self, uid: str) -> None:
        """Set (days=N or date=YYYY-MM-DD) or clear (clear=1) a posting's follow-up date.

        Form or JSON body. Form -> 303 back to 'return' (or the posting's submit step);
        anything else -> 200 JSON. Errors are JSON, never redirects.
        """
        origin = self.headers.get("Origin")
        if origin is not None:
            host = (self.headers.get("Host") or "").lower()
            if urlsplit(origin).netloc.lower() != host:
                self._json(403, {"error": "cross-origin POST refused"})
                return
        body = self._read_body()
        if body is None:
            return
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype == "application/x-www-form-urlencoded":
            try:
                raw = parse_qs(body.decode("utf-8"), keep_blank_values=True)
            except UnicodeDecodeError:
                self._json(400, {"error": "body is not valid UTF-8"})
                return
            fields = {k: v[0] for k, v in raw.items()}
        elif ctype == "application/json" or (not ctype and not body.strip()):
            try:
                fields = json.loads(body.decode("utf-8")) if body.strip() else {}
            except (ValueError, UnicodeDecodeError):
                self._json(400, {"error": "body is not valid JSON"})
                return
            if not isinstance(fields, dict):
                self._json(400, {"error": "JSON body must be an object"})
                return
        else:
            self._json(415, {"error": "send a form or JSON body"})
            return

        def field(name: str) -> str:
            value = fields.get(name)
            return "" if value is None else str(value).strip()

        back = _safe_return(fields.get("return"))
        if field("clear").lower() in ("1", "true", "yes", "on"):
            value: str | None = None
        elif field("date"):
            text = field("date")
            try:
                if not _FOLLOW_UP_DATE.fullmatch(text):
                    raise ValueError(text)
                value = datetime.strptime(text, "%Y-%m-%d").date().isoformat()
            except ValueError:
                self._json(400, {"error": "date must be YYYY-MM-DD", "got": text})
                return
        elif field("days"):
            try:
                days = int(field("days"))
            except ValueError:
                days = 0
            if not 1 <= days <= FOLLOW_UP_MAX_DAYS:
                self._json(400, {"error": "days must be a whole number from 1 to "
                                          f"{FOLLOW_UP_MAX_DAYS}"})
                return
            value = (date.today() + timedelta(days=days)).isoformat()
        else:
            self._json(400, {"error": "give days=N, date=YYYY-MM-DD or clear=1"})
            return
        if not uid or "\x00" in uid:
            self._json(404, {"error": "not found"})
            return
        store = Store(self.server.db_path)
        try:
            if store.get(uid) is None:
                self._json(404, {"error": "unknown uid", "uid": uid})
                return
            try:
                store.set_follow_up(uid, value)
            except KeyError:
                self._json(404, {"error": "unknown uid", "uid": uid})
                return
        finally:
            store.close()
        if "text/html" in (self.headers.get("Accept") or "").lower():
            location = back or (POSTING_PREFIX + quote(uid, safe="") + "?step=submit")
            self._send(303, b"", "text/plain; charset=utf-8", {"Location": location})
        else:
            self._json(200, {"ok": True, "uid": uid, "follow_up_at": value})

    def _serve_stats(self) -> None:
        store = Store(self.server.db_path)
        try:
            funnel = store.funnel_counts()
            variants = store.variant_stats()
        finally:
            store.close()
        # Sessions come from the copilot's own job list, which is where a queued job
        # actually lives; the events table never records one, so reading it would show
        # an always-empty table that looks like a working feature.
        sessions: dict[str, int] = {}
        copilot = self.server.copilot
        if copilot is not None:
            for job in copilot.jobs():
                kind = str(job.get("kind") or "")
                if kind:
                    sessions[kind] = sessions.get(kind, 0) + 1
        self._send(200, _render_stats(funnel, variants, sessions).encode("utf-8"),
                   "text/html; charset=utf-8", {"Cache-Control": "no-store"})

    def _serve_stats_ics(self) -> None:
        store = Store(self.server.db_path)
        try:
            rows = store.due_follow_ups(date.today().isoformat())
        finally:
            store.close()
        text = _render_followups_ics(rows, datetime.now(timezone.utc))
        self._send(200, text.encode("utf-8"), "text/calendar; charset=utf-8",
                   {"Cache-Control": "no-store",
                    "Content-Disposition": 'attachment; filename="follow-ups.ics"'})

    def _api_check_liveness(self, uid: str) -> None:
        """Fetch the posting URL once and record live/gone/unknown (form -> 303, JSON -> 200).

        The network call runs with no database connection open, so a slow site never
        holds the store. The result is written even when it is 'unknown': the event log
        then shows that a check was attempted and why it was inconclusive.
        """
        origin = self.headers.get("Origin")
        if origin is not None:
            host = (self.headers.get("Host") or "").lower()
            if urlsplit(origin).netloc.lower() != host:
                self._json(403, {"error": "cross-origin POST refused"})
                return
        body = self._read_body()
        if body is None:
            return
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype == "application/x-www-form-urlencoded":
            try:
                fields = parse_qs(body.decode("utf-8"), keep_blank_values=True)
            except UnicodeDecodeError:
                self._json(400, {"error": "body is not valid UTF-8"})
                return
            back = _safe_return(fields.get("return", [""])[0])
        elif ctype == "application/json":
            try:
                data = json.loads(body.decode("utf-8")) if body.strip() else {}
            except (ValueError, UnicodeDecodeError):
                self._json(400, {"error": "body is not valid JSON"})
                return
            if not isinstance(data, dict):
                self._json(400, {"error": "JSON body must be an object"})
                return
            back = _safe_return(data.get("return"))
        else:
            self._json(415, {"error": "Content-Type must be application/x-www-form-urlencoded "
                                      "or application/json"})
            return
        if not uid or "\x00" in uid:
            self._json(404, {"error": "unknown uid", "uid": uid})
            return
        store = Store(self.server.db_path)
        try:
            posting = store.get(uid)
        finally:
            store.close()
        if posting is None:
            self._json(404, {"error": "unknown uid", "uid": uid})
            return
        result = sources.check_liveness(posting.get("url") or "")
        store = Store(self.server.db_path)
        try:
            store.set_liveness(uid, result["status"], result["reason"], result["checked_at"])
        except KeyError:
            self._json(404, {"error": "unknown uid", "uid": uid})
            return
        finally:
            store.close()
        if "text/html" in (self.headers.get("Accept") or "").lower():
            location = back or (POSTING_PREFIX + quote(uid, safe="") + "?step=submit")
            self._send(303, b"", "text/plain; charset=utf-8", {"Location": location})
        else:
            self._json(200, {"ok": True, "uid": uid, "live_status": result["status"],
                             "http_code": result["http_code"], "reason": result["reason"],
                             "checked_at": result["checked_at"]})

    def _parse_action_body(self, body: bytes) -> tuple[object, object, object] | None:
        """Return (kind, force, questions) from a form or JSON body, or 400/415 and None.

        questions is only read by the questions kind; for other kinds it is ignored.
        """
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype == "application/x-www-form-urlencoded":
            try:
                fields = parse_qs(body.decode("utf-8"), keep_blank_values=True)
            except UnicodeDecodeError:
                self._json(400, {"error": "body is not valid UTF-8"})
                return None
            return (fields.get("kind", [""])[0], fields.get("force", [""])[0],
                    fields.get("questions", [""])[0])
        if ctype == "application/json":
            try:
                data = json.loads(body.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                self._json(400, {"error": "body is not valid JSON"})
                return None
            if not isinstance(data, dict):
                self._json(400, {"error": "JSON body must be an object"})
                return None
            return (data.get("kind"), data.get("force"), data.get("questions"))
        self._json(415, {"error": "Content-Type must be application/x-www-form-urlencoded or application/json"})
        return None

    def _api_action(self, uid: str) -> None:
        origin = self.headers.get("Origin")
        if origin is not None:
            host = (self.headers.get("Host") or "").lower()
            if urlsplit(origin).netloc.lower() != host:
                self._json(403, {"error": "cross-origin POST refused"})
                return
        body = self._read_body()
        if body is None:
            return
        parsed = self._parse_action_body(body)
        if parsed is None:
            return
        kind, force_raw, questions_raw = parsed
        if not isinstance(kind, str) or kind not in ACTION_KINDS:
            self._json(400, {"error": "unknown kind", "allowed": list(ACTION_KINDS)})
            return
        copilot = self.server.copilot
        if copilot is None:
            self._json(503, {"error": "AI copilot is not running on this server"})
            return
        if not uid or "\x00" in uid:
            self._json(404, {"error": "not found"})
            return
        store = Store(self.server.db_path)
        try:
            posting = store.get(uid)
        finally:
            store.close()
        if posting is None:
            self._json(404, {"error": "unknown uid", "uid": uid})
            return
        html_caller = "text/html" in (self.headers.get("Accept") or "").lower()
        questions = None
        if kind == "questions":
            if questions_raw is not None and not isinstance(questions_raw, str):
                self._json(400, {"error": "questions must be text"})
                return
            questions = (questions_raw or "").replace("\r\n", "\n")
            if not questions.strip():
                self._json(400, {"error": "questions are required for the questions kind"})
                return
            if len(questions) > QUESTIONS_MAX_CHARS:
                self._json(400, {"error": "questions are too long",
                                 "max_chars": QUESTIONS_MAX_CHARS})
                return
        if kind != PREP_KIND and not _truthy_force(force_raw):
            verdict = _reach_for(posting)
            if verdict["verdict"] == "out_of_reach":
                if html_caller:
                    self._send(409, _out_of_reach_html(uid, kind, verdict).encode("utf-8"),
                               "text/html; charset=utf-8", NO_STORE)
                else:
                    self._json(409, {"error": "out of reach", "uid": uid, "reach": verdict})
                return
        if questions is not None:
            try:
                save_questions(copilot.packet_root, uid, questions)
            except OSError as exc:
                self._json(500, {"error": f"could not save the questions: {exc}"})
                return
        try:
            job = copilot.submit(uid, kind)
        except KeyError:
            self._json(404, {"error": "unknown uid", "uid": uid})
            return
        if html_caller:
            location = POSTING_PREFIX + quote(uid, safe="")
            self._send(303, b"", "text/plain; charset=utf-8", {"Location": location})
        else:
            self._json(200, {"ok": True, "job": job,
                             "queue": queue_state(uid, copilot)["view"]})

    def _api_body_checks(self, uid: str) -> None:
        """The letter checks for a body that has not been saved yet (read-only, no write).

        Same validator the server renders with, so the live rows can never disagree
        with the rows the page shows after a reload.
        """
        origin = self.headers.get("Origin")
        if origin is not None:
            host = (self.headers.get("Host") or "").lower()
            if urlsplit(origin).netloc.lower() != host:
                self._json(403, {"error": "cross-origin POST refused"})
                return
        raw = self._read_body()
        if raw is None:
            return
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        text = ""
        if ctype == "application/x-www-form-urlencoded":
            try:
                fields = parse_qs(raw.decode("utf-8"), keep_blank_values=True)
            except UnicodeDecodeError:
                self._json(400, {"error": "body is not valid UTF-8"})
                return
            text = fields.get("body", [""])[0]
        elif ctype == "application/json":
            try:
                data = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                self._json(400, {"error": "body is not valid JSON"})
                return
            if not isinstance(data, dict) or not isinstance(data.get("body", ""), str):
                self._json(400, {"error": "JSON body must be an object with a body string"})
                return
            text = data.get("body", "")
        else:
            self._json(415, {"error": "Content-Type must be application/x-www-form-urlencoded or application/json"})
            return
        store = Store(self.server.db_path)
        try:
            posting = store.get(uid) if uid and "\x00" not in uid else None
        finally:
            store.close()
        if posting is None:
            self._json(404, {"error": "unknown uid", "uid": uid})
            return
        checks = _letter_checks(text, posting.get("description") or "",
                                company=posting.get("company"),
                                resume_text=_resume_text(packet_dir_name(uid)))
        self._json(200, {"ok": True, "words": len(text.split()), "checks": checks})

    def _api_body(self, uid: str) -> None:
        origin = self.headers.get("Origin")
        if origin is not None:
            host = (self.headers.get("Host") or "").lower()
            if urlsplit(origin).netloc.lower() != host:
                self._json(403, {"error": "cross-origin POST refused"})
                return
        raw = self._read_body()
        if raw is None:
            return
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/x-www-form-urlencoded":
            self._json(415, {"error": "Content-Type must be application/x-www-form-urlencoded"})
            return
        try:
            fields = parse_qs(raw.decode("utf-8"), keep_blank_values=True)
        except UnicodeDecodeError:
            self._json(400, {"error": "body is not valid UTF-8"})
            return
        text = fields.get("body", [""])[0]
        if not uid or "\x00" in uid:
            self._json(404, {"error": "not found"})
            return
        copilot = self.server.copilot
        if copilot is None:
            self._json(503, {"error": "AI copilot is not running on this server"})
            return
        store = Store(self.server.db_path)
        try:
            known = store.get(uid) is not None
        finally:
            store.close()
        if not known:
            self._json(404, {"error": "unknown uid", "uid": uid})
            return
        try:
            pdf = copilot.publish_body(uid, text)
        except ValueError as exc:  # empty or non-ASCII: nothing was written
            self._json(400, {"error": str(exc)})
            return
        except KeyError:
            self._json(404, {"error": "unknown uid", "uid": uid})
            return
        except ArtifactError as exc:  # render or ligature check failed: accepted files kept
            self._json(422, {"error": str(exc)})
            return
        schedule_dashboard_render(self.server.db_path, str(DASHBOARD_PATH))
        if "text/html" in (self.headers.get("Accept") or "").lower():
            self._send(303, b"", "text/plain; charset=utf-8",
                       {"Location": POSTING_PREFIX + quote(uid, safe="")})
        else:
            self._json(200, {"ok": True, "path": str(pdf)})

    def do_POST(self) -> None:
        if not self._require_auth():
            return
        raw = urlsplit(self.path).path
        if raw == SCAN_PATH:
            self._api_scan_start()
            return
        if raw.startswith(FACTS_API):
            self._api_facts_post(raw[len(FACTS_API):])
            return
        if raw.startswith(API_PREFIX):
            parts = raw[len(API_PREFIX):].split("/")
            if len(parts) == 2 and parts[1] == "status":
                self._api_set_status(unquote(parts[0]))
                return
            if len(parts) == 2 and parts[1] == "action":
                self._api_action(unquote(parts[0]))
                return
            if len(parts) == 2 and parts[1] == "body":
                self._api_body(unquote(parts[0]))
                return
            if len(parts) == 2 and parts[1] == "variant":
                self._api_set_variant(unquote(parts[0]))
                return
            if len(parts) == 2 and parts[1] == "checks":
                self._api_body_checks(unquote(parts[0]))
                return
            if len(parts) == 2 and parts[1] == "liveness":
                self._api_check_liveness(unquote(parts[0]))
                return
            if len(parts) == 2 and parts[1] == "followup":
                self._api_set_follow_up(unquote(parts[0]))
                return
        self._send(405, b"Method Not Allowed\n", "text/plain; charset=utf-8", {"Allow": "GET"})

    # -- other write methods ---------------------------------------------------

    def _method_not_allowed(self) -> None:
        if not self._require_auth():
            return
        self._send(405, b"Method Not Allowed\n", "text/plain; charset=utf-8", {"Allow": "GET"})

    do_PUT = _method_not_allowed
    do_DELETE = _method_not_allowed
    do_PATCH = _method_not_allowed


class ServeHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, username: str, password: str, db_path: str,
                 copilot: "Copilot | None" = None):
        self.username = username
        self.password = password
        self.db_path = db_path
        self.copilot = copilot  # one shared instance; None disables the action POST
        self.scan_job = ScanJob(REPO_ROOT, db_path)
        _backfill_levels(db_path)  # once at boot; a missing DB is skipped, never fatal
        super().__init__(address, _Handler)


def make_server(host: str, port: int, username: str, password: str,
                db_path: str | None = None, copilot: "Copilot | None" = None) -> ServeHTTPServer:
    """Build (but do not run) the server. Used by main() and by the tests."""
    if not username.strip() or not password.strip():
        raise ValueError("username and password must be non-empty")
    return ServeHTTPServer((host, port), username, password, db_path or default_db_path(),
                           copilot=copilot)


# ---------- CLI ----------

def _err(msg: str) -> None:
    print(msg, file=sys.stderr)


def _port(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected an integer, got {text!r}")
    if not 0 <= value <= 65535:
        raise argparse.ArgumentTypeError(f"must be 0-65535, got {value}")
    return value


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="jobsearch-serve",
        description="HTTP Basic-auth web UI for the jobsearch dashboard, packets and posting workspace.",
    )
    p.add_argument("--host", default=DEFAULT_HOST,
                   help=f"bind address (default: {DEFAULT_HOST})")
    p.add_argument("--port", type=_port, default=DEFAULT_PORT,
                   help=f"bind port (default: {DEFAULT_PORT}; 0 picks a free port)")
    p.add_argument("--db", default=default_db_path(),
                   help="jobsearch SQLite database (default: $JOBS_DB or ~/jobsearch/jobsearch.db)")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    creds = _credentials_from_env()
    if creds is None:
        _err(f"refusing to start: {USER_ENV} and {PASSWORD_ENV} must both be set and non-empty")
        return EX_CONFIG
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        stream=sys.stderr)
    copilot = Copilot(db_path=args.db)  # recovers interrupted jobs in its constructor
    copilot.start()
    try:
        server = make_server(args.host, args.port, *creds, db_path=args.db, copilot=copilot)
    except OSError as exc:
        copilot.stop()
        _err(f"error: cannot bind {args.host}:{args.port}: {exc}")
        return 1
    host, port = server.server_address[:2]
    logger.info("serving on http://%s:%s (auth: Basic, user from %s)", host, port, USER_ENV)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        copilot.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
