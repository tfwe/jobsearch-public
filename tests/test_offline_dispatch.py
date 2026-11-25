"""The pipeline runs offline: no model, no network, no API key, no live board.

Two things are exercised here that the other test modules leave to fixtures:

* tools/make_fixture.py, which seeds a SQLite store from the captured payloads in
  samples/ through the repository's own fetch code.
* tools/stub-model-cli.sh, which stands in for the model CLI, so a job can be queued,
  dispatched and finished — including the reach gate that returns 409 first.
"""
import http.client
import json
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import quote

import pytest

from jobsearch import serve
from jobsearch.copilot import Copilot
from jobsearch.store import Store

ROOT = Path(__file__).resolve().parents[1]
STUB = ROOT / "tools" / "stub-model-cli.sh"
MAKE_FIXTURE = ROOT / "tools" / "make_fixture.py"

# One posting per captured board payload, plus the second smartrecruiters record.
EXPECTED_FIXTURE_ROWS = 9

USER = "tester"
PASSWORD = "not-a-real-password"
FORM = {"Content-Type": "application/x-www-form-urlencoded"}

UID = "greenhouse:acme/9001"
SLUG = "greenhouse_acme_9001"
OOR_POSTING = {
    "uid": UID, "source": "greenhouse", "company": "Acme", "title": "Principal Engineer",
    "location": "Remote", "url": "https://example.invalid/job/9001",
    "description": "We need 7+ years of experience in distributed systems.",
}


def _auth_header():
    import base64
    raw = f"{USER}:{PASSWORD}".encode("utf-8")
    return {"Authorization": "Basic " + base64.b64encode(raw).decode("ascii")}


def _post(port, path, body=b"", headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.request("POST", path, body=body, headers=headers or {})
        resp = conn.getresponse()
        return resp.status, dict(resp.getheaders()), resp.read()
    finally:
        conn.close()


def _wait_for_job(cop, states, timeout=60.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for job in cop.jobs():
            if job["state"] in states:
                return job
        time.sleep(0.05)
    raise AssertionError(f"no job reached {states}: {cop.jobs()}")


@pytest.fixture
def tree(tmp_path, monkeypatch):
    """A throwaway jobsearch tree: the serve module's packet and dashboard roots."""
    root = tmp_path / "jobsearch"
    packets = root / "packets"
    (packets / SLUG).mkdir(parents=True)
    (packets / SLUG / "posting.md").write_text("# Posting\n", encoding="utf-8")
    (root / "dashboard.html").write_text("<!DOCTYPE html><p>dash</p>", encoding="utf-8")
    monkeypatch.setattr(serve, "DASHBOARD_PATH", root / "dashboard.html")
    monkeypatch.setattr(serve, "PACKETS_ROOT", packets)
    return root


def test_fixture_builder_writes_a_store_from_the_captured_payloads(tmp_path):
    out = tmp_path / "fixture" / "jobsearch-fixture.db"
    proc = subprocess.run([sys.executable, str(MAKE_FIXTURE), str(out)],
                          capture_output=True, text=True, timeout=180)
    assert proc.returncode == 0, proc.stderr
    store = Store(str(out))
    try:
        rows = store._conn.execute("SELECT uid, company, title FROM postings").fetchall()
    finally:
        store.close()
    assert len(rows) == EXPECTED_FIXTURE_ROWS
    assert all(r["uid"] and r["title"] for r in rows)


def test_a_worker_dispatches_a_job_to_the_stub_cli(tmp_path, tree):
    """No model, no key: the real worker, validator and artifact writer, on the stub."""
    facts = tmp_path / "facts.yaml"
    facts.write_text('version: 1\ncontact:\n  name: "Alex Morgan"\n', encoding="utf-8")
    db = tmp_path / "jobs.db"
    store = Store(str(db))
    store.upsert_postings([OOR_POSTING])
    store.close()

    state = tmp_path / "copilot"
    cop = Copilot(jobs_path=state / "jobs.json", packet_root=tree / "packets",
                  facts_path=facts, db_path=db, neon_cmd=str(STUB),
                  state_dir=state, bodies_dir=tmp_path / "bodies")
    cop.start()
    try:
        jid = cop.submit(UID, "analysis")["id"]
        job = _wait_for_job(cop, {"done", "failed"})
    finally:
        cop.stop()

    assert job["id"] == jid, "another job was picked up"
    assert job["state"] == "done", job.get("message") or job.get("error")
    assert job["blocker"] == ""
    assert [s["name"] for s in job["stages"]] == ["model"], job["stages"]
    artifact = Path(job["artifact_path"])
    assert artifact.name == "analysis.md" and artifact.is_file()
    assert "VERDICT" in artifact.read_text(encoding="utf-8")


def test_out_of_reach_action_409_then_forces_and_finishes_offline(tmp_path, tree):
    """The gated path, end to end, with nothing but the stub to answer."""
    facts = tmp_path / "facts.yaml"
    facts.write_text('version: 1\ncontact:\n  name: "Alex Morgan"\n', encoding="utf-8")
    db = tmp_path / "jobs.db"
    store = Store(str(db))
    store.upsert_postings([OOR_POSTING])
    store.close()

    state = tmp_path / "copilot"
    cop = Copilot(jobs_path=state / "jobs.json", packet_root=tree / "packets",
                  facts_path=facts, db_path=db, neon_cmd=str(STUB),
                  state_dir=state, bodies_dir=tmp_path / "bodies")
    cop.start()
    srv = serve.make_server("127.0.0.1", 0, USER, PASSWORD, db_path=str(db), copilot=cop)
    thread = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05},
                              daemon=True)
    thread.start()
    try:
        port = srv.server_address[1]
        path = "/api/postings/" + quote(UID, safe="") + "/action"
        headers = {**_auth_header(), **FORM}

        status, _, body = _post(port, path, b"kind=analysis", headers)
        assert status == 409, body
        assert json.loads(body)["reach"]["in_reach"] is False
        assert cop.jobs() == [], "the gate queued a job anyway"

        status, _, body = _post(port, path, b"kind=analysis&force=1", headers)
        assert status == 200, body
        assert json.loads(body)["job"]["kind"] == "analysis"

        job = _wait_for_job(cop, {"done", "failed"})
        assert job["state"] == "done", job.get("message") or job.get("error")
        assert Path(job["artifact_path"]).is_file()
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=5)
        cop.stop()


def test_stub_cli_refuses_an_unknown_prompt(tmp_path):
    """The stub is honest about not being a model: it exits non-zero on anything else."""
    proc = subprocess.run([str(STUB), "write me a poem", "--context", str(tmp_path),
                           "--max-chars", "200000"],
                          capture_output=True, text=True, timeout=30)
    assert proc.returncode == 2
    assert "unrecognised" in proc.stderr
