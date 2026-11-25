"""Tests for jobsearch.copilot: prompts, validation, submit/dedupe, and the worker.

The worker tests run a fake CLI (a small python script in tmp_path) that replays a
plan of {exit, stdout, stderr} steps, one per call. The real neon binary is never run.
"""
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from jobsearch import copilot
from jobsearch.copilot import (FACTS_KIND, FACTS_UID, PROMPTS, Copilot, FactsStaleError,
                               FactsStateError, validate)
from jobsearch.store import Store

UID = "greenhouse:acme/8328549"
SLUG = "greenhouse_acme_8328549"
POSTING = {
    "uid": UID, "source": "greenhouse", "company": "Acme", "title": "Security Engineer",
    "location": "Winnipeg, MB", "url": "https://example.test/job/8328549",
    "description": "Run detection engineering on the SOC team.",
}

FAKE_SRC = r'''#!PYTHON
import json, os, pathlib, sys, time
here = pathlib.Path(__file__).resolve().parent
(here / "pid").write_text(str(os.getpid()), encoding="utf-8")
plan = json.loads((here / "plan.json").read_text(encoding="utf-8"))
log = here / "calls.jsonl"
n = len(log.read_text(encoding="utf-8").splitlines()) if log.exists() else 0
with log.open("a", encoding="utf-8") as fh:
    fh.write(json.dumps(sys.argv[1:]) + "\n")
step = plan[min(n, len(plan) - 1)]
for line in step.get("stage_lines", []):  # dispatcher-style progress on stderr, one line at a time
    sys.stderr.write(line + "\n")
    sys.stderr.flush()
    time.sleep(step.get("pause", 0.05))
sys.stdout.write(step.get("stdout", ""))
sys.stderr.write(step.get("stderr", ""))
sys.stdout.flush()
sys.stderr.flush()
time.sleep(step.get("sleep", 0))  # stay alive briefly (or long, for the timeout test)
sys.exit(step.get("exit", 0))
'''


def good_letter(first="lorem"):
    words = [first] + ["lorem"] * 249
    return "\n\n".join(" ".join(words[i:i + 25]) for i in range(0, 250, 25)) + "\n"


def good_questions(blocks=3, words=70):
    """Bold question headings, each followed by one answer paragraph of plain ASCII."""
    out = []
    for i in range(blocks):
        out.append(f"**Question {i + 1}?**\n" + " ".join(["lorem"] * words) + "\n")
    return "\n".join(out)


def good_analysis(filler=60):
    """Four headings, each alone on its line, with 60 filler words under each (244 words total)."""
    sections = []
    for heading in ("VERDICT", "FIT", "GAPS", "TO_BE_PERFECT"):
        sections.append(heading + "\n" + " ".join(["lorem"] * filler) + "\n")
    return "\n".join(sections)


@pytest.fixture
def env(tmp_path):
    facts = tmp_path / "facts.yaml"
    facts.write_text("version: 1\ncontact:\n  name: \"Alex Morgan\"\n", encoding="utf-8")
    db = tmp_path / "jobs.db"
    st = Store(str(db))
    st.upsert_postings([POSTING])
    st.close()
    packets = tmp_path / "packets"
    (packets / SLUG).mkdir(parents=True)  # no resume.pdf: resume.txt must be skipped
    (packets / SLUG / "posting.md").write_text("# old", encoding="utf-8")
    fake_dir = tmp_path / "fake"
    fake_dir.mkdir()
    return SimpleNamespace(tmp=tmp_path, facts=facts, db=db, packets=packets, fake_dir=fake_dir,
                           bodies=tmp_path / "bodies")


@pytest.fixture(autouse=True)
def fake_latex(monkeypatch):
    """Safety net: no test reaches the real tectonic or pdftotext. Tests override as needed."""
    def render(uid, body_path, out_dir=None, **kwargs):
        out = Path(out_dir)
        (out / "cover_letter.pdf").write_bytes(b"%PDF fake " + Path(body_path).read_bytes())
        (out / "cover_letter.tex").write_text("\\documentclass{article}", encoding="utf-8")
        return str((out / "cover_letter.pdf").resolve())
    monkeypatch.setattr(copilot, "render_letter", render)
    monkeypatch.setattr(copilot, "_pdf_text", lambda pdf: "plain text\n")


def make_fake(env, plan):
    path = env.fake_dir / "neon"
    path.write_text(FAKE_SRC.replace("PYTHON", sys.executable), encoding="utf-8")
    os.chmod(path, 0o755)
    (env.fake_dir / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
    return str(path)


def make_cop(env, neon_cmd, state="state"):
    return Copilot(
        jobs_path=env.tmp / state / "copilot" / "jobs.json",
        packet_root=env.packets, facts_path=env.facts, db_path=env.db,
        neon_cmd=neon_cmd, state_dir=env.tmp / state / "copilot", bodies_dir=env.bodies,
    )


def calls_of(env):
    log = env.fake_dir / "calls.jsonl"
    if not log.exists():
        return []
    return [json.loads(ln) for ln in log.read_text(encoding="utf-8").splitlines()]


def wait_for(cop, jid, states, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = cop.job(jid)
        if job["state"] in states:
            return job
        threading.Event().wait(0.02)  # not time.sleep: tests patch that
    raise AssertionError(f"job {jid} never reached {states}: {cop.job(jid)}")


@pytest.fixture
def worker(env):
    """Start the worker against a fake CLI; always stop it afterwards."""
    started = []

    def _make(plan, state="state"):
        cop = make_cop(env, make_fake(env, plan), state=state)
        started.append(cop)
        cop.start()
        return cop

    yield _make
    for cop in started:
        cop.stop()


# ---------- prompts ----------

def test_prompts_have_two_keys_and_format_cleanly():
    assert set(PROMPTS) == {"letter", "analysis", "questions", "facts_update"}
    for key, tpl in PROMPTS.items():
        if key == "facts_update":  # formatted with instruction and file texts; see the facts tests
            continue
        for field in ("{company}", "{title}", "{location}"):
            assert field in tpl, (key, field)
        text = tpl.format(company="Acme", title="Security Engineer", location="Winnipeg, MB",
                          questions_note="")
        assert "Acme" in text and "Security Engineer" in text and "Winnipeg, MB" in text
    assert "FRENCH_BLOCKER" in PROMPTS["letter"]
    q = PROMPTS["questions"]
    assert "questions.txt" in q and "60-140 words" in q and "**Question text**" in q
    assert "facts.yaml is the ONLY" in q and all(ord(ch) < 128 for ch in q)


def test_analysis_prompt_grounding_headings_and_length():
    tpl = PROMPTS["analysis"]
    assert "ONLY admissible source" in tpl and "facts.yaml" in tpl
    assert "posting.md" in tpl and "resume.txt" in tpl
    headings = ["VERDICT", "FIT", "GAPS", "TO_BE_PERFECT"]
    positions = [tpl.index(f"\n{h}\n") for h in headings]  # each heading alone on its line
    assert positions == sorted(positions)
    assert "250-500 words" in tpl and "plain ASCII" in tpl
    assert "French" not in tpl  # the prompt must not mention French
    assert all(ord(ch) < 128 for ch in tpl)
    assert set(copilot.ALIASES) == {"critique", "gaps"} and set(copilot.ALIASES.values()) == {"analysis"}


def test_retired_names_keep_artifact_names_and_old_files_stay_listed():
    assert copilot.ARTIFACT_NAMES["analysis"] == "analysis.md"
    assert copilot.ARTIFACT_NAMES["critique"] == "critique.md"
    assert copilot.ARTIFACT_NAMES["gaps"] == "gaps.md"
    assert copilot.KINDS == ("letter", "analysis", "questions")
    assert copilot.ACTION_KINDS == ("prep", "letter", "analysis", "questions", "critique", "gaps")
    assert copilot.EST_SECONDS == {"analysis": 200, "letter": 200, "prep": 30, "questions": 150,
                                   "facts_update": 150}


# ---------- validate ----------

def test_validate_accepts_clean_250_word_letter():
    text = good_letter()
    assert len(text.split()) == 250
    assert validate("letter", text, "", "") == (True, [])


def test_validate_rejects_non_ascii():
    ok, problems = validate("letter", good_letter("na\u00efve"), "", "")
    assert not ok and "non_ascii" in problems


def test_validate_rejects_short_letter():
    ok, problems = validate("letter", " ".join(["lorem"] * 100), "", "")
    assert not ok and "word_count=100" in problems


def test_validate_rejects_greeting_and_backslash():
    ok, problems = validate("letter", "Dear hiring team,\n" + good_letter(), "", "")
    assert not ok and "greeting" in problems
    ok, problems = validate("letter", good_letter().replace("lorem", "a\\b", 1), "", "")
    assert not ok and "code_tokens" in problems


def test_french_blocker_returns_only_that_problem():
    ok, problems = validate("analysis", "FRENCH_BLOCKER: must be fluent in French", "", "")
    assert not ok and problems == ["FRENCH_BLOCKER"]


def test_analysis_accepts_well_formed_answer_with_loose_heading_case():
    assert validate("analysis", good_analysis(), "", "") == (True, [])
    loose = good_analysis().replace("FIT\n", "  fit  \n")
    assert validate("analysis", loose, "", "") == (True, [])


def test_analysis_rejects_missing_heading_non_ascii_and_short_answer():
    no_fit = "\n".join(ln for ln in good_analysis().splitlines() if ln != "FIT")
    ok, problems = validate("analysis", no_fit, "", "")
    assert not ok and "missing_heading=FIT" in problems

    ok, problems = validate("analysis", good_analysis().replace("lorem", "caf\u00e9", 1), "", "")
    assert not ok and "non_ascii" in problems

    short = "VERDICT\nstretch\nFIT\nGAPS\nTO_BE_PERFECT\nlorem ipsum\n"
    ok, problems = validate("analysis", short, "", "")
    assert not ok and any(p.startswith("word_count=") for p in problems)
    assert validate("analysis", "", "", "") == (False, ["empty"])


def test_analysis_keeps_greeting_and_code_token_rules():
    ok, problems = validate("analysis", "Dear team,\n" + good_analysis(), "", "")
    assert not ok and "greeting" in problems
    ok, problems = validate("analysis", good_analysis().replace("lorem", "a\\b", 1), "", "")
    assert not ok and "code_tokens" in problems


def test_retired_kind_name_validates_as_analysis():
    assert validate("critique", good_analysis(), "", "") == (True, [])
    ok, problems = validate("gaps", "VERDICT only\n", "", "")
    assert not ok and "missing_heading=FIT" in problems


def test_analysis_does_no_number_check_against_facts():
    text = good_analysis().replace("lorem", "97.4%", 1)
    assert validate("analysis", text, "version: 1\n", "") == (True, [])


def _letter_with(*tokens):
    """good_letter() with its first len(tokens) words swapped for the given tokens (word count unchanged)."""
    words = good_letter().split()
    words[:len(tokens)] = list(tokens)
    return " ".join(words) + "\n"


def test_letter_whose_numbers_all_appear_in_facts_passes():
    facts = "metric: 90.3%\nspend: $2.31\ncount: 14,335\nteam: 8\n"
    text = _letter_with("90.3%", "$2.31", "14,335,", "8")
    assert validate("letter", text, facts, "") == (True, [])


def test_letter_metric_missing_from_facts_fails_naming_token():
    facts = "metric: 90.3%\n"
    ok, problems = validate("letter", _letter_with("97.4%"), facts, "")
    assert not ok
    assert "unverified_metric=97.4%" in problems


PROJECT_FACTS = (
    "version: 1\n"
    "projects:\n"
    "  - name: Aurora Ledger Reconciliation Pipeline\n"
    "    stack: Python, SQLite, pytest\n"
    "  - name: Beacon Agent Operations Layer\n"
    "  - name: Embedding Fine-Tuning - R16\n"
    "  - name: AI Hub\n"
    "rank: 16\n"
)


def test_letter_prompt_has_grounding_voice_and_format_rules():
    tpl = PROMPTS["letter"]
    assert "GROUNDING RULE" in tpl and "Every number you write must appear verbatim in facts.yaml" in tpl
    assert "BANNED" in tpl and "(Python, SQLite, pytest)" in tpl
    assert "OPENING LINE" in tpl and "CLOSING LINE" in tpl
    assert "240-320 words" in tpl and "three concise body paragraphs only" in tpl
    assert tpl.endswith("Role: {title} at {company} ({location}).")
    assert all(ord(ch) < 128 for ch in tpl)


def test_letter_naming_a_project_fails_project_proper_noun():
    text = _letter_with("Aurora", "Ledger", "Reconciliation", "Pipeline")
    ok, problems = validate("letter", text, PROJECT_FACTS, "")
    assert not ok
    assert "project_proper_noun=Aurora Ledger Reconciliation Pipeline" in problems


def test_project_name_match_is_case_insensitive():
    text = _letter_with("aurora", "ledger", "reconciliation", "pipeline")
    ok, problems = validate("letter", text, PROJECT_FACTS, "")
    assert "project_proper_noun=Aurora Ledger Reconciliation Pipeline" in problems


def test_short_project_names_are_not_policed():
    text = _letter_with("AI", "Hub", "wins")
    ok, problems = validate("letter", text, PROJECT_FACTS, "")
    assert not any(p.startswith("project_proper_noun") for p in problems)


def test_tech_list_parenthetical_fails():
    text = _letter_with("I", "built", "it", "(Python,", "SQLite,", "pytest)")
    ok, problems = validate("letter", text, PROJECT_FACTS, "")
    assert not ok
    assert "tech_list_parenthetical=(Python, SQLite, pytest)" in problems


def test_two_item_parenthetical_and_non_list_groups_pass_rule():
    text = _letter_with("(r=16,", "alpha=16)", "and", "(SC)")
    assert validate("letter", text, "rank: 16\n", "") == (True, [])


def test_letter_with_r_alpha_group_and_no_project_names_passes():
    text = _letter_with("LoRA", "tuning", "used", "(r=16,", "alpha=16)")
    facts = "version: 1\nrank: 16\nprojects:\n  - name: Beacon Agent Operations Layer\n"
    assert validate("letter", text, facts, "") == (True, [])


def test_unparseable_facts_skip_project_rule_without_failing():
    text = _letter_with("Aurora", "Ledger", "Reconciliation", "Pipeline")
    bad = "projects: [unclosed\n  - name: x\n"
    ok, problems = validate("letter", text, bad, "")
    assert not any(p.startswith("project_proper_noun") for p in problems)
    assert validate("letter", good_letter(), bad, "") == (True, [])


def test_project_rules_are_letter_only():
    text = good_analysis().replace("lorem", "Aurora Ledger Reconciliation Pipeline (Python, SQLite, pytest)", 1)
    assert validate("analysis", text, PROJECT_FACTS, "") == (True, [])


# ---------- questions ----------

def test_questions_kind_is_queued_and_writes_answers(env, worker):
    questions = "Why Acme?\nWhat did you ship?\n"
    (env.packets / SLUG / "questions.txt").write_text(questions, encoding="utf-8")
    text = good_questions()
    cop = worker([{"exit": 0, "stdout": text}, {"exit": 0, "stdout": text}])
    queued = cop.submit(UID, "questions")
    assert queued["kind"] == "questions" and queued["state"] == "queued"
    assert queued["est_seconds"] == 150
    job = wait_for(cop, queued["id"], ("done", "failed"))
    assert job["state"] == "done", job
    path = env.packets / SLUG / "answers.md"
    assert job["artifact_path"] == str(path)
    assert path.read_text(encoding="utf-8") == text
    bundle = cop.state_dir / job["id"]
    assert (bundle / "questions.txt").read_text(encoding="utf-8") == questions
    prompt = calls_of(env)[0][0]
    assert "questions.txt" in prompt and "NO QUESTIONS" not in prompt
    # No questions.txt: the prompt says so and the job fails with a clear message.
    (env.packets / SLUG / "questions.txt").unlink()
    before = len(calls_of(env))
    missing = _run(cop, "questions")
    assert missing["state"] == "failed"
    assert missing["error"] == "no_questions: no questions were supplied"
    # Fails before the model call: a missing questions file must not cost a session.
    assert len(calls_of(env)) == before
    assert path.read_text(encoding="utf-8") == text  # the earlier answers are untouched


def test_validate_questions_rejects_empty_and_non_ascii():
    assert validate("questions", "", "", "") == (False, ["empty"])
    assert validate("questions", "  \n \t\n", "", "") == (False, ["empty"])
    ok, problems = validate("questions", good_questions().replace("lorem", "caf\u00e9", 1), "", "")
    assert not ok and "non_ascii" in problems
    assert validate("questions", "**Why Acme?**\n", "", "") == (False, ["no_answer_block"])
    assert validate("questions", "Plain answer with no heading.\n", "", "") == (
        False, ["no_answer_block"])
    assert validate("questions", "**Q1?**\n**Q2?**\nAnswer to Q2.\n", "", "") == (True, [])
    assert validate("questions", good_questions(), "", "") == (True, [])


# ---------- submit ----------

def test_submit_dedupes_queued_job_and_rejects_bad_input(env):
    cop = make_cop(env, make_fake(env, [{"exit": 0, "stdout": "x"}]))
    first = cop.submit(UID, "letter")
    second = cop.submit(UID, "letter")
    assert second["id"] == first["id"] and first["state"] == "queued"
    assert len(cop.jobs()) == 1
    assert cop.submit(UID, "gaps")["id"] != first["id"]
    with pytest.raises(ValueError):
        cop.submit(UID, "bogus")
    with pytest.raises(KeyError):
        cop.submit("nope:unknown", "letter")
    assert cop.latest(UID)["kind"] == "analysis"
    assert calls_of(env) == []  # never started, so nothing ran


def test_submit_critique_yields_analysis_job_with_estimate_and_empty_stages(env):
    cop = make_cop(env, make_fake(env, [{"exit": 0}]))
    job = cop.submit(UID, "critique")
    assert job["kind"] == "analysis" and job["est_seconds"] == 200
    assert job["stage"] == "" and job["stages"] == []
    assert set(job) >= {"id", "uid", "kind", "state", "created", "started", "finished", "error",
                        "answer_path", "artifact_path", "blocker", "message", "est_seconds",
                        "stage", "stage_secs", "stages"}
    assert "eta_s" not in job  # ETA is derived by the caller from started + est_seconds


def test_submit_gaps_yields_analysis_and_dedupes_across_aliases(env):
    cop = make_cop(env, make_fake(env, [{"exit": 0}]))
    first = cop.submit(UID, "gaps")
    assert first["kind"] == "analysis"
    assert cop.submit(UID, "analysis")["id"] == first["id"]
    assert cop.submit(UID, "critique")["id"] == first["id"]
    assert len(cop.jobs()) == 1
    assert cop.submit(UID, "prep")["est_seconds"] == 30


def test_neon_cmd_defaults_to_env_var(env, monkeypatch):
    monkeypatch.setenv("COPILOT_NEON_CMD", "/tmp/fake-neon-from-env")
    cop = Copilot(jobs_path=env.tmp / "j.json", packet_root=env.packets,
                  facts_path=env.facts, db_path=env.db, state_dir=env.tmp / "s")
    assert cop.neon_cmd == "/tmp/fake-neon-from-env"


# ---------- worker ----------

def test_worker_runs_fake_cli_end_to_end(env, worker):
    cop = worker([{"exit": 0, "stdout": good_letter()}])
    job = cop.submit(UID, "letter")
    done = wait_for(cop, job["id"], ("done", "failed"))
    assert done["state"] == "done", done
    assert done["error"] == "" and done["blocker"] == ""
    answer = done["answer_path"]
    assert os.path.isfile(answer)
    with open(answer, encoding="utf-8") as fh:
        assert fh.read() == good_letter()

    bundle = env.tmp / "state" / "copilot" / job["id"]
    assert (bundle / "facts.yaml").read_text(encoding="utf-8") == env.facts.read_text(encoding="utf-8")
    posting = (bundle / "posting.md").read_text(encoding="utf-8")
    assert "Acme" in posting and "Security Engineer" in posting and POSTING["url"] in posting
    assert "Run detection engineering" in posting
    assert not (bundle / "resume.txt").exists()  # packet has no resume.pdf

    (argv,) = calls_of(env)
    assert argv[0].startswith("Write a first-person cover letter")
    assert "Acme" in argv[0] and "Winnipeg, MB" in argv[0]
    assert argv[1:] == ["--context", str(bundle), "--max-chars", "200000"]
    assert cop.latest(UID)["id"] == job["id"]


def test_non_ascii_answer_fails_check(env, worker):
    cop = worker([{"exit": 0, "stdout": good_letter("caf\u00e9")}])
    job = wait_for(cop, cop.submit(UID, "letter")["id"], ("done", "failed"))
    assert job["state"] == "failed"
    assert job["error"].startswith("check_failed: ") and "non_ascii" in job["error"]
    assert os.path.isfile(env.tmp / "state" / "copilot" / job["id"] / "answer.txt")


def test_rate_limit_exit_5_sleeps_and_retries_once(env, worker, monkeypatch):
    sleeps = []
    monkeypatch.setattr(copilot.time, "sleep", lambda s: sleeps.append(s))
    cop = worker([{"exit": 5, "stderr": "quota gap\n"}, {"exit": 0, "stdout": good_letter()}])
    job = wait_for(cop, cop.submit(UID, "letter")["id"], ("done", "failed"))
    assert job["state"] == "done", job
    assert sleeps.count(65) == 1  # other threads may also sleep during the patch
    assert len(calls_of(env)) == 2


def test_rate_limit_twice_fails_with_last_stderr_line(env, worker, monkeypatch):
    monkeypatch.setattr(copilot.time, "sleep", lambda s: None)
    cop = worker([{"exit": 5, "stderr": "first\n"}, {"exit": 5, "stderr": "quota gap\n"}])
    job = wait_for(cop, cop.submit(UID, "gaps")["id"], ("done", "failed"))
    assert job["state"] == "failed" and job["error"] == "quota gap"
    assert len(calls_of(env)) == 2


def test_french_blocker_marks_job_blocked(env, worker):
    line = "FRENCH_BLOCKER: Must be bilingual in French"
    cop = worker([{"exit": 0, "stdout": line + "\n"}])
    job = wait_for(cop, cop.submit(UID, "letter")["id"], ("done", "failed"))
    assert job["state"] == "failed" and job["blocker"] == "french"
    assert job["error"] == line


def test_nonzero_exit_fails_with_stderr_tail(env, worker):
    cop = worker([{"exit": 1, "stderr": "boom\ntransport failure\n"}])
    job = wait_for(cop, cop.submit(UID, "critique")["id"], ("done", "failed"))
    assert job["state"] == "failed" and job["error"] == "transport failure"


def test_stage_lines_update_job_live_and_are_saved_and_logged(env, worker):
    plan = [{"stage_lines": ["# stage relaunch 0.5s", "# stage model 1.2s", "# stage await 3.0s"],
             "sleep": 1.0, "stdout": good_analysis()}]
    cop = worker(plan)
    jid = cop.submit(UID, "analysis")["id"]
    deadline = time.monotonic() + 10
    while len(cop.job(jid)["stages"]) < 3 and time.monotonic() < deadline:
        threading.Event().wait(0.02)
    live = cop.job(jid)
    assert live["state"] == "running" and live["stage"] == "await" and live["stage_secs"] == 3.0
    on_disk = {j["id"]: j for j in json.loads((env.tmp / "state" / "copilot" / "jobs.json")
                                              .read_text(encoding="utf-8"))["jobs"]}
    assert on_disk[jid]["stage"] == "await"  # the update was saved, not just held in memory

    done = wait_for(cop, jid, ("done", "failed"))
    assert done["state"] == "done", done
    assert [s["name"] for s in done["stages"]] == ["relaunch", "model", "await"]
    assert [s["secs"] for s in done["stages"]] == [0.5, 1.2, 3.0]
    assert done["stage"] == "" and done["stage_secs"] == 0.0
    run_log = env.tmp / "state" / "copilot" / jid / "run.log"
    assert run_log.read_text(encoding="utf-8").splitlines() == [
        "# stage relaunch 0.5s", "# stage model 1.2s", "# stage await 3.0s"]


def test_start_marker_shows_stage_without_duplicating_it(env, worker):
    # A fake run emits start and finished markers. 'await start' must show as the live stage and
    # must not add a stages entry; the finished 'context' line keeps its seconds.
    plan = [{"stage_lines": ["# stage context start", "# stage context 3.6s", "# stage await start"],
             "sleep": 1.0, "stdout": good_analysis()}]
    cop = worker(plan)
    jid = cop.submit(UID, "analysis")["id"]
    deadline = time.monotonic() + 10
    while cop.job(jid)["stage"] != "await" and time.monotonic() < deadline:
        threading.Event().wait(0.02)
    live = cop.job(jid)
    assert live["state"] == "running" and live["stage"] == "await", live
    assert live["stages"] == [{"name": "context", "secs": 3.6}]
    assert live["stage_secs"] == 3.6  # a start line leaves the last finished seconds alone

    done = wait_for(cop, jid, ("done", "failed"))
    assert done["state"] == "done", done
    assert [s["name"] for s in done["stages"]] == ["context"]
    assert done["stages"][0]["secs"] == 3.6
    run_log = env.tmp / "state" / "copilot" / jid / "run.log"
    assert run_log.read_text(encoding="utf-8").splitlines() == [
        "# stage context start", "# stage context 3.6s", "# stage await start"]


def test_no_await_line_means_no_await_stage(env, worker):
    cop = worker([{"exit": 0, "stage_lines": ["# stage relaunch 0.1s"], "stdout": good_analysis()}])
    job = _run(cop, "analysis")
    assert job["state"] == "done", job
    assert [s["name"] for s in job["stages"]] == ["relaunch"]


def test_timeout_kills_child_and_keeps_stage_list(env, worker, monkeypatch):
    monkeypatch.setattr(copilot, "CALL_TIMEOUT_S", 2)
    plan = [{"stage_lines": ["# stage relaunch 0.1s", "# stage model 0.2s", "# stage composer 0.3s"],
             "sleep": 60}]
    cop = worker(plan)
    job = _run(cop, "analysis")
    assert job["state"] == "failed" and job["error"] == "timeout", job
    assert [s["name"] for s in job["stages"]] == ["relaunch", "model", "composer"]
    assert job["stage"] == "" and job["finished"]
    pid = int((env.fake_dir / "pid").read_text(encoding="utf-8"))
    with pytest.raises(ProcessLookupError):  # the child was killed and reaped
        os.kill(pid, 0)


def test_failed_job_clears_stale_stage(env, worker):
    cop = worker([{"exit": 1, "stage_lines": ["# stage model 0.1s"], "stderr": "boom\n"}])
    job = _run(cop, "analysis")
    assert job["state"] == "failed" and job["stage"] == ""
    assert [s["name"] for s in job["stages"]] == ["model"]


def test_running_job_is_recovered_on_construction(env):
    jobs_path = env.tmp / "recover" / "jobs.json"
    jobs_path.parent.mkdir(parents=True)
    stuck = {"id": "abc123", "uid": UID, "kind": "letter", "state": "running",
             "created": "2026-11-23T10:00:00+00:00", "started": "2026-11-23T10:00:01+00:00",
             "finished": None, "error": "", "answer_path": "", "blocker": ""}
    queued = dict(stuck, id="def456", state="queued", started=None)
    jobs_path.write_text(json.dumps({"jobs": [stuck, queued]}), encoding="utf-8")
    cop = Copilot(jobs_path=jobs_path, packet_root=env.packets, facts_path=env.facts,
                  db_path=env.db, neon_cmd=make_fake(env, [{"exit": 0}]),
                  state_dir=env.tmp / "recover" / "state")
    recovered = cop.job("abc123")
    assert recovered["state"] == "failed"
    assert recovered["error"] == "interrupted_by_restart" and recovered["finished"]
    assert cop.job("def456")["state"] == "queued"
    on_disk = json.loads(jobs_path.read_text(encoding="utf-8"))["jobs"]
    assert {j["id"]: j["state"] for j in on_disk} == {"abc123": "failed", "def456": "queued"}
    assert calls_of(env) == []  # not started, so the stuck job did not rerun


# ---------- artifacts ----------

def _run(cop, kind, uid=UID):
    return wait_for(cop, cop.submit(uid, kind)["id"], ("done", "failed"))


def _status_events(env):
    st = Store(str(env.db))
    try:
        return st.get(UID)["status"], st.events(UID)
    finally:
        st.close()


def test_ligature_hits_names_only_fb00_to_fb06():
    assert copilot.ligature_hits("ef\ufb01cient \ufb00 \ufb06 plain") == ["U+FB00", "U+FB01", "U+FB06"]
    assert copilot.ligature_hits("efficient \ufb07 \ufaff") == []  # just outside the range


def test_letter_job_writes_body_renders_pdf_and_sets_prepped(env, worker, monkeypatch):
    calls = []

    def render(uid, body_path, out_dir=None, **kwargs):
        calls.append({"uid": uid, "body": Path(body_path).read_text(encoding="utf-8"),
                      "out_dir": Path(out_dir), **kwargs})
        out = Path(out_dir)
        (out / "cover_letter.pdf").write_bytes(b"%PDF new")
        (out / "cover_letter.tex").write_text("tex", encoding="utf-8")
        return str((out / "cover_letter.pdf").resolve())

    monkeypatch.setattr(copilot, "render_letter", render)
    cop = worker([{"exit": 0, "stdout": good_letter()}])
    job = _run(cop, "letter")
    assert job["state"] == "done", job
    pdf = env.packets / SLUG / "cover_letter.pdf"
    assert job["artifact_path"] == str(pdf)
    assert (env.bodies / f"{SLUG}.txt").read_text(encoding="utf-8") == good_letter()
    assert pdf.read_bytes() == b"%PDF new"
    assert (env.packets / SLUG / "cover_letter.tex").read_text(encoding="utf-8") == "tex"
    (call,) = calls
    assert call["uid"] == UID and call["body"] == good_letter()
    assert call["facts_path"] == str(env.facts)
    assert call["out_dir"] != env.packets / SLUG  # rendered in staging, then published
    status, events = _status_events(env)
    assert status == "prepped"
    assert [e["detail"] for e in events if e["kind"] == "status"] == ["new->prepped"]
    assert any(e["kind"] == "note" and job["id"] in e["detail"] for e in events)


def test_failed_render_keeps_previous_letter_and_fails_job(env, worker, monkeypatch):
    cop = worker([{"exit": 0, "stdout": good_letter("lorem")},
                  {"exit": 0, "stdout": good_letter("ipsum")}])
    first = _run(cop, "letter")
    assert first["state"] == "done", first
    pdf = env.packets / SLUG / "cover_letter.pdf"
    old_pdf = pdf.read_bytes()
    old_body = (env.bodies / f"{SLUG}.txt").read_text(encoding="utf-8")

    def boom(uid, body_path, out_dir=None, **kwargs):
        raise RuntimeError("tectonic failed (exit 1): ! LaTeX Error")
    monkeypatch.setattr(copilot, "render_letter", boom)
    second = _run(cop, "letter")
    assert second["state"] == "failed", second
    assert second["error"].startswith("artifact_failed: render_failed: ")
    assert "tectonic failed" in second["error"]
    assert second["artifact_path"] == "" and second["finished"]
    assert pdf.read_bytes() == old_pdf
    assert (env.bodies / f"{SLUG}.txt").read_text(encoding="utf-8") == old_body
    assert (env.bodies / f"{SLUG}.candidate.txt").read_text(encoding="utf-8") == good_letter("ipsum")
    status, events = _status_events(env)
    assert status == "prepped"  # the failed job did not log a second status move
    assert [e["detail"] for e in events if e["kind"] == "status"] == ["new->prepped"]


def test_ligature_in_pdf_fails_letter_and_keeps_previous(env, worker, monkeypatch):
    cop = worker([{"exit": 0, "stdout": good_letter("lorem")},
                  {"exit": 0, "stdout": good_letter("ipsum")}])
    assert _run(cop, "letter")["state"] == "done"
    pdf = env.packets / SLUG / "cover_letter.pdf"
    old_pdf = pdf.read_bytes()
    old_body = (env.bodies / f"{SLUG}.txt").read_text(encoding="utf-8")
    monkeypatch.setattr(copilot, "_pdf_text", lambda p: "e\ufb01cient\n")
    job = _run(cop, "letter")
    assert job["state"] == "failed" and "U+FB01" in job["error"], job
    assert job["error"].startswith("artifact_failed: ligature_in_pdf")
    assert pdf.read_bytes() == old_pdf
    assert (env.bodies / f"{SLUG}.txt").read_text(encoding="utf-8") == old_body


def test_pdftotext_failure_fails_closed(env, worker, monkeypatch):
    def broken(pdf):
        raise copilot.ArtifactError("pdftotext_failed: exit 1")
    monkeypatch.setattr(copilot, "_pdf_text", broken)
    cop = worker([{"exit": 0, "stdout": good_letter()}])
    job = _run(cop, "letter")
    assert job["state"] == "failed" and "pdftotext_failed" in job["error"]
    assert not (env.packets / SLUG / "cover_letter.pdf").exists()
    assert not (env.bodies / f"{SLUG}.txt").exists()
    assert _status_events(env)[0] == "new"


@pytest.mark.parametrize("kind", ["analysis", "critique", "gaps"])
def test_analysis_and_retired_names_write_analysis_md_and_leave_status_alone(env, worker, kind):
    text = good_analysis()
    cop = worker([{"exit": 0, "stdout": text}])
    job = _run(cop, kind)
    assert job["state"] == "done", job
    assert job["kind"] == "analysis"
    path = env.packets / SLUG / "analysis.md"
    assert job["artifact_path"] == str(path)
    assert path.read_text(encoding="utf-8") == text
    assert not (env.packets / SLUG / "cover_letter.pdf").exists()
    assert not (env.bodies / f"{SLUG}.txt").exists()
    status, events = _status_events(env)
    assert status == "new" and events == []


def test_failed_validation_overwrites_no_artifact(env, worker, monkeypatch):
    renders = []
    monkeypatch.setattr(copilot, "render_letter", lambda *a, **k: renders.append(a) or "")
    (env.packets / SLUG / "analysis.md").write_text("OLD analysis", encoding="utf-8")
    (env.packets / SLUG / "cover_letter.pdf").write_bytes(b"OLD pdf")
    env.bodies.mkdir()
    (env.bodies / f"{SLUG}.txt").write_text("OLD body", encoding="utf-8")
    cop = worker([{"exit": 0, "stdout": good_letter("caf\u00e9")},
                  {"exit": 0, "stdout": good_analysis().replace("lorem", "caf\u00e9", 1)}])
    letter = _run(cop, "letter")
    analysis = _run(cop, "critique")  # a retired name still goes through the same checks
    assert letter["state"] == "failed" and letter["error"].startswith("check_failed: ")
    assert analysis["state"] == "failed" and analysis["error"].startswith("check_failed: ")
    assert (env.packets / SLUG / "analysis.md").read_text(encoding="utf-8") == "OLD analysis"
    assert (env.packets / SLUG / "cover_letter.pdf").read_bytes() == b"OLD pdf"
    assert (env.bodies / f"{SLUG}.txt").read_text(encoding="utf-8") == "OLD body"
    assert renders == []
    assert _status_events(env)[0] == "new"


def test_queued_legacy_critique_job_on_disk_runs_as_analysis(env, worker):
    legacy = {"id": "legacy01", "uid": UID, "kind": "critique", "state": "queued",
              "created": "2026-11-23T10:00:00+00:00", "started": None, "finished": None,
              "error": "", "answer_path": "", "artifact_path": "", "blocker": "", "message": ""}
    path = env.tmp / "legacy" / "copilot" / "jobs.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"jobs": [legacy]}), encoding="utf-8")
    cop = worker([{"exit": 0, "stdout": good_analysis()}], state="legacy")
    job = wait_for(cop, "legacy01", ("done", "failed"))
    assert job["state"] == "done", job
    assert copilot._kind_of(job) == "analysis"  # the record keeps its old kind; it runs as analysis
    assert (env.packets / SLUG / "analysis.md").is_file()
    assert job["stages"] == [] and job["stage"] == ""


def test_default_bodies_dir_is_under_home():
    cop = Copilot(jobs_path=Path("/nonexistent/j.json"))
    assert cop.bodies_dir == Path.home() / "jobsearch" / "bodies"


# ---------- prep (no-AI packet build) ----------

PREP_OK = (
    "import sys\n"
    "print('build.sh general ok')\n"
    "print('packet dir: /tmp/fake/packets/built')\n"
    "sys.exit(0)\n"
)


def _prep_cop(env, worker, monkeypatch, code, *, plan=None):
    """Started worker whose prep runs `python -c CODE` instead of the real CLI (no LaTeX)."""
    cop = worker(plan or [{"exit": 0}])
    monkeypatch.setattr(cop, "_prep_command", lambda uid: [sys.executable, "-c", code])
    return cop


def test_prep_is_an_accepted_kind_and_stages_nothing_when_queued(env):
    cop = make_cop(env, make_fake(env, [{"exit": 0}]))
    job = cop.submit(UID, "prep")
    assert job["kind"] == "prep" and job["state"] == "queued"
    assert cop.latest(UID)["id"] == job["id"]
    assert not (env.tmp / "state" / "copilot" / job["id"]).exists()


def test_prep_exit_0_marks_done_with_packet_dir_and_leaves_store_alone(env, worker, monkeypatch):
    cop = _prep_cop(env, worker, monkeypatch, PREP_OK)
    job = _run(cop, "prep")
    assert job["state"] == "done", job
    assert job["artifact_path"] == str(env.packets / SLUG)
    assert job["message"] == "packet dir: /tmp/fake/packets/built"
    assert job["error"] == ""
    assert not (env.tmp / "state" / "copilot" / job["id"]).exists()  # no bundle staged
    assert calls_of(env) == []  # neon never called
    assert _status_events(env) == ("new", [])  # the store is untouched by this branch


def test_prep_exit_1_marks_failed_and_leaves_posting_status(env, worker, monkeypatch):
    code = ("import sys\n"
            "print('build.sh general failed (exit 1)')\n"
            "sys.stderr.write('boom\\nno variant built\\n')\n"
            "sys.exit(1)\n")
    cop = _prep_cop(env, worker, monkeypatch, code)
    job = _run(cop, "prep")
    assert job["state"] == "failed" and job["error"] == "no variant built", job
    assert job["artifact_path"] == "" and job["finished"]
    assert _status_events(env) == ("new", [])


def test_prep_failure_with_only_stdout_uses_last_stdout_line(env, worker, monkeypatch):
    code = "import sys\nprint('refused: all variants failed')\nsys.exit(2)\n"
    cop = _prep_cop(env, worker, monkeypatch, code)
    job = _run(cop, "prep")
    assert job["state"] == "failed" and job["error"] == "refused: all variants failed"
    assert _status_events(env) == ("new", [])


def test_prep_timeout_fails_job_and_leaves_status(env, worker, monkeypatch):
    monkeypatch.setattr(copilot, "PREP_TIMEOUT_S", 1)
    cop = _prep_cop(env, worker, monkeypatch, "import time\ntime.sleep(30)\n")
    job = _run(cop, "prep")
    assert job["state"] == "failed" and job["error"] == "prep_timeout"
    assert _status_events(env) == ("new", [])


def test_prep_runs_cli_module_with_this_interpreter_in_project_root(env, worker, monkeypatch):
    seen = []

    def fake_run(args, **kwargs):
        seen.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout="packet dir: x\n", stderr="")
    monkeypatch.setattr(copilot.subprocess, "run", fake_run)
    cop = worker([{"exit": 0}])
    job = _run(cop, "prep")
    assert job["state"] == "done", job
    (args, kwargs), = seen
    assert args == [sys.executable, "-m", "jobsearch.cli", "prep", UID]
    assert kwargs["cwd"] == str(copilot.PROJECT_ROOT) == str(Path(copilot.__file__).resolve().parents[1])
    assert kwargs["timeout"] == 600 == copilot.PREP_TIMEOUT_S
    assert kwargs["capture_output"] is True
    assert calls_of(env) == []


# ---------- letter status guard ----------

def test_letter_job_on_shortlisted_posting_sets_prepped(env, worker):
    st = Store(str(env.db))
    st.set_status(UID, "shortlisted", "triage")
    st.close()
    cop = worker([{"exit": 0, "stdout": good_letter()}])
    job = _run(cop, "letter")
    assert job["state"] == "done", job
    status, events = _status_events(env)
    assert status == "prepped"
    assert [e["detail"] for e in events if e["kind"] == "status"] == ["new->shortlisted", "shortlisted->prepped"]
    assert any(e["kind"] == "note" and job["id"] in e["detail"] and "without status change" not in e["detail"]
               for e in events)


def test_letter_job_on_applied_posting_keeps_status_and_logs_note(env, worker):
    st = Store(str(env.db))
    st.set_status(UID, "applied", "sent")
    st.close()
    cop = worker([{"exit": 0, "stdout": good_letter()}])
    job = _run(cop, "letter")
    assert job["state"] == "done", job
    assert (env.packets / SLUG / "cover_letter.pdf").is_file()
    status, events = _status_events(env)
    assert status == "applied"
    assert [e["detail"] for e in events if e["kind"] == "status"] == ["new->applied", "applied->applied"]
    notes = [e["detail"] for e in events if e["kind"] == "note" and job["id"] in e["detail"]]
    assert len(notes) == 1 and "rendered without status change" in notes[0] and "posting at applied" in notes[0]


# ---------- hand edit: publish_body (no model) ----------

def test_publish_body_writes_pdf_and_returns_its_path(env):
    cop = make_cop(env, "unused-neon")
    pdf = cop.publish_body(UID, "Dear team,\nhand edited.\n")
    assert pdf == env.packets / SLUG / "cover_letter.pdf"
    assert pdf.read_bytes() == b"%PDF fake Dear team,\nhand edited.\n"  # fake render saw the new text
    assert (env.packets / SLUG / "cover_letter.tex").is_file()
    assert (env.bodies / f"{SLUG}.txt").read_text(encoding="utf-8") == "Dear team,\nhand edited.\n"
    assert not list((env.bodies / ".render").glob("*"))  # staging cleaned up


def test_publish_body_records_event_naming_posting_and_hand_edit(env):
    cop = make_cop(env, "unused-neon")
    cop.publish_body(UID, "Dear team.\n")
    status, events = _status_events(env)
    assert status == "prepped"
    assert [e["detail"] for e in events if e["kind"] == "status"] == ["new->prepped"]
    assert any(e["kind"] == "note" and UID in e["detail"] and "edited by hand" in e["detail"]
               for e in events)


def test_publish_body_on_applied_posting_keeps_status(env):
    st = Store(str(env.db))
    st.set_status(UID, "applied", "sent")
    st.close()
    cop = make_cop(env, "unused-neon")
    cop.publish_body(UID, "hand edit\n")
    status, events = _status_events(env)
    assert status == "applied"
    assert any(e["kind"] == "note" and "edited by hand" in e["detail"]
               and "without status change" in e["detail"] for e in events)


def test_publish_body_rejects_empty_and_non_ascii_before_writing(env):
    cop = make_cop(env, "unused-neon")
    for text in ("", "   \n\t", None):
        with pytest.raises(ValueError, match="empty"):
            cop.publish_body(UID, text)
    with pytest.raises(ValueError, match="non-ASCII.*U\\+00EF"):
        cop.publish_body(UID, "na\u00efve\n")
    with pytest.raises(ValueError, match="non-ASCII.*U\\+2019"):
        cop.publish_body(UID, "smart \u2019 quote\n")
    assert not (env.packets / SLUG / "cover_letter.pdf").exists()
    assert not (env.bodies / f"{SLUG}.candidate.txt").exists()
    assert not (env.bodies / f"{SLUG}.txt").exists()
    assert _status_events(env) == ("new", [])


def test_publish_body_unknown_uid_is_keyerror_and_writes_nothing(env):
    cop = make_cop(env, "unused-neon")
    with pytest.raises(KeyError):
        cop.publish_body("nope:none/0000", "text\n")
    assert not (env.bodies / "nope_none_0000.candidate.txt").exists()


def test_publish_body_normalises_crlf_from_browser_textarea(env):
    cop = make_cop(env, "unused-neon")
    cop.publish_body(UID, "line one\r\nline two\r\n")
    assert (env.bodies / f"{SLUG}.txt").read_bytes() == b"line one\nline two\n"


def test_publish_body_failed_render_keeps_previous_pdf_tex_and_body(env, monkeypatch):
    cop = make_cop(env, "unused-neon")
    cop.publish_body(UID, "first version\n")
    pdf = env.packets / SLUG / "cover_letter.pdf"
    tex = env.packets / SLUG / "cover_letter.tex"
    old_pdf, old_tex = pdf.read_bytes(), tex.read_bytes()

    def boom(uid, body_path, out_dir=None, **kwargs):
        raise RuntimeError("tectonic failed (exit 1): ! LaTeX Error")
    monkeypatch.setattr(copilot, "render_letter", boom)
    with pytest.raises(copilot.ArtifactError, match="render_failed: .*tectonic failed"):
        cop.publish_body(UID, "second version\n")
    assert pdf.read_bytes() == old_pdf and tex.read_bytes() == old_tex
    assert (env.bodies / f"{SLUG}.txt").read_text(encoding="utf-8") == "first version\n"
    status, events = _status_events(env)
    assert status == "prepped"
    assert [e["detail"] for e in events if e["kind"] == "status"] == ["new->prepped"]


def test_publish_body_ligature_in_pdf_keeps_previous(env, monkeypatch):
    cop = make_cop(env, "unused-neon")
    cop.publish_body(UID, "first\n")
    pdf = env.packets / SLUG / "cover_letter.pdf"
    old_pdf = pdf.read_bytes()
    monkeypatch.setattr(copilot, "_pdf_text", lambda p: "e\ufb01cient\n")
    with pytest.raises(copilot.ArtifactError, match="ligature_in_pdf.*U\\+FB01"):
        cop.publish_body(UID, "second\n")
    assert pdf.read_bytes() == old_pdf
    assert (env.bodies / f"{SLUG}.txt").read_text(encoding="utf-8") == "first\n"


def test_multi_word_stage_name_is_recorded(env, worker):
    plan = [{"stage_lines": ["# stage relaunch retry 0.4s", "# stage model start"],
             "sleep": 1.0, "stdout": good_analysis()}]
    cop = worker(plan)
    jid = cop.submit(UID, "analysis")["id"]
    deadline = time.monotonic() + 10
    while cop.job(jid)["stage"] != "model" and time.monotonic() < deadline:
        threading.Event().wait(0.02)
    live = cop.job(jid)
    assert live["stage"] == "model" and live["stages"] == [{"name": "relaunch retry", "secs": 0.4}]
    done = wait_for(cop, jid, ("done", "failed"))
    assert done["state"] == "done", done
    assert done["stages"] == [{"name": "relaunch retry", "secs": 0.4}]


def test_non_stage_stderr_line_lands_in_run_log(env, worker):
    plan = [{"stage_lines": ["# stage relaunch 0.1s"], "stderr": "warning: slow upstream\n",
             "stdout": good_analysis()}]
    cop = worker(plan)
    jid = cop.submit(UID, "analysis")["id"]
    done = wait_for(cop, jid, ("done", "failed"))
    assert done["state"] == "done", done
    assert done["stages"] == [{"name": "relaunch", "secs": 0.1}]
    run_log = env.tmp / "state" / "copilot" / jid / "run.log"
    assert run_log.read_text(encoding="utf-8").splitlines() == [
        "# stage relaunch 0.1s", "warning: slow upstream"]


def test_repeated_finished_marker_does_not_duplicate_stage(env, worker):
    cop = worker([{"exit": 0, "stage_lines": ["# stage model 0.1s", "# stage model 0.2s"],
                   "stdout": good_analysis()}])
    job = _run(cop, "analysis")
    assert job["state"] == "done", job
    assert job["stages"] == [{"name": "model", "secs": 0.2}]


# ---------- facts_update ----------

FACTS_START = 'version: 1\ncontact:\n  name: "Alex Morgan"\n'
LOCATION_EDIT = {"status": "propose", "target": "facts.yaml",
                 "summary": "Set the contact location to Winnipeg, MB.",
                 "ops": [{"op": "upsert", "path": "contact.location", "value": "Winnipeg, MB"}]}
PREFS_EDIT = {"status": "propose", "target": "preferences.yaml",
              "summary": "Open to relocation anywhere in Canada.",
              "ops": [{"op": "upsert", "path": "relocation.countries", "value": ["CA"]}]}


def facts_answer(base=None, **over):
    return json.dumps({**(base or PREFS_EDIT), **over})


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _git(resume, *args):
    return subprocess.run(["git", "-C", str(resume), *args], capture_output=True, text=True,
                          check=True).stdout


# The shipped default runs no checker scripts (a reader has none); these tests exercise the
# mechanism, so they point the module constant at scripts they write themselves.
CHECKERS = ("check_profile.py", "verify_profile.py")


@pytest.fixture(autouse=True)
def _checkers(monkeypatch):
    monkeypatch.setattr(copilot, "FACTS_CHECKS", CHECKERS)


def make_resume(tmp_path, fail=None):
    """A throwaway resume repo in tmp_path: facts.yaml plus both checkers, committed.

    fail names one checker that exits 3 with a line of output; the other exits 0.
    """
    resume = tmp_path / "resume"
    resume.mkdir()
    (resume / "facts.yaml").write_text(FACTS_START, encoding="utf-8")
    for name in CHECKERS:
        if name == fail:
            code = f"import sys\nprint('{name}: FAILED on purpose')\nsys.exit(3)\n"
        else:
            code = "import sys\nsys.exit(0)\n"
        (resume / name).write_text(code, encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(resume)], check=True)
    # Not @example.*: the host's global commit-identity hook refuses example domains.
    _git(resume, "config", "user.email", "facts-test@invalid.test")
    _git(resume, "config", "user.name", "Tester")
    _git(resume, "add", "-A")
    _git(resume, "commit", "-q", "-m", "init")
    return resume


@pytest.fixture
def facts_worker(env):
    started = []

    def _make(resume, plan, start=True):
        cop = Copilot(jobs_path=env.tmp / "state" / "copilot" / "jobs.json",
                      packet_root=env.packets, facts_path=resume / "facts.yaml",
                      prefs_path=resume / "preferences.yaml", db_path=env.db,
                      neon_cmd=make_fake(env, plan), state_dir=env.tmp / "state" / "copilot",
                      bodies_dir=env.bodies)
        started.append(cop)
        if start:
            cop.start()
        return cop

    yield _make
    for cop in started:
        cop.stop()


def propose_facts(cop, instruction, answer):
    jid = cop.submit(FACTS_UID, FACTS_KIND, instruction=instruction)["id"]
    return wait_for(cop, jid, ("pending_review", "done", "failed"))


def test_facts_submit_needs_no_posting_and_dedupes(env, facts_worker, tmp_path):
    resume = make_resume(tmp_path)
    cop = facts_worker(resume, [{"exit": 0, "stdout": facts_answer(status="no_change", ops=[])}],
                       start=False)
    first = cop.submit(FACTS_UID, FACTS_KIND, instruction="  I relocate anywhere in Canada.  ")
    second = cop.submit(FACTS_UID, FACTS_KIND, instruction="something else, same queue")
    assert second["id"] == first["id"]
    assert first["uid"] == FACTS_UID and first["kind"] == FACTS_KIND
    assert first["state"] == "queued" and first["est_seconds"] == 150
    assert first["instruction"] == "I relocate anywhere in Canada."
    assert len(cop.jobs()) == 1
    cop.start()
    done = wait_for(cop, first["id"], ("done", "failed"))
    assert done["state"] == "done" and done["proposal_state"] == "no_change", done


def test_facts_submit_rejects_bad_input(env, facts_worker, tmp_path):
    resume = make_resume(tmp_path)
    cop = facts_worker(resume, [{"exit": 0, "stdout": "{}"}], start=False)
    with pytest.raises(ValueError, match="takes uid"):
        cop.submit(UID, FACTS_KIND, instruction="x")
    with pytest.raises(ValueError, match="non-empty instruction"):
        cop.submit(FACTS_UID, FACTS_KIND, instruction="   ")
    with pytest.raises(ValueError, match="over 1000"):
        cop.submit(FACTS_UID, FACTS_KIND, instruction="x" * 1001)
    with pytest.raises(ValueError, match="unknown kind"):
        cop.submit(FACTS_UID, "bogus", instruction="x")
    assert cop.jobs() == []


def test_facts_bundle_carries_no_posting_text(env, facts_worker, tmp_path):
    resume = make_resume(tmp_path)
    cop = facts_worker(resume, [{"exit": 0, "stdout": facts_answer()}])
    job = propose_facts(cop, "I am open to relocation anywhere in Canada.", None)
    assert job["state"] == "pending_review", job
    bundle = env.tmp / "state" / "copilot" / job["id"]
    names = sorted(p.name for p in bundle.iterdir())
    assert {"facts.yaml", "preferences.yaml", "instruction.txt", "answer.txt"} <= set(names)
    for path in bundle.iterdir():
        text = path.read_text(encoding="utf-8")
        for leak in ("Acme", "Security Engineer", "Run detection engineering", "posting.md"):
            assert leak not in text, (path.name, leak)
    assert (bundle / "instruction.txt").read_text(encoding="utf-8") == \
        "I am open to relocation anywhere in Canada."


def test_facts_prompt_carries_instruction_and_both_files(env, facts_worker, tmp_path):
    resume = make_resume(tmp_path)
    (resume / "preferences.yaml").write_text("version: 1\nrelocation:\n  countries: [CA]\n",
                                             encoding="utf-8")
    cop = facts_worker(resume, [{"exit": 0, "stdout": facts_answer(status="no_change", ops=[])}])
    job = propose_facts(cop, "Open to relocation anywhere in Canada.", None)
    assert job["state"] == "done", job
    (argv,) = calls_of(env)
    prompt = argv[0]
    assert "Open to relocation anywhere in Canada." in prompt
    assert 'name: "Alex Morgan"' in prompt  # current facts.yaml, as plain text
    assert "countries: [CA]" in prompt       # current preferences.yaml, as plain text
    assert "Acme" not in prompt and "Security Engineer" not in prompt
    assert "ONE raw JSON object" in prompt and "ALLOWED PATHS (preferences.yaml)" in prompt
    assert argv[1:] == ["--context", str(env.tmp / "state" / "copilot" / job["id"]),
                        "--max-chars", "200000"]
    assert all(ord(ch) < 128 for ch in copilot.PROMPTS[FACTS_KIND])


@pytest.mark.parametrize("answer, instruction, expected", [
    ("Sure, here it is: " + json.dumps(PREFS_EDIT) + " Hope that helps.",
     "Open to relocation anywhere in Canada.", "invalid_json"),
    ("```json\n" + json.dumps(PREFS_EDIT) + "\n```", "Open to relocation anywhere in Canada.",
     "invalid_json"),
    (facts_answer(summary="Open to relocation anywhere in Canada \u2014 yes."),
     "Open to relocation anywhere in Canada.", "non_ascii"),
    (facts_answer(LOCATION_EDIT, ops=[{"op": "upsert", "path": "contact.phone",
                                       "value": "403-555-0199"}], target="facts.yaml"),
     "Update my phone number.", "unsupported_number"),
    (facts_answer(ops=[{"op": "upsert", "path": "relocation.bogus", "value": "x"}]),
     "Open to relocation anywhere in Canada.", "path_not_allowed"),
])
def test_facts_validator_rejections_fail_and_write_nothing(env, facts_worker, tmp_path,
                                                           answer, instruction, expected):
    resume = make_resume(tmp_path)
    facts_before = (resume / "facts.yaml").read_bytes()
    cop = facts_worker(resume, [{"exit": 0, "stdout": answer}])
    job = propose_facts(cop, instruction, answer)
    assert job["state"] == "failed", job
    assert job["error"].startswith("check_failed: ") and expected in job["error"], job["error"]
    assert (resume / "facts.yaml").read_bytes() == facts_before
    assert not (resume / "preferences.yaml").exists()
    assert not any(p.name.endswith(".tmp") for p in resume.iterdir())


def test_facts_propose_rewrites_a_set_on_an_absent_key_into_an_upsert(env, facts_worker, tmp_path):
    """Observed live: the model answered "set" for a preferences file that did not exist yet, and
    the instruction died as path_absent. A set on an absent key must propose, not fail."""
    resume = make_resume(tmp_path)
    answer = facts_answer(ops=[{"op": "set", "path": "relocation.countries", "value": ["CA"]},
                               {"op": "set", "path": "work_authorization.US.status",
                                "value": "requires_sponsorship"}])
    cop = facts_worker(resume, [{"exit": 0, "stdout": answer}])
    job = propose_facts(cop, "Open to relocation anywhere in Canada, US needs sponsorship.", None)
    assert job["state"] == "pending_review", job
    assert job["upgraded"] == ["relocation.countries", "work_authorization.US.status"]
    assert [o["op"] for o in job["ops"]] == ["upsert", "upsert"]
    assert "countries: [CA]" in job["new_text"]
    assert not (resume / "preferences.yaml").exists()


def test_facts_propose_lands_pending_review_and_leaves_files_unchanged(env, facts_worker, tmp_path):
    resume = make_resume(tmp_path)
    facts_before = (resume / "facts.yaml").read_bytes()
    cop = facts_worker(resume, [{"exit": 0, "stdout": facts_answer()}])
    job = propose_facts(cop, "Open to relocation anywhere in Canada.", None)
    assert job["state"] == "pending_review", job
    assert job["proposal_state"] == "pending_review" and job["target"] == "preferences.yaml"
    assert job["diff"].startswith("--- a/preferences.yaml\n+++ b/preferences.yaml\n")
    assert "countries" in job["diff"]
    assert job["base_sha256"] == _sha(b"") and job["base_existed"] is False
    assert job["new_text"].startswith("version: 1\n")
    assert (resume / "facts.yaml").read_bytes() == facts_before
    assert not (resume / "preferences.yaml").exists()
    bundle = env.tmp / "state" / "copilot" / job["id"]
    assert json.loads((bundle / "proposal.json").read_text(encoding="utf-8"))["new_text"] == \
        job["new_text"]


@pytest.mark.parametrize("status, extra", [
    ("no_change", {"ops": []}),
    ("clarify", {"ops": [], "question": "Which city do you mean?"}),
    ("refuse", {"ops": [], "reason": "No evidence was supplied for that metric."}),
])
def test_facts_no_change_clarify_refuse_finish_done(env, facts_worker, tmp_path, status, extra):
    resume = make_resume(tmp_path)
    facts_before = (resume / "facts.yaml").read_bytes()
    answer = facts_answer(PREFS_EDIT, status=status, **extra)
    cop = facts_worker(resume, [{"exit": 0, "stdout": answer}])
    job = propose_facts(cop, "Open to relocation anywhere in Canada.", None)
    assert job["state"] == "done" and job["proposal_state"] == status, job
    assert job["error"] == "" and job["summary"]
    assert job.get("question", "") == extra.get("question", "")
    assert job.get("reason", "") == extra.get("reason", "")
    assert (resume / "facts.yaml").read_bytes() == facts_before
    assert not (resume / "preferences.yaml").exists()


def test_accept_with_stale_sha_refuses_and_writes_nothing(env, facts_worker, tmp_path):
    resume = make_resume(tmp_path)
    cop = facts_worker(resume, [{"exit": 0, "stdout": facts_answer(LOCATION_EDIT)}])
    job = propose_facts(cop, "Set my location to Winnipeg, MB.", None)
    assert job["state"] == "pending_review", job
    jid, base = job["id"], job["base_sha256"]
    assert base == _sha(FACTS_START.encode("utf-8"))

    hand_edit = FACTS_START.encode("utf-8") + b"# hand edit\n"
    (resume / "facts.yaml").write_bytes(hand_edit)
    with pytest.raises(FactsStaleError):
        cop.accept_facts(jid, base)  # the reviewer saw the old file, which is gone
    assert (resume / "facts.yaml").read_bytes() == hand_edit
    assert cop.job(jid)["state"] == "pending_review"
    with pytest.raises(FactsStaleError):
        cop.accept_facts(jid, _sha(hand_edit))  # a sha the reviewer never saw in the preview
    assert (resume / "facts.yaml").read_bytes() == hand_edit
    assert _git(resume, "rev-list", "--count", "HEAD").strip() == "1"


def test_accept_with_right_sha_writes_commits_and_leaves_clean_tree(env, facts_worker, tmp_path):
    resume = make_resume(tmp_path)
    cop = facts_worker(resume, [{"exit": 0, "stdout": facts_answer(LOCATION_EDIT)}])
    job = propose_facts(cop, "Set my location to Winnipeg, MB.", None)
    assert job["state"] == "pending_review", job
    done = cop.accept_facts(job["id"], job["base_sha256"])
    assert done["state"] == "applied", done
    assert (resume / "facts.yaml").read_text(encoding="utf-8") == job["new_text"]
    assert "Winnipeg, MB" in (resume / "facts.yaml").read_text(encoding="utf-8")
    head = _git(resume, "rev-parse", "HEAD").strip()
    assert done["commit_sha"] == head and len(head) == 40
    assert _git(resume, "log", "-1", "--format=%s").strip() == \
        'facts-ui: "Set my location to Winnipeg, MB."'
    assert _git(resume, "rev-list", "--count", "HEAD").strip() == "2"
    assert _git(resume, "status", "--porcelain") == ""


def test_accept_preferences_creates_file_and_skips_facts_checkers(env, facts_worker, tmp_path):
    resume = make_resume(tmp_path, fail="check_profile.py")  # would fail a facts.yaml commit
    cop = facts_worker(resume, [{"exit": 0, "stdout": facts_answer()}])
    job = propose_facts(cop, "Open to relocation anywhere in Canada.", None)
    assert job["state"] == "pending_review", job
    done = cop.accept_facts(job["id"], job["base_sha256"])
    assert done["state"] == "applied", done
    assert (resume / "preferences.yaml").read_text(encoding="utf-8") == job["new_text"]
    assert _git(resume, "log", "-1", "--format=%s").strip().startswith("facts-ui: ")
    assert _git(resume, "status", "--porcelain") == ""


@pytest.mark.parametrize("fail", CHECKERS)
def test_accept_checker_failure_restores_previous_bytes(env, facts_worker, tmp_path, fail):
    resume = make_resume(tmp_path, fail=fail)
    old = (resume / "facts.yaml").read_bytes()
    cop = facts_worker(resume, [{"exit": 0, "stdout": facts_answer(LOCATION_EDIT)}])
    job = propose_facts(cop, "Set my location to Winnipeg, MB.", None)
    assert job["state"] == "pending_review", job
    done = cop.accept_facts(job["id"], job["base_sha256"])
    assert done["state"] == "rejected", done
    assert f"{fail} exit 3" in done["error"] and f"{fail}: FAILED on purpose" in done["error"]
    assert (resume / "facts.yaml").read_bytes() == old
    assert not (resume / "facts.yaml.tmp").exists()
    assert _git(resume, "rev-list", "--count", "HEAD").strip() == "1"
    assert _git(resume, "status", "--porcelain") == ""


def test_accept_git_failure_removes_new_preferences_file(env, facts_worker, tmp_path):
    resume = make_resume(tmp_path)
    # core.hooksPath is global on this host; point this repo at its own hooks for the test.
    _git(resume, "config", "core.hooksPath", str(resume / ".git" / "hooks"))
    hook = resume / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\necho 'hook refused' >&2\nexit 1\n", encoding="utf-8")
    os.chmod(hook, 0o755)
    cop = facts_worker(resume, [{"exit": 0, "stdout": facts_answer()}])
    job = propose_facts(cop, "Open to relocation anywhere in Canada.", None)
    done = cop.accept_facts(job["id"], job["base_sha256"])
    assert done["state"] == "rejected" and "git commit failed" in done["error"], done
    assert "hook refused" in done["error"]
    assert not (resume / "preferences.yaml").exists()
    assert _git(resume, "rev-list", "--count", "HEAD").strip() == "1"
    assert _git(resume, "status", "--porcelain") == ""


def test_reject_marks_rejected_and_writes_nothing(env, facts_worker, tmp_path):
    resume = make_resume(tmp_path)
    facts_before = (resume / "facts.yaml").read_bytes()
    cop = facts_worker(resume, [{"exit": 0, "stdout": facts_answer(LOCATION_EDIT)}])
    job = propose_facts(cop, "Set my location to Winnipeg, MB.", None)
    rejected = cop.reject_facts(job["id"])
    assert rejected["state"] == "rejected" and rejected["proposal_state"] == "rejected", rejected
    assert (resume / "facts.yaml").read_bytes() == facts_before
    assert _git(resume, "rev-list", "--count", "HEAD").strip() == "1"
    with pytest.raises(FactsStateError):
        cop.accept_facts(job["id"], job["base_sha256"])
    with pytest.raises(FactsStateError):
        cop.reject_facts(job["id"])
