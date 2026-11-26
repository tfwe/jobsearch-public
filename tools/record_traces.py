#!/usr/bin/env python3
"""Record the reach-gate (HTTP 409) and LLM-output-validator traces, offline.

    python tools/build_demo_corpus.py          # first: writes demo/corpus/corpus.json
    python tools/record_traces.py              # writes the two traces and SHA256SUMS

Section A (reach gate). Each posting in demo/corpus/corpus.json is loaded into a throwaway
store. reach.classify runs on it directly, then the real HTTP server (serve.make_server on
127.0.0.1, port 0) takes one POST per posting to /api/postings/UID/action with kind=analysis
and Basic auth, exactly as tests/test_offline_dispatch.py does. Out-of-reach postings answer
409 with a reach verdict in the body; the others are queued and run through the stub model
CLI. One out-of-reach posting is then reposted with force=1 and its job is followed to its
end state.

Section B (validator). copilot.validate runs on the stub's three canned outputs, then on
seeded-bad variants of the stub's letter, one per rule.

Outputs (all ASCII, LF): demo/corpus/trace-reach-409.txt, demo/corpus/trace-validator.txt,
demo/corpus/SHA256SUMS. Nothing in the outputs depends on the clock, a port number, a job id
or a temp path, so two runs produce identical bytes.

Uses only the mirror's own code and stdlib. Throwaway trees live under /tmp and are removed.
"""
from __future__ import annotations

import base64
import hashlib
import http.client
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from jobsearch import reach, serve  # noqa: E402
from jobsearch.copilot import FRENCH_TOKEN, Copilot, validate  # noqa: E402
from jobsearch.store import Store  # noqa: E402

CORPUS_DIR = ROOT / "demo" / "corpus"
CORPUS = CORPUS_DIR / "corpus.json"
TRACE_REACH = CORPUS_DIR / "trace-reach-409.txt"
TRACE_VALIDATOR = CORPUS_DIR / "trace-validator.txt"
SUMS = CORPUS_DIR / "SHA256SUMS"
STUB = ROOT / "tools" / "stub-model-cli.sh"
FACTS = ROOT / "tests" / "fixture_facts.yaml"

USER = "tester"
PASSWORD = "not-a-real-password"
FORM = {"Content-Type": "application/x-www-form-urlencoded"}
SLUG_OF = lambda uid: uid.replace(":", "_").replace("/", "_")  # noqa: E731  (packet dir name)
JOB_WAIT_S = 300.0

# Stub prompts: one per canned output. Each contains the keyword the stub matches on.
STUB_PROMPTS = (
    ("letter", "Write a first-person cover letter body for the applicant."),
    ("analysis", "Assess the applicant's fit for the role in posting.md."),
    ("questions", "Draft answers to the employer's application questions."),
)


def _auth() -> dict:
    raw = f"{USER}:{PASSWORD}".encode("utf-8")
    return {"Authorization": "Basic " + base64.b64encode(raw).decode("ascii")}


def _post(port: int, path: str, body: bytes, headers: dict) -> tuple[int, bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    try:
        conn.request("POST", path, body=body, headers=headers)
        resp = conn.getresponse()
        return resp.status, resp.read()
    finally:
        conn.close()


def _wait_idle(cop: Copilot, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if all(j["state"] not in ("queued", "running") for j in cop.jobs()):
            return
        time.sleep(0.05)
    raise RuntimeError("copilot jobs did not settle")


def _wait_uid(cop: Copilot, uid: str, timeout: float) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = cop.latest(uid)
        if job and job["state"] in ("done", "failed"):
            return job
        time.sleep(0.05)
    raise RuntimeError(f"job for {uid} did not finish")


def load_corpus() -> list[dict]:
    data = json.loads(CORPUS.read_text(encoding="utf-8"))
    postings = data["postings"]
    if len(postings) != data["posting_count"]:
        raise SystemExit("corpus.json posting_count does not match its postings")
    return postings


def section_a(postings: list[dict], work: Path) -> tuple[list[str], dict]:
    """Reach gate: direct classify, then the real server's 409 / 200 for every posting."""
    packets = work / "packets"
    (packets).mkdir(parents=True)
    dashboard = work / "dashboard.html"
    dashboard.write_text("<!DOCTYPE html><p>dash</p>\n", encoding="utf-8")
    for p in postings:  # the stub writes into a packet dir per job; create them up front
        (packets / SLUG_OF(p["uid"])).mkdir(parents=True, exist_ok=True)

    facts = work / "facts.yaml"
    facts.write_text('version: 1\ncontact:\n  name: "Alex Morgan"\n', encoding="utf-8")
    db = work / "jobs.db"
    store = Store(str(db))
    new, _ = store.upsert_postings(postings)
    store.close()
    assert new == len(postings), (new, len(postings))

    saved = (serve.PACKETS_ROOT, serve.DASHBOARD_PATH)
    serve.PACKETS_ROOT, serve.DASHBOARD_PATH = packets, dashboard
    state = work / "copilot"
    cop = Copilot(jobs_path=state / "jobs.json", packet_root=packets, facts_path=facts,
                  db_path=db, neon_cmd=str(STUB), state_dir=state,
                  bodies_dir=work / "bodies")
    srv = None
    thread = None
    lines: list[str] = []
    counts = {"409": 0, "200": 0, "mismatch": 0, "by_verdict": {}}
    try:
        cop.start()
        srv = serve.make_server("127.0.0.1", 0, USER, PASSWORD, db_path=str(db), copilot=cop)
        thread = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05},
                                  daemon=True)
        thread.start()
        port = srv.server_address[1]
        headers = {**_auth(), **FORM}

        lines.append("# Section A: reach gate. One line per posting, in corpus order.")
        lines.append("# columns: HTTP status, reach verdict in the response body (- when none),"
                     " reach.classify verdict, uid, title")
        for p in postings:
            direct = reach.classify(p["title"], p["description"])["verdict"]
            counts["by_verdict"][direct] = counts["by_verdict"].get(direct, 0) + 1
            path = "/api/postings/" + quote(p["uid"], safe="") + "/action"
            status, body = _post(port, path, b"kind=analysis", headers)
            if status == 409:
                counts["409"] += 1
                body_verdict = json.loads(body)["reach"]["verdict"]
                if body_verdict != direct:
                    counts["mismatch"] += 1
            elif status == 200:
                counts["200"] += 1
                body_verdict = "-"
            else:
                raise RuntimeError(f"unexpected HTTP {status} for {p['uid']}: {body[:200]!r}")
            lines.append(f"HTTP {status} body={body_verdict} direct={direct} "
                         f"uid={p['uid']} title={p['title']}")

        _wait_idle(cop, JOB_WAIT_S)  # the 200s queued real stub jobs; let them finish
        queued_final: dict[str, int] = {}
        for job in cop.jobs():
            queued_final[job["state"]] = queued_final.get(job["state"], 0) + 1
        lines.append("# queued analysis jobs after the loop, by final state: "
                     + ", ".join(f"{k}={queued_final[k]}" for k in sorted(queued_final)))

        # Force one out-of-reach posting through the 409 gate.
        forced = next(p for p in postings
                      if reach.classify(p["title"], p["description"])["verdict"] == "out_of_reach")
        path = "/api/postings/" + quote(forced["uid"], safe="") + "/action"
        status, body = _post(port, path, b"kind=analysis&force=1", headers)
        if status != 200:
            raise RuntimeError(f"force=1 returned HTTP {status}: {body[:200]!r}")
        job = _wait_uid(cop, forced["uid"], JOB_WAIT_S)
        stage_names = [s["name"] for s in job["stages"]]
        artifact = Path(job["artifact_path"]).name if job["artifact_path"] else "-"
        lines.append("# force=1 on one out-of-reach posting, followed to its end state")
        lines.append(f"forced HTTP {status} uid={forced['uid']} title={forced['title']}")
        lines.append(f"forced job state={job['state']} blocker={job['blocker'] or '-'} "
                     f"kind={job['kind']} stages={','.join(stage_names) or '-'} "
                     f"artifact={artifact} error={job['error'] or '-'}")
    finally:
        if srv is not None:
            srv.shutdown()
            srv.server_close()
        if thread is not None:
            thread.join(timeout=5)
        cop.stop()
        serve.PACKETS_ROOT, serve.DASHBOARD_PATH = saved

    lines.insert(0, f"# producing tool: tools/record_traces.py section A (reach gate, real server)")
    lines.insert(1, "# reproduce: python tools/build_demo_corpus.py && python tools/record_traces.py")
    lines.insert(2, f"# counts: postings={len(postings)} http_409={counts['409']} "
                    f"http_200={counts['200']} classify_mismatch={counts['mismatch']}")
    lines.insert(3, "# direct classify split: " + ", ".join(
        f"{k}={counts['by_verdict'][k]}" for k in sorted(counts["by_verdict"])))
    return lines, counts


def _stub_output(prompt: str, bundle: Path) -> tuple[int, str]:
    proc = subprocess.run([str(STUB), prompt, "--context", str(bundle), "--max-chars", "200000"],
                          capture_output=True, text=True, encoding="utf-8", timeout=60)
    return proc.returncode, proc.stdout


def _seeded(letter: str) -> list[tuple[str, str, str]]:
    """(rule, kind, text): one seeded-bad letter per rule, each a single edit of the stub letter."""
    def swap(old: str, new: str) -> str:
        assert old in letter, f"seed anchor missing: {old!r}"
        return letter.replace(old, new, 1)

    return [
        ("unverified_metric", "letter",
         swap("I write my own tests,", "I write my own tests, cutting review time by 63%,")),
        ("project_proper_noun", "letter",
         swap("the content pipeline", "the Aurora Ledger Reconciliation Pipeline")),
        ("non_ascii", "letter",
         swap("small, boring,", "small\u2014boring,")),
        ("FRENCH_BLOCKER", "letter",
         f"{FRENCH_TOKEN}: Fluent French is required for this role."),
        ("greeting", "letter", "Dear Hiring Manager,\n" + letter),
        ("code_tokens", "letter",
         swap("small, boring,", "small, {boring},")),
    ]


def section_b(work: Path) -> tuple[list[str], dict]:
    facts_text = FACTS.read_text(encoding="utf-8")
    bundle = work / "bundle"
    bundle.mkdir(parents=True, exist_ok=True)
    outputs: dict[str, str] = {}
    lines = ["# Section B: copilot.validate. Facts from tests/fixture_facts.yaml.",
             "# columns: kind, case, ok, problems (JSON list)"]
    rows: list[tuple[str, str, bool, list[str]]] = []

    for kind, prompt in STUB_PROMPTS:
        rc, out = _stub_output(prompt, bundle)
        if rc != 0 or not out.strip():
            raise RuntimeError(f"stub failed for {kind}: rc={rc}")
        outputs[kind] = out
        ok, problems = validate(kind, out, facts_text, "")
        rows.append((kind, f"stub-canned-{kind}", ok, problems))

    for rule, kind, text in _seeded(outputs["letter"]):
        ok, problems = validate(kind, text, facts_text, "")
        rows.append((kind, f"seed-{rule}", ok, problems))

    fired = {}
    for kind, case, ok, problems in rows:
        lines.append(f"kind={kind} case={case} ok={str(ok)} problems={json.dumps(problems)}")
        if case.startswith("seed-"):
            fired[case[5:]] = problems
    n_ok = sum(1 for r in rows if r[2])
    # Each seeded case must trip the rule it was built for; the count is reported, not forced.
    rule_hits = {c: any(p.split("=")[0] == c for p in pr) for c, pr in fired.items()}
    summary = {"cases": len(rows), "ok": n_ok, "failed": len(rows) - n_ok,
               "seeded_rule_hits": sum(1 for v in rule_hits.values() if v),
               "seeded_total": len(fired)}
    head = [
        "# producing tool: tools/record_traces.py section B (copilot.validate)",
        "# reproduce: python tools/record_traces.py",
        f"# counts: cases={summary['cases']} ok={summary['ok']} failed={summary['failed']} "
        f"seeded_rule_hits={summary['seeded_rule_hits']}/{summary['seeded_total']}",
    ]
    return head + lines, summary


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    postings = load_corpus()
    work = Path(tempfile.mkdtemp(prefix="jobsearch-trace-", dir="/tmp"))
    try:
        a_lines, a_counts = section_a(postings, work / "a")
        b_lines, b_summary = section_b(work / "b")
    finally:
        shutil.rmtree(work, ignore_errors=True)

    TRACE_REACH.write_text("\n".join(a_lines) + "\n", encoding="ascii")
    TRACE_VALIDATOR.write_text("\n".join(b_lines) + "\n", encoding="ascii")
    sums = [f"{_sha(p)}  {p.name}" for p in (CORPUS, TRACE_REACH, TRACE_VALIDATOR)]
    SUMS.write_text("\n".join(sums) + "\n", encoding="ascii")

    print(f"postings={len(postings)} http_409={a_counts['409']} http_200={a_counts['200']} "
          f"classify_mismatch={a_counts['mismatch']}")
    print(f"validator cases={b_summary['cases']} ok={b_summary['ok']} failed={b_summary['failed']}")
    print("\n".join(sums))
    return 0


if __name__ == "__main__":
    sys.exit(main())
