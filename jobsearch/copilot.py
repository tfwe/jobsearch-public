"""Single-lane AI job worker: stage a posting bundle, call the neon CLI, validate, record.

Flow per job: queued -> running -> done|failed. One daemon thread runs jobs one at a
time (the model CLI allows one call at a time). Bundles and answers stay on disk under
state_dir/<job id>/ for inspection; nothing is deleted.

The model CLI is read from COPILOT_NEON_CMD (default: `neon` on PATH). Any executable
with the same calling convention works; the repository ships a no-model stub at
tools/stub-model-cli.sh. Tests point it at a fake script; they never run a real binary.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import yaml

from jobsearch import facts_ops
from jobsearch.letter import render_letter
from jobsearch.store import Store, default_db_path

KINDS = ("letter", "analysis", "questions")  # AI kinds: each stages a bundle and calls the model
ALIASES = {"critique": "analysis", "gaps": "analysis"}  # retired kind names -> current kind
PREP_KIND = "prep"  # no-AI: runs `jobsearch.cli prep UID` as a subprocess; stages nothing
ACTION_KINDS = (PREP_KIND,) + KINDS + tuple(ALIASES)
EST_SECONDS = {"analysis": 200, "letter": 200, "prep": 30, "questions": 150, "facts_update": 150}  # wall time, UI
FACTS_KIND = "facts_update"  # facts/preferences edit: propose, then accept or reject; not in KINDS or ACTION_KINDS
FACTS_UID = "__facts__"  # sentinel uid for facts_update jobs, which have no posting
FACTS_INSTRUCTION_MAX = 1000  # characters
FACTS_CHECKS = tuple(os.environ.get("JOBS_FACTS_CHECKS", "").split())  # optional checkers
FACTS_CHECK_TIMEOUT_S = 60
FACTS_PREFS_ABSENT = ("(preferences.yaml does not exist yet: treat it as version: 1 with nothing set)")
QUESTIONS_MAX_CHARS = 8000  # the questions field the action route accepts
QUESTIONS_FILE = "questions.txt"  # in the packet dir (written by the route) and in the bundle
QUESTION_HEADING = re.compile(r"^\*\*[^*]+\*\*$")  # a question heading: bold, one line
ANALYSIS_HEADINGS = ("VERDICT", "FIT", "GAPS", "TO_BE_PERFECT")
ANALYSIS_MIN_WORDS = 120  # validation floor and ceiling; the prompt asks for 250-500
ANALYSIS_MAX_WORDS = 700
STAGE_LINE = re.compile(r"^# stage (\S+(?: \S+)*) ([0-9.]+)s\s*$")  # finished-stage lines the dispatcher prints on stderr
STAGE_START_LINE = re.compile(r"^# stage (\S+(?: \S+)*) start\s*$")  # a step that has begun; no seconds yet
RUN_LOG = "run.log"  # raw stderr lines of every dispatch call, kept in the bundle directory
PROJECT_ROOT = Path(__file__).resolve().parents[1]
PREP_TIMEOUT_S = 600
DEFAULT_NEON_CMD = "neon"
DEFAULT_FACTS_PATH = str(Path.home() / "resume" / "facts.yaml")
RATE_LIMIT_EXIT = 5          # quota governor: 60s minimum gap not yet elapsed
RATE_LIMIT_SLEEP_S = 65
CALL_TIMEOUT_S = 1800
MAX_CHARS = "200000"
FRENCH_TOKEN = "FRENCH_BLOCKER"
GREETING_STARTS = ("Dear", "Hello", "Sincerely", "Best regards")
LIGATURE_RANGE = range(0xFB00, 0xFB07)  # U+FB00..U+FB06: rejected in letter bodies and PDF text
ARTIFACT_NAMES = {"critique": "critique.md", "gaps": "gaps.md", "analysis": "analysis.md",
                  "questions": "answers.md"}  # old names kept readable
PREP_MODULE_ARGS = ["-m", "jobsearch.cli", "prep"]
NUMERIC_TOKEN = re.compile(r"\$?\d[\d,]*(?:\.\d+)?%?")  # letter numbers must appear in facts.yaml
PROJECT_NAME_MIN_LEN = 10  # shorter project names are too generic to police
PAREN_GROUP = re.compile(r"\([^()]*\)")  # innermost parenthesised group
LETTER_PREP_FROM = ("new", "shortlisted", "prepped")  # letter render may only move a posting to 'prepped' from these

PROMPTS = {
    "letter": "\n".join([
        "Write a first-person cover letter body for the applicant applying to the role in posting.md.",
        "GROUNDING RULE: facts.yaml is the ONLY admissible source of career claims. Never invent a metric, employer, date, project, skill, or certification not explicitly in facts.yaml. Every number you write must appear verbatim in facts.yaml.",
        "LANGUAGE RULE: never claim a language, and never write in a language, that facts.yaml does not list for the applicant. If posting.md requires a language the applicant does not have, output only the token FRENCH_BLOCKER, a colon, then the exact offending quote from posting.md, and stop.",
        "VOICE AND FRAMING RULES:",
        "- Write for a recruiter or hiring manager who has never seen the applicant's GitHub or personal repos. Do NOT copy resume shorthand or bullet phrasing from resume.txt; use resume.txt only to see which work the applicant highlights for this track.",
        "- BANNED: never use the applicant's internal project or repository titles as proper nouns, and never use parenthetical technology lists such as (Python, SQLite, pytest). Keep real third-party proper nouns only: employers, universities, certifications, models, datasets, frameworks, and the target company.",
        "- WHAT TO DO INSTEAD: pick the two or three pieces of work from facts.yaml that best match posting.md, and for each one describe in plain prose the problem, the approach with the tools woven into the sentence, and the measured outcome using the exact numbers from facts.yaml. Do not cram every project into one letter.",
        "- OPENING LINE: start on the specific engineering problem or environment described in posting.md and tie it to the role and company. Never open with 'I am applying for' or generic flattery about the mission.",
        "- CLOSING LINE: one concrete, forward-looking sentence connecting verified strengths to what the applicant would deliver on this team. No 'I look forward to hearing from you' or 'Thank you for your consideration'.",
        "FORMAT: 240-320 words, plain ASCII only (no smart quotes, em-dashes, ligatures, markdown, or LaTeX), no greeting and no sign-off, three concise body paragraphs only.",
        "Role: {title} at {company} ({location}).",
    ]),
    "analysis": "\n".join([
        "Assess the applicant's fit for the role in posting.md. Read posting.md, facts.yaml and resume.txt (resume.txt may be absent; skip it then).",
        "GROUNDING RULE: facts.yaml is the ONLY admissible source of career claims. Never invent a metric, employer, date, project, skill, or certification not explicitly in facts.yaml. Every number you write must appear verbatim in facts.yaml.",
        "FORMAT: 250-500 words, plain ASCII only (no smart quotes, em-dashes, markdown, bold, bullets with symbols, or LaTeX). No greeting and no sign-off.",
        "Use exactly these four headings, in this order, each alone on its own line, followed by its content on the lines below it:",
        "VERDICT",
        "One line: strong, stretch, or no chance, then the reason, written in the poster's own terms (quote their words for the key requirement).",
        "FIT",
        "Two or three concrete overlaps between the requirements stated in posting.md and facts.yaml. For each overlap, name where in facts.yaml it comes from (the section and item name).",
        "GAPS",
        "What posting.md asks for that facts.yaml does not prove. Order the list by how much a hiring manager would care, most important first.",
        "TO_BE_PERFECT",
        "The shortest honest path to being an ideal candidate for this posting: what to build or study, how to prove it in public (repository, write-up, or benchmark), and a realistic time cost in weeks or months. Do not invent credentials or claim any certification the applicant does not hold.",
        "Role: {title} at {company} ({location}).",
    ]),
    "questions": "\n".join([
        "Draft answers to the employer's own application questions for the applicant, using posting.md.",
        "The questions are in questions.txt in this bundle. Answer every one of them, in order.",
        "Copy each question exactly as written in questions.txt, as the heading of its answer.",
        "GROUNDING RULE: facts.yaml is the ONLY admissible source of career claims. Never invent",
        "a metric, employer, date, project, skill, or certification not explicitly in facts.yaml.",
        "Every number you write must appear verbatim in facts.yaml. If a question asks for a fact",
        "that facts.yaml does not contain, say so plainly in that answer instead of guessing.",
        "FORMAT: plain ASCII only (no smart quotes, em-dashes, ligatures, or LaTeX). Each answer",
        "is 60-140 words, first person, in plain prose. No greeting and no sign-off.",
        "Head each answer with its question in bold on its own line, like **Question text**,",
        "then the answer paragraph on the lines below it.",
        "Role: {title} at {company} ({location}).",
        "{questions_note}",
    ]),
    "facts_update": "\n".join([
        "You translate one plain-English instruction from the user into a structured edit to their preferences file or their facts file. Your output is machine-read: answer with JSON only.",
        "The instruction, the current facts.yaml and the current preferences.yaml follow at the end of this brief.",
        "GROUNDING RULE: facts.yaml is the ONLY admissible source of career claims. Never invent a metric, employer, date, project, skill, or certification not explicitly in facts.yaml or the instruction. Every number you write must appear verbatim in facts.yaml, preferences.yaml, or the instruction.",
        "SEPARATION RULE: Intent, relocation willingness, and visa sponsorship requirements belong in preferences.yaml only. Never write preferences into facts.yaml and never turn a preference into a career claim.",
        "AMBIGUITY RULE: If the instruction is ambiguous, contradicts an existing fact, or does not fit the allowed paths, return status clarify with a concrete question instead of guessing or inventing a YAML key.",
        "REFUSAL RULE: If the instruction asks to add or alter a project metric, certification, employer, or date that needs evidence not provided, return status refuse and say what evidence or confirmation is missing.",
        "NO-OP RULE: If the instruction is already reflected in the current file, return status no_change with an empty ops list.",
        "ALLOWED PATHS (preferences.yaml): relocation.countries (ISO-2 list), relocation.cities, relocation.exclude_regions, relocation.reason, work_authorization.<CC>.status (authorized|requires_sponsorship|not_authorized), work_authorization.<CC>.reason.",
        "OP RULE: use upsert for any key that is not already in the current file, including every key when the file is empty or does not exist yet; use set only to rewrite a key that is already there; use delete only to remove a key that is there.",
        "ALLOWED PATHS (facts.yaml): contact.location, contact.phone, contact.email.",
        "FORMAT: Plain ASCII only. Answer with ONE raw JSON object and nothing else: no markdown, no code fence, no prose before or after it. Shape:",
        '{{"status":"propose|no_change|clarify|refuse","target":"preferences.yaml|facts.yaml","summary":"1-2 sentences","question":"","reason":"","ops":[{{"op":"set|upsert|delete","path":"...","value":"..."}}]}}',
        "",
        "INSTRUCTION:",
        "{instruction}",
        "",
        "CURRENT facts.yaml:",
        "{facts_text}",
        "CURRENT preferences.yaml:",
        "{prefs_text}",
    ]),
}

NO_QUESTIONS_NOTE = ("NO QUESTIONS WERE SUPPLIED: questions.txt is missing or empty. "
                     "Reply with the single word NO_QUESTIONS and nothing else.")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _slug(uid: str) -> str:
    """Packet directory name for a uid; same mapping as jobsearch.letter.slug_for_uid."""
    return uid.replace(":", "_").replace("/", "_")


def _kind_of(job: dict) -> str:
    """The job's current kind; jobs saved before the rename may still say critique or gaps."""
    return ALIASES.get(job["kind"], job["kind"])


def _last_line(text: str) -> str:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return lines[-1] if lines else ""


def _project_names(facts_text: str) -> list[str]:
    """Names from facts.yaml projects[].name. Empty when the facts text does not parse."""
    try:
        data = yaml.safe_load(facts_text)
    except yaml.YAMLError:
        return []
    projects = data.get("projects") if isinstance(data, dict) else None
    if not isinstance(projects, list):
        return []
    return [p["name"] for p in projects if isinstance(p, dict) and isinstance(p.get("name"), str)]


def _analysis_problems(text: str) -> list[str]:
    """Problems specific to the analysis kind: non-empty, four headings at line start, word count."""
    if not text.strip():
        return ["empty"]
    problems: list[str] = []
    stripped = [ln.strip().upper() for ln in text.splitlines()]
    for heading in ANALYSIS_HEADINGS:
        if heading not in stripped:  # whole stripped line equals the heading (case-insensitive)
            problems.append(f"missing_heading={heading}")
    n = len(text.split())
    if not ANALYSIS_MIN_WORDS <= n <= ANALYSIS_MAX_WORDS:
        problems.append(f"word_count={n}")
    return problems


def _questions_problems(text: str) -> list[str]:
    """Problems specific to the questions kind: non-empty, and at least one answer block.

    An answer block is a bold question heading line followed by a non-empty line before
    the next heading. ASCII is checked for every kind in validate().
    """
    if not text.strip():
        return ["empty"]
    blocks = 0
    open_heading = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if QUESTION_HEADING.match(line):
            open_heading = True
        elif open_heading:
            blocks += 1
            open_heading = False
    return [] if blocks else ["no_answer_block"]


def validate(kind: str, text: str, facts_text: str, posting_text: str) -> tuple[bool, list[str]]:
    """Check a model answer. Returns (ok, problems).

    For kind letter with non-empty facts_text, every numeric token must appear
    verbatim (as a substring) in facts_text. posting_text is accepted for the
    frozen API; no current rule compares against it. Retired kind names validate
    as their ALIASES target.
    """
    kind = ALIASES.get(kind, kind)
    lines = text.splitlines()
    if any(FRENCH_TOKEN in ln for ln in lines):
        return False, [FRENCH_TOKEN]
    problems: list[str] = []
    if any(ord(ch) > 127 for ch in text):
        problems.append("non_ascii")
    if kind == "analysis":
        problems.extend(_analysis_problems(text))
    if kind == "questions":  # ASCII is the generic check above; this adds the other two
        problems.extend(_questions_problems(text))
    if kind == "letter":
        n = len(text.split())
        if not 240 <= n <= 320:
            problems.append(f"word_count={n}")
        if facts_text:
            for tok in dict.fromkeys(NUMERIC_TOKEN.findall(text)):
                tok = tok.rstrip(",")
                if tok not in facts_text:
                    problems.append(f"unverified_metric={tok}")
        lowered = text.lower()
        for name in _project_names(facts_text):
            name = name.strip()
            if len(name) >= PROJECT_NAME_MIN_LEN and name.lower() in lowered:
                problems.append(f"project_proper_noun={name}")
        for group in PAREN_GROUP.findall(text):
            if group.count(",") >= 2:
                problems.append(f"tech_list_parenthetical={group}")
    if any(ln.lstrip().startswith(GREETING_STARTS) for ln in lines):
        problems.append("greeting")
    if "\\" in text or "{" in text or "}" in text or "```" in text:
        problems.append("code_tokens")
    return (not problems), problems


class ArtifactError(RuntimeError):
    """A validated answer could not be turned into its packet artifact."""


def ligature_hits(text: str) -> list[str]:
    """Sorted U+XXXX names of ligature codepoints present in text (empty when clean)."""
    return sorted({f"U+{ord(ch):04X}" for ch in text if ord(ch) in LIGATURE_RANGE})


def _pdf_text(pdf: Path) -> str:
    """Text layer of a compiled PDF. Fails closed: any pdftotext error is an ArtifactError."""
    try:
        proc = subprocess.run(["pdftotext", str(pdf), "-"], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=120)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ArtifactError(f"pdftotext_failed: {exc}") from exc
    if proc.returncode != 0:
        raise ArtifactError(f"pdftotext_failed: exit {proc.returncode}")
    return proc.stdout


def _write_text(path: Path, text: str) -> None:
    """Write via a sibling temp file and rename, so a reader never sees a partial file."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _publish(src: Path, dst: Path) -> None:
    """Copy src beside dst, then rename over dst (works across filesystems)."""
    tmp = dst.with_name(dst.name + ".tmp")
    shutil.copyfile(src, tmp)
    os.replace(tmp, dst)


def save_questions(packet_root, uid: str, text: str) -> Path:
    """Write the employer's application questions into the packet dir; returns the file.

    The action route calls this before queueing a questions job; the worker copies the
    file into the job bundle when it runs.
    """
    target = Path(packet_root) / _slug(uid)
    target.mkdir(parents=True, exist_ok=True)
    path = target / QUESTIONS_FILE
    _write_text(path, text)
    return path


class FactsStaleError(RuntimeError):
    """accept_facts refused: the target file changed since the reviewer saw the preview. Nothing was written."""


class FactsStateError(RuntimeError):
    """A facts action refused: the job is not in a state that action accepts."""


def _file_sha(path: Path) -> str:
    """sha256 of the file's bytes; a missing file hashes as empty."""
    return hashlib.sha256(path.read_bytes() if path.exists() else b"").hexdigest()


def _write_bytes(path: Path, data: bytes) -> None:
    """Byte-exact atomic write, for restoring a file's previous contents."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _one_line(text: str) -> str:
    return " ".join(text.split())


class Copilot:
    def __init__(self, jobs_path=None, packet_root=None, facts_path=None, db_path=None,
                 neon_cmd=None, state_dir=None, bodies_dir=None, prefs_path=None):
        home = Path.home()
        self.jobs_path = Path(jobs_path) if jobs_path else home / "jobsearch" / "state" / "copilot" / "jobs.json"
        self.packet_root = Path(packet_root) if packet_root else home / "jobsearch" / "packets"
        self.facts_path = Path(facts_path) if facts_path else Path(DEFAULT_FACTS_PATH)
        self.prefs_path = Path(prefs_path) if prefs_path else self.facts_path.with_name("preferences.yaml")
        self.db_path = str(db_path) if db_path else default_db_path()
        self.neon_cmd = neon_cmd or os.environ.get("COPILOT_NEON_CMD") or DEFAULT_NEON_CMD
        self.state_dir = Path(state_dir) if state_dir else home / "jobsearch" / "state" / "copilot"
        self.bodies_dir = Path(bodies_dir) if bodies_dir else home / "jobsearch" / "bodies"

        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._jobs: list[dict] = self._load()

        now = _now()
        recovered = False
        for job in self._jobs:
            if job["state"] in ("running", "applying"):
                job.update(state="failed", error="interrupted_by_restart", finished=now, stage="")
                recovered = True
        if recovered:
            self._save()

    # -- persistence (caller holds the lock, except in __init__) ----------
    def _load(self) -> list[dict]:
        if not self.jobs_path.exists():
            return []
        data = json.loads(self.jobs_path.read_text(encoding="utf-8"))
        jobs = list(data.get("jobs", []))
        for job in jobs:  # records saved before staged progress existed
            job.setdefault("stage", "")
            job.setdefault("stage_secs", 0.0)
            job.setdefault("stages", [])
            job.setdefault("est_seconds", EST_SECONDS.get(_kind_of(job)))
        return jobs

    def _save(self) -> None:
        self.jobs_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.jobs_path.with_name(self.jobs_path.name + ".tmp")
        tmp.write_text(json.dumps({"jobs": self._jobs}, indent=2), encoding="utf-8")
        os.replace(tmp, self.jobs_path)

    def _find(self, jid: str) -> dict | None:
        for job in self._jobs:
            if job["id"] == jid:
                return job
        return None

    def _next_queued(self) -> dict | None:
        for job in self._jobs:
            if job["state"] == "queued":
                return job
        return None

    def _posting(self, uid: str) -> dict | None:
        st = Store(self.db_path)
        try:
            return st.get(uid)
        finally:
            st.close()

    # -- public API ----------------------------------------------------------
    def submit(self, uid: str, kind: str, instruction: str = "", confirm_protected: bool = False) -> dict:
        if kind not in ACTION_KINDS and kind != FACTS_KIND:
            raise ValueError(f"unknown kind {kind!r}; expected one of {ACTION_KINDS + (FACTS_KIND,)}")
        kind = ALIASES.get(kind, kind)  # retired names are stored as their current kind
        if kind == FACTS_KIND:
            if uid != FACTS_UID:
                raise ValueError(f"facts_update takes uid {FACTS_UID!r}, not {uid!r}")
            instruction = (instruction or "").replace("\r\n", "\n").strip()
            if not instruction:
                raise ValueError("facts_update needs a non-empty instruction")
            if len(instruction) > FACTS_INSTRUCTION_MAX:
                raise ValueError(f"instruction is over {FACTS_INSTRUCTION_MAX} characters")
        elif self._posting(uid) is None:
            raise KeyError(f"no posting with uid {uid!r}")
        with self._lock:
            for job in self._jobs:
                if job["uid"] == uid and _kind_of(job) == kind and job["state"] in ("queued", "running"):
                    return dict(job)
            job = {
                "id": uuid.uuid4().hex[:12], "uid": uid, "kind": kind, "state": "queued",
                "created": _now(), "started": None, "finished": None, "error": "",
                "answer_path": "", "artifact_path": "", "blocker": "", "message": "",
                "est_seconds": EST_SECONDS[kind],
                "stage": "", "stage_secs": 0.0, "stages": [],
            }
            if kind == FACTS_KIND:
                job.update(instruction=instruction, confirm_protected=bool(confirm_protected))
            self._jobs.append(job)
            self._save()
            self._cv.notify_all()
            return dict(job)

    def latest(self, uid: str) -> dict | None:
        with self._lock:
            for job in reversed(self._jobs):
                if job["uid"] == uid:
                    return dict(job)
        return None

    def job(self, jid: str) -> dict | None:
        with self._lock:
            job = self._find(jid)
            return dict(job) if job else None

    def jobs(self) -> list[dict]:
        with self._lock:
            return [dict(j) for j in self._jobs]

    def start(self) -> None:
        with self._lock:
            if self._thread is not None:
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, name="copilot-worker", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        with self._lock:
            self._stop.set()
            self._cv.notify_all()
            thread = self._thread
        if thread is not None:
            thread.join()
        with self._lock:
            self._thread = None

    # -- worker --------------------------------------------------------------
    def _loop(self) -> None:
        while True:
            with self._cv:
                while not self._stop.is_set() and self._next_queued() is None:
                    self._cv.wait(timeout=1.0)
                if self._stop.is_set():
                    return
                job = self._next_queued()
                job.update(state="running", started=_now())
                self._save()
                jid = job["id"]
            try:
                self._process(jid)
            except Exception as exc:  # keep the lane alive; record the failure
                self._finish(jid, "failed", error=f"worker_error: {exc}")

    def _finish(self, jid: str, state: str, **fields) -> None:
        with self._lock:
            job = self._find(jid)
            job.update(state=state, finished=_now(), stage="", stage_secs=0.0, **fields)
            self._save()

    def _process(self, jid: str) -> None:
        with self._lock:
            job = dict(self._find(jid))
        uid, kind = job["uid"], _kind_of(job)

        if kind == FACTS_KIND:  # no posting: the instruction lives on the job
            self._process_facts(jid, job)
            return

        posting = self._posting(uid)
        if posting is None:
            self._finish(jid, "failed", error="unknown_uid")
            return

        if kind == PREP_KIND:
            self._run_prep(jid, uid)
            return

        if kind == "questions":
            src = Path(self.packet_root) / _slug(uid) / QUESTIONS_FILE
            try:
                supplied = src.is_file() and src.read_text(encoding="utf-8").strip()
            except OSError:
                supplied = ""
            if not supplied:
                # Nothing to answer: fail before spending a 2-3 minute session.
                self._finish(jid, "failed",
                             error="no_questions: no questions were supplied")
                return

        bundle = self.state_dir / jid
        has_questions = False
        try:
            bundle.mkdir(parents=True, exist_ok=True)
            posting_md = self._stage(bundle, posting)
            if kind == "questions":
                has_questions = self._stage_questions(bundle, uid)
        except Exception as exc:
            self._finish(jid, "failed", error=f"stage_failed: {exc}")
            return

        prompt = PROMPTS[kind].format(
            company=posting.get("company") or "", title=posting.get("title") or "",
            location=posting.get("location") or "",
            questions_note="" if has_questions or kind != "questions" else NO_QUESTIONS_NOTE)
        rc, out, err = self._invoke(prompt, bundle, jid)
        if rc == RATE_LIMIT_EXIT:
            time.sleep(RATE_LIMIT_SLEEP_S)
            rc, out, err = self._invoke(prompt, bundle, jid)
        self._clear_stage(jid)  # the call is over: nothing is running any more
        last = _last_line(err)
        if rc != 0:
            self._finish(jid, "failed", error=last or f"exit {rc}")
            return
        if kind == "questions" and not has_questions:
            self._finish(jid, "failed", error="no_questions: no questions were supplied")
            return

        answer_path = bundle / "answer.txt"
        answer_path.write_text(out, encoding="utf-8")
        if not out.strip():
            self._finish(jid, "failed", error=last or "empty_answer")
            return

        facts_text = (bundle / "facts.yaml").read_text(encoding="utf-8")
        ok, problems = validate(kind, out, facts_text, posting_md)
        if problems == [FRENCH_TOKEN]:
            reason = next(ln.strip() for ln in out.splitlines() if FRENCH_TOKEN in ln)
            self._finish(jid, "failed", error=reason, blocker="french")
        elif ok:
            try:
                artifact = self._write_artifact(jid, uid, kind, out)
            except ArtifactError as exc:
                self._finish(jid, "failed", error=f"artifact_failed: {exc}")
                return
            self._finish(jid, "done", answer_path=str(answer_path), artifact_path=str(artifact))
        else:
            self._finish(jid, "failed", error="check_failed: " + ", ".join(problems))

    # -- facts_update: propose, then accept or reject ------------------------
    def _facts_target(self, name: str) -> Path:
        return self.prefs_path if name == "preferences.yaml" else self.facts_path

    def _process_facts(self, jid: str, job: dict) -> None:
        """Worker branch for facts_update. Stage, ask the model, validate, propose.

        Nothing is written to facts.yaml or preferences.yaml here. A valid edit waits in
        pending_review until accept_facts() is called with the sha the reviewer saw.
        """
        instruction = str(job.get("instruction") or "")
        if not instruction.strip():
            self._finish(jid, "failed", error="stage_failed: no instruction")
            return
        bundle = self.state_dir / jid
        try:
            bundle.mkdir(parents=True, exist_ok=True)
            facts_bytes = self.facts_path.read_bytes()
            prefs_existed = self.prefs_path.exists()
            prefs_bytes = self.prefs_path.read_bytes() if prefs_existed else b""
            facts_text = facts_bytes.decode("utf-8")
            prefs_text = prefs_bytes.decode("utf-8")
            (bundle / "facts.yaml").write_bytes(facts_bytes)
            (bundle / "preferences.yaml").write_text(prefs_text or "version: 1\n", encoding="utf-8")
            (bundle / "instruction.txt").write_text(instruction, encoding="utf-8")
        except Exception as exc:
            self._finish(jid, "failed", error=f"stage_failed: {exc}")
            return

        prompt = PROMPTS[FACTS_KIND].format(
            instruction=instruction, facts_text=facts_text,
            prefs_text=prefs_text or FACTS_PREFS_ABSENT)
        rc, out, err = self._invoke(prompt, bundle, jid)
        if rc == RATE_LIMIT_EXIT:
            time.sleep(RATE_LIMIT_SLEEP_S)
            rc, out, err = self._invoke(prompt, bundle, jid)
        self._clear_stage(jid)
        last = _last_line(err)
        if rc != 0:
            self._finish(jid, "failed", error=last or f"exit {rc}")
            return
        answer_path = str(bundle / "answer.txt")
        (bundle / "answer.txt").write_text(out, encoding="utf-8")

        payload, perr = facts_ops.parse_answer(out)
        if perr:
            self._finish(jid, "failed", error=f"check_failed: {perr}", answer_path=answer_path)
            return
        ops, verr = facts_ops.validate(
            payload, facts_text=facts_text, prefs_text=prefs_text,
            confirm_protected=bool(job.get("confirm_protected")), instruction_text=instruction)
        if verr:
            self._finish(jid, "failed", error=f"check_failed: {verr}", answer_path=answer_path)
            return

        status = payload["status"]
        summary = str(payload.get("summary") or "")
        if status != "propose":  # no_change, clarify or refuse: nothing to review or apply
            self._finish(jid, "done", error="", answer_path=answer_path, proposal_state=status,
                         summary=summary, question=str(payload.get("question") or ""),
                         reason=str(payload.get("reason") or ""))
            return

        target = payload["target"]
        if target == "facts.yaml":
            current_text, current_bytes, existed = facts_text, facts_bytes, True
        else:
            current_text, current_bytes, existed = prefs_text, prefs_bytes, prefs_existed
        ops, upgraded = facts_ops.upgrade_absent_sets(current_text, ops or [])
        try:
            new_text = facts_ops.apply(current_text, ops)
        except facts_ops.FactsOpsError as exc:
            self._finish(jid, "failed", error=f"check_failed: apply: {exc}", answer_path=answer_path)
            return
        diff = facts_ops.diff_text(current_text, new_text, target)
        if not diff:  # the edit changes nothing: same as no_change
            self._finish(jid, "done", error="", answer_path=answer_path, proposal_state="no_change",
                         summary=summary, question="", reason="")
            return
        base_sha = hashlib.sha256(current_bytes).hexdigest()
        proposal = {"target": target, "base_sha256": base_sha, "base_existed": existed,
                    "summary": summary, "ops": ops, "diff": diff, "new_text": new_text}
        (bundle / "proposal.json").write_text(json.dumps(proposal, indent=2), encoding="utf-8")
        self._finish(jid, "pending_review", error="", answer_path=answer_path, target=target,
                     summary=summary, question="", reason="", ops=ops, diff=diff,
                     new_text=new_text, base_sha256=base_sha, base_existed=existed,
                     proposal_state="pending_review", upgraded=upgraded)

    def accept_facts(self, jid: str, seen_sha256: str) -> dict:
        """Write a pending_review proposal, run the checks, and commit it. Returns the job.

        seen_sha256 is the sha256 of the target file the reviewer was shown. If the file on
        disk differs from it, or from the proposal's base, raises FactsStaleError and writes
        nothing. A failed check or git step restores the previous bytes and finishes the job
        "rejected" with the output in error. Success finishes it "applied" with commit_sha.
        """
        with self._lock:
            job = self._find(jid)
            if job is None or job["kind"] != FACTS_KIND:
                raise KeyError(f"no facts_update job {jid!r}")
            if job["state"] != "pending_review":
                raise FactsStateError(f"job {jid} is {job['state']}, not pending_review")
            target = self._facts_target(job["target"])
            current = _file_sha(target)
            if seen_sha256 != job["base_sha256"] or current != seen_sha256:
                raise FactsStaleError(f"{target.name} changed since the preview (now {current[:12]})")
            job.update(state="applying", error="")
            self._save()
            snapshot = dict(job)

        old_bytes = target.read_bytes() if target.exists() else None
        resume = self.facts_path.parent
        try:
            _write_text(target, snapshot["new_text"])
        except OSError as exc:
            self._restore(target, old_bytes)
            self._finish(jid, "rejected", error=f"write_failed: {exc}", proposal_state="rejected")
            return self.job(jid)
        problem = self._verify_written(target, resume)
        commit_sha = ""
        if not problem:
            commit_sha, problem = self._commit_written(target, resume, snapshot["instruction"])
        if problem:
            self._restore(target, old_bytes)
            self._finish(jid, "rejected", error=problem, proposal_state="rejected")
            return self.job(jid)
        self._finish(jid, "applied", error="", proposal_state="accepted", commit_sha=commit_sha)
        return self.job(jid)

    def reject_facts(self, jid: str) -> dict:
        """Mark a pending_review proposal rejected. Writes nothing."""
        with self._lock:
            job = self._find(jid)
            if job is None or job["kind"] != FACTS_KIND:
                raise KeyError(f"no facts_update job {jid!r}")
            if job["state"] != "pending_review":
                raise FactsStateError(f"job {jid} is {job['state']}, not pending_review")
            job.update(state="rejected", finished=_now(), proposal_state="rejected")
            self._save()
            return dict(job)

    def _verify_written(self, target: Path, resume: Path) -> str:
        """Check the file just written. Returns "" on pass, otherwise the failure text."""
        try:
            parsed = yaml.safe_load(target.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            return f"{target.name} does not parse: {exc}"
        if not isinstance(parsed, dict):
            return f"{target.name} is not a YAML mapping"
        if target.name != "facts.yaml":  # preferences.yaml is read by no checker
            return ""
        for name in FACTS_CHECKS:
            script = resume / name
            if not script.is_file():
                return f"{name} missing in {resume}"
            try:
                proc = subprocess.run([sys.executable, str(script)], cwd=str(resume),
                                      capture_output=True, text=True, encoding="utf-8",
                                      errors="replace", stdin=subprocess.DEVNULL,
                                      timeout=FACTS_CHECK_TIMEOUT_S)
            except (OSError, subprocess.TimeoutExpired) as exc:
                return f"{name} failed to run: {exc}"
            if proc.returncode != 0:
                output = (proc.stdout + proc.stderr).strip()
                return f"{name} exit {proc.returncode}\n{output[-2000:]}"
        return ""

    def _commit_written(self, target: Path, resume: Path, instruction: str) -> tuple[str, str]:
        """git add and commit only the target file. Returns (commit sha, "") or ("", error)."""
        message = f'facts-ui: "{_one_line(instruction).replace(chr(34), chr(39))}"'
        add = self._git(resume, "add", "--", target.name)
        if add.returncode != 0:
            self._git(resume, "reset", "-q", "--", target.name)
            return "", f"git add failed: {(add.stderr or add.stdout).strip()[-500:]}"
        commit = self._git(resume, "commit", "-q", "-m", message, "--", target.name)
        if commit.returncode != 0:
            self._git(resume, "reset", "-q", "--", target.name)
            return "", f"git commit failed: {(commit.stderr or commit.stdout).strip()[-500:]}"
        head = self._git(resume, "rev-parse", "HEAD")
        return head.stdout.strip(), ""

    @staticmethod
    def _git(resume: Path, *args: str) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(["git", "-C", str(resume), *args], capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL,
                                  timeout=FACTS_CHECK_TIMEOUT_S)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return subprocess.CompletedProcess(args, 1, "", str(exc))

    @staticmethod
    def _restore(target: Path, old_bytes: bytes | None) -> None:
        """Put back the previous bytes; a file that did not exist before is removed."""
        if old_bytes is None:
            target.unlink(missing_ok=True)
        else:
            _write_bytes(target, old_bytes)

    def _prep_command(self, uid: str) -> list[str]:
        return [sys.executable, *PREP_MODULE_ARGS, uid]

    def _run_prep(self, jid: str, uid: str) -> None:
        """Run the CLI's prep step. Exit 0 means it built the packet; the CLI owns the status.

        Nothing is staged and no model is called. This branch never writes the store:
        the CLI sets 'prepped' only after a build succeeds, and a non-zero exit leaves it alone.
        """
        try:
            proc = subprocess.run(self._prep_command(uid), cwd=str(PROJECT_ROOT),
                                  capture_output=True, text=True, encoding="utf-8",
                                  errors="replace", stdin=subprocess.DEVNULL,
                                  timeout=PREP_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            self._finish(jid, "failed", error="prep_timeout")
            return
        except OSError as exc:
            self._finish(jid, "failed", error=f"prep_failed: {exc}")
            return
        if proc.returncode != 0:
            error = _last_line(proc.stderr) or _last_line(proc.stdout) or f"exit {proc.returncode}"
            self._finish(jid, "failed", error=error)
            return
        self._finish(jid, "done", answer_path="", error="",
                     artifact_path=str(self.packet_root / _slug(uid)),
                     message=_last_line(proc.stdout) or "prep done")

    def _write_artifact(self, jid: str, uid: str, kind: str, answer: str) -> Path:
        """Turn a validated answer into its packet file; returns the artifact path.

        Runs before the job is marked done, so 'done' always has its file. On any
        failure, previously accepted files are left as they were.
        """
        try:
            if kind == "letter":
                return self._write_letter(jid, uid, _slug(uid), answer)
            target_dir = self.packet_root / _slug(uid)
            target_dir.mkdir(parents=True, exist_ok=True)
            path = target_dir / ARTIFACT_NAMES[kind]
            _write_text(path, answer)
            return path
        except OSError as exc:
            raise ArtifactError(f"write_failed: {exc}") from exc

    def _write_letter(self, jid: str, uid: str, slug: str, answer: str) -> Path:
        return self._compile_letter(jid, f"copilot job {jid}", uid, slug, answer)

    def publish_body(self, uid: str, body_text: str) -> Path:
        """Recompile a hand-edited letter body with no model call; returns the new PDF path.

        Runs the AI letter path minus the model: candidate, render into staging, ligature
        check, publish, status guard. Empty or non-ASCII text is refused before anything is
        written. Raises KeyError for an unknown uid and ArtifactError when the render or the
        ligature check fails; in both cases the accepted body, tex and PDF are left as they were.
        """
        text = (body_text or "").replace("\r\n", "\n")  # browser textareas submit CRLF
        if not text.strip():
            raise ValueError("body is empty: nothing to compile")
        bad = sorted({ch for ch in text if ord(ch) > 127})
        if bad:
            names = ", ".join(f"U+{ord(ch):04X}" for ch in bad[:5])
            more = "" if len(bad) <= 5 else f" and {len(bad) - 5} more"
            raise ValueError(f"body contains non-ASCII characters ({names}{more}); use plain ASCII")
        if self._posting(uid) is None:
            raise KeyError(f"no posting with uid {uid!r}")
        token = f"manual-{uuid.uuid4().hex[:12]}"
        try:
            return self._compile_letter(token, f"body edited by hand (posting {uid})",
                                        uid, _slug(uid), text)
        except OSError as exc:
            raise ArtifactError(f"write_failed: {exc}") from exc

    def _compile_letter(self, token: str, source: str, uid: str, slug: str, body: str) -> Path:
        """Shared letter path for the AI step and the hand edit.

        token names the staging dir; source prefixes the status event note.
        """
        self.bodies_dir.mkdir(parents=True, exist_ok=True)
        accepted = self.bodies_dir / f"{slug}.txt"
        candidate = self.bodies_dir / f"{slug}.candidate.txt"
        staging = self.bodies_dir / ".render" / token
        # The candidate is a new name, so a failed attempt never touches the accepted body.
        candidate.write_text(body, encoding="utf-8")
        hits = ligature_hits(body)
        if hits:
            raise ArtifactError(f"ligature_in_body: {', '.join(hits)}")
        # render_letter deletes any stale PDF in its out_dir before compiling, so it
        # compiles into a fresh staging dir and the packet keeps its old PDF on failure.
        staging.mkdir(parents=True, exist_ok=True)
        try:
            pdf = Path(render_letter(uid, str(candidate), out_dir=str(staging),
                                     facts_path=str(self.facts_path)))
        except Exception as exc:
            raise ArtifactError(f"render_failed: {exc}") from exc
        tex = staging / "cover_letter.tex"
        if not pdf.is_file() or not tex.is_file():
            raise ArtifactError("render_failed: no cover_letter.pdf/.tex written")
        hits = ligature_hits(_pdf_text(pdf))
        if hits:
            raise ArtifactError(f"ligature_in_pdf: {', '.join(hits)}")
        target = self.packet_root / slug
        target.mkdir(parents=True, exist_ok=True)
        # Every check has passed: only now replace the accepted body, tex and PDF.
        _publish(pdf, target / "cover_letter.pdf")
        _publish(tex, target / "cover_letter.tex")
        _publish(candidate, accepted)
        shutil.rmtree(staging, ignore_errors=True)
        self._record_letter_status(source, uid)
        return target / "cover_letter.pdf"

    def _record_letter_status(self, source: str, uid: str) -> None:
        """Move a posting to 'prepped' only from new, shortlisted or prepped.

        Any later status (applied, interview, offer, terminal states) is kept. The event
        is still logged, with the same status, so the audit trail shows the render.
        """
        store = Store(self.db_path)
        try:
            current = (store.get(uid) or {}).get("status")
            if current in LETTER_PREP_FROM:
                store.set_status(uid, "prepped", f"{source}: cover letter rendered")
            else:
                store.set_status(uid, current,
                                 f"{source}: cover letter rendered without status change "
                                 f"(posting at {current})")
        finally:
            store.close()

    def _stage_questions(self, bundle: Path, uid: str) -> bool:
        """Copy the packet's questions.txt into the bundle. False when it is missing or empty."""
        try:
            text = (self.packet_root / _slug(uid) / QUESTIONS_FILE).read_text(encoding="utf-8")
        except (OSError, ValueError):
            return False
        if not text.strip():
            return False
        (bundle / QUESTIONS_FILE).write_text(text, encoding="utf-8")
        return True

    def _stage(self, bundle: Path, posting: dict) -> str:
        shutil.copyfile(self.facts_path, bundle / "facts.yaml")
        posting_md = (
            f"# {posting.get('title') or ''}\n\n"
            f"- Company: {posting.get('company') or ''}\n"
            f"- Location: {posting.get('location') or ''}\n"
            f"- URL: {posting.get('url') or ''}\n\n"
            f"{posting.get('description') or ''}\n"
        )
        (bundle / "posting.md").write_text(posting_md, encoding="utf-8")
        pdf = self.packet_root / _slug(posting["uid"]) / "resume.pdf"
        if pdf.is_file():
            proc = subprocess.run(["pdftotext", str(pdf), "-"], capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=120)
            if proc.returncode != 0:
                raise RuntimeError(f"pdftotext exit {proc.returncode}: {_last_line(proc.stderr)}")
            (bundle / "resume.txt").write_text(proc.stdout, encoding="utf-8")
        return posting_md

    def _invoke(self, prompt: str, bundle: Path, jid: str | None = None) -> tuple[int, str, str]:
        """Run the model CLI once. Returns (rc, stdout, stderr); a timeout returns (1, "", "timeout").

        Stderr is read line by line while the call runs. Each '# stage NAME SECS' line updates
        the job record (stage, stage_secs, stages) and is saved at once, and is appended raw to
        run.log in the bundle. Stdout is collected into a buffer. On timeout the child is killed.
        """
        cmd = [self.neon_cmd, prompt, "--context", str(bundle), "--max-chars", MAX_CHARS]
        log = (bundle / RUN_LOG).open("a", encoding="utf-8")  # opened before spawn: no orphan on failure
        try:
            proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True, encoding="utf-8",
                                    errors="replace", bufsize=1)
        except OSError as exc:
            log.close()
            return 1, "", str(exc)
        out_parts: list[str] = []
        err_parts: list[str] = []

        def read_stdout() -> None:
            out_parts.append(proc.stdout.read())

        def read_stderr() -> None:
            for line in proc.stderr:
                err_parts.append(line)
                try:  # every stderr line is kept in run.log, stage markers or not
                    log.write(line if line.endswith("\n") else line + "\n")
                    log.flush()
                except (OSError, ValueError):  # log closed on timeout: keep draining stderr
                    pass
                text = line.rstrip("\r\n")
                m = STAGE_LINE.match(text)
                start = None if m else STAGE_START_LINE.match(text)
                if (not m and not start) or jid is None:
                    continue
                secs = 0.0
                if m:
                    try:
                        secs = float(m.group(2))
                    except ValueError:
                        continue
                try:
                    if m:
                        self._record_stage(jid, m.group(1), secs)
                    else:
                        self._record_stage_start(jid, start.group(1))
                except Exception:  # keep draining stderr even if a save fails, or the child blocks
                    pass

        readers = [threading.Thread(target=read_stdout, daemon=True),
                   threading.Thread(target=read_stderr, daemon=True)]
        try:
            for t in readers:
                t.start()
            try:
                rc = proc.wait(timeout=CALL_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                for t in readers:
                    t.join(timeout=5)
                return 1, "", "timeout"
            for t in readers:
                t.join()
            return rc, "".join(out_parts), "".join(err_parts)
        finally:
            log.close()
            if proc.poll() is None:  # only on an unexpected exception: never leave a child running
                proc.kill()
                proc.wait()

    def _record_stage(self, jid: str, name: str, secs: float) -> None:
        """A step has finished. A name already in the list is updated in place, never appended twice."""
        with self._lock:
            job = self._find(jid)
            job.update(stage=name, stage_secs=secs)
            for entry in job["stages"]:
                if entry["name"] == name:
                    entry["secs"] = secs
                    break
            else:
                job["stages"].append({"name": name, "secs": secs})
            self._save()

    def _record_stage_start(self, jid: str, name: str) -> None:
        """A step has begun: show it as the current stage. Seconds and the finished list wait for its end."""
        with self._lock:
            job = self._find(jid)
            job.update(stage=name)
            self._save()

    def _clear_stage(self, jid: str) -> None:
        with self._lock:
            job = self._find(jid)
            job.update(stage="", stage_secs=0.0)
            self._save()
