"""Tests for jobsearch.serve: auth, routes, traversal, write methods, and the no-credentials refusal.

The live-server tests bind 127.0.0.1 on port 0 with dummy credentials and a tmp_path tree.
"""
import http.client
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
import types
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, urlencode

import pytest

from jobsearch import copilot as copilot_mod
from jobsearch import serve
from jobsearch.copilot import Copilot
from jobsearch.store import Store

USER = "tester"
PASSWORD = "not-a-real-password"
REPO = Path(__file__).resolve().parents[1]
PACKET = "ashby_render_test"
FILES = {
    "resume.pdf": b"%PDF-1.4 fake resume",
    "cover_letter.pdf": b"%PDF-1.4 fake cover",
    "cover_letter.tex": b"\\documentclass{article}",
    "cover_letter_body.txt": b"Dear team,",
    "posting.md": b"# Posting",
}


@pytest.fixture
def tree(tmp_path, monkeypatch):
    """A fake ~/jobsearch: dashboard.html, packets/<PACKET>/<five files>, and a secret beside packets/."""
    root = tmp_path / "jobsearch"
    packets = root / "packets"
    (packets / PACKET).mkdir(parents=True)
    for name, data in FILES.items():
        (packets / PACKET / name).write_bytes(data)
    (root / "dashboard.html").write_text("<!DOCTYPE html><p>dash</p>", encoding="utf-8")
    (root / "secret.txt").write_text("SECRET", encoding="utf-8")
    monkeypatch.setattr(serve, "DASHBOARD_PATH", root / "dashboard.html")
    monkeypatch.setattr(serve, "PACKETS_ROOT", packets)
    return root


@pytest.fixture
def server(tree, monkeypatch):
    monkeypatch.setenv("JOBSEARCH_SERVE_USER", USER)
    monkeypatch.setenv("JOBSEARCH_SERVE_PASSWORD", PASSWORD)
    srv = serve.make_server("127.0.0.1", 0, USER, PASSWORD)
    thread = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    try:
        yield srv.server_address[1]
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=5)


def _auth_header(user=USER, password=PASSWORD):
    import base64
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def _req(port, method, path, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request(method, path, headers=headers or {})
        resp = conn.getresponse()
        return resp.status, dict(resp.getheaders()), resp.read()
    finally:
        conn.close()


# ---------- startup refusal ----------

@pytest.mark.parametrize("user,password", [
    (None, PASSWORD), (USER, None), ("", PASSWORD), (USER, ""), (None, None), ("  ", PASSWORD),
])
def test_refuses_to_start_without_credentials(monkeypatch, capsys, user, password):
    for name, val in (("JOBSEARCH_SERVE_USER", user), ("JOBSEARCH_SERVE_PASSWORD", password)):
        if val is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, val)
    assert serve.main(["--port", "0"]) == 78
    err = capsys.readouterr().err
    assert "JOBSEARCH_SERVE_USER" in err and "JOBSEARCH_SERVE_PASSWORD" in err


def test_subprocess_without_credentials_exits_78():
    env = {k: v for k, v in os.environ.items()
           if k not in ("JOBSEARCH_SERVE_USER", "JOBSEARCH_SERVE_PASSWORD")}
    proc = subprocess.run([sys.executable, "-m", "jobsearch.serve", "--port", "0"],
                          cwd=REPO, env=env, capture_output=True, text=True, timeout=30)
    assert proc.returncode == 78
    assert "refusing to start" in proc.stderr


def test_default_host_and_port_are_the_loopback_address():
    args = serve.build_parser().parse_args([])
    from jobsearch.serve import DEFAULT_HOST
    assert args.host == DEFAULT_HOST
    assert args.port == 8770


def test_make_server_rejects_empty_credentials():
    with pytest.raises(ValueError):
        serve.make_server("127.0.0.1", 0, "", PASSWORD)


# ---------- auth ----------

def test_401_without_credentials(server):
    status, headers, _ = _req(server, "GET", "/")
    assert status == 401
    assert headers["WWW-Authenticate"].startswith("Basic ")


@pytest.mark.parametrize("headers", [
    _auth_header(password="wrong"),
    _auth_header(user="someone"),
    {"Authorization": "Bearer abc"},
    {"Authorization": "Basic !!!notbase64"},
])
def test_401_with_bad_credentials(server, headers):
    assert _req(server, "GET", "/healthz", headers)[0] == 401


def test_401_applies_to_unknown_paths_too(server):
    assert _req(server, "GET", "/nope")[0] == 401


# ---------- routes ----------

def test_dashboard_served_with_no_store(server, tree):
    status, headers, body = _req(server, "GET", "/dashboard", _auth_header())
    assert status == 200
    assert headers["Content-Type"] == "text/html; charset=utf-8"
    assert headers["Cache-Control"] == "no-store"
    assert body == (tree / "dashboard.html").read_bytes()


def test_healthz_returns_json_body(server, tree):
    status, headers, body = _req(server, "GET", "/healthz", _auth_header())
    assert status == 200
    assert headers["Content-Type"] == "application/json"
    data = json.loads(body)
    assert data["ok"] is True
    assert data["dashboard_mtime"] == int((tree / "dashboard.html").stat().st_mtime)
    assert data["age_s"] >= 0


def test_healthz_reports_missing_dashboard(server, tree):
    (tree / "dashboard.html").unlink()
    status, _, body = _req(server, "GET", "/healthz", _auth_header())
    assert status == 503
    assert json.loads(body)["ok"] is False
    assert _req(server, "GET", "/dashboard", _auth_header())[0] == 404


def test_packets_index_lists_files_as_links(server):
    status, headers, body = _req(server, "GET", "/packets/", _auth_header())
    assert status == 200
    assert headers["Content-Type"] == "text/html; charset=utf-8"
    text = body.decode("utf-8")
    assert PACKET in text
    for name in FILES:
        assert f'href="/packets/{PACKET}/{name}"' in text


def test_human_bytes_reads_like_a_size():
    assert serve.human_bytes(0) == "0 B"
    assert serve.human_bytes(812) == "812 B"
    assert serve.human_bytes(4200) == "4.2 kB"
    assert serve.human_bytes(2_100_000) == "2.1 MB"


def test_a_packet_file_downloads_as_an_attachment_when_asked(server):
    status, headers, body = _req(server, "GET",
                                 f"/packets/{PACKET}/resume.pdf?download=1", _auth_header())
    assert status == 200 and body.startswith(b"%PDF")
    assert headers["Content-Disposition"] == 'attachment; filename="resume.pdf"'
    status, headers, _ = _req(server, "GET", f"/packets/{PACKET}/resume.pdf", _auth_header())
    assert status == 200 and "Content-Disposition" not in headers


@pytest.mark.parametrize("name,ctype", [
    ("resume.pdf", "application/pdf"),
    ("cover_letter.pdf", "application/pdf"),
    ("cover_letter.tex", "text/x-tex"),
    ("cover_letter_body.txt", "text/plain; charset=utf-8"),
    ("posting.md", "text/plain; charset=utf-8"),
])
def test_packet_file_content_type_and_bytes(server, name, ctype):
    status, headers, body = _req(server, "GET", f"/packets/{PACKET}/{name}", _auth_header())
    assert status == 200
    assert headers["Content-Type"] == ctype
    assert headers["Cache-Control"].startswith("private")
    assert body == FILES[name]


def test_unknown_path_is_404(server):
    for path in ("/nope", "/healthz/", "/packets", "/packets/" + PACKET + "/", "/index.html"):
        assert _req(server, "GET", path, _auth_header())[0] == 404, path


# ---------- traversal and allow-list ----------

@pytest.mark.parametrize("path", [
    "/packets/../secret.txt",
    f"/packets/{PACKET}/../../secret.txt",
    "/packets/%2e%2e/secret.txt",
    "/packets/%2E%2E/secret.txt",
    f"/packets/{PACKET}/%2e%2e%2f%2e%2e%2fsecret.txt",
    f"/packets/{PACKET}/..%2f..%2fsecret.txt",
    "/packets//etc/passwd",
    "//packets/../secret.txt",
    f"/packets/{PACKET}/%00resume.pdf",
    "/packets/no_such_dir/resume.pdf",
    f"/packets/{PACKET}/secret.txt",
    f"/packets/{PACKET}/.env",
    f"/packets/{PACKET}/resume.pdf/extra",
    "/packets/.",
    "/packets/..",
    f"/packets/{PACKET}/%2fresume.pdf",
])
def test_traversal_and_disallowed_paths_are_404(server, path):
    status, _, body = _req(server, "GET", path, _auth_header())
    assert status == 404, path
    assert b"SECRET" not in body


def test_symlink_escaping_packets_root_is_404(server, tree):
    link = tree / "packets" / PACKET / "resume.pdf"
    link.unlink()
    link.symlink_to(tree / "secret.txt")
    status, _, body = _req(server, "GET", f"/packets/{PACKET}/resume.pdf", _auth_header())
    assert status == 404
    assert b"SECRET" not in body


def test_packets_dir_symlink_out_of_root_is_404(server, tree, tmp_path):
    outside = tmp_path / "outside_packet"
    outside.mkdir()
    (outside / "posting.md").write_text("OUTSIDE", encoding="utf-8")
    (tree / "packets" / "escape").symlink_to(outside)
    assert _req(server, "GET", "/packets/escape/posting.md", _auth_header())[0] == 404


# ---------- write methods ----------

@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH"])
def test_write_methods_are_405(server, tree, method):
    headers = {**_auth_header(), "Content-Length": "4"}
    status, resp_headers, _ = _req(server, method, "/", headers)
    assert status == 405
    assert resp_headers["Allow"] == "GET"
    assert (tree / "dashboard.html").read_text(encoding="utf-8") == "<!DOCTYPE html><p>dash</p>"


def test_write_method_on_packet_file_is_405(server):
    assert _req(server, "DELETE", f"/packets/{PACKET}/resume.pdf", _auth_header())[0] == 405


# ---------- postings API ----------

UID = "greenhouse:acme/4711"
UID_DIR = "greenhouse_acme_4711"
OTHER_UID = "greenhouse:acme/0042"
OTHER_DIR = "greenhouse_acme_0042"


@pytest.fixture
def jobs_db(tmp_path):
    path = tmp_path / "jobs.db"
    store = Store(str(path))
    store.upsert_postings([
        {"uid": UID, "source": "greenhouse", "company": "Acme", "title": "Engineer",
         "location": "Remote", "url": "https://example.invalid/job/4711"},
        {"uid": OTHER_UID, "source": "greenhouse", "company": "Acme", "title": "Analyst",
         "location": "Boston", "url": "https://example.invalid/job/0042"},
    ])
    store.set_score(UID, 0.81, "A", ["match"])
    store.close()
    return path


@pytest.fixture
def api(tree, jobs_db, monkeypatch):
    """A live server on port 0 backed by jobs_db and the fake packets tree."""
    monkeypatch.setenv("JOBSEARCH_SERVE_USER", USER)
    monkeypatch.setenv("JOBSEARCH_SERVE_PASSWORD", PASSWORD)
    srv = serve.make_server("127.0.0.1", 0, USER, PASSWORD, db_path=str(jobs_db))
    thread = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    try:
        yield srv.server_address[1]
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=5)


def _post(port, path, body=b"", headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request("POST", path, body=body, headers=headers or {})
        resp = conn.getresponse()
        return resp.status, dict(resp.getheaders()), resp.read()
    finally:
        conn.close()


def _events(db, uid):
    store = Store(str(db))
    try:
        return store.events(uid)
    finally:
        store.close()


def _status_of(db, uid):
    store = Store(str(db))
    try:
        return store.get(uid)["status"]
    finally:
        store.close()


FORM = {"Content-Type": "application/x-www-form-urlencoded"}
JSON_HDR = {"Content-Type": "application/json"}
POSTING_LOCATION = "/posting/greenhouse%3Aacme%2F4711"
STATUS_PATH = "/api/postings/" + UID.replace("/", "%2F") + "/status"


def _wait_for_renders(timeout: float = 10.0) -> None:
    """Block until no background dashboard render is in flight (test-side only)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with serve._render_guard:
            if not serve._render_running:
                return
        time.sleep(0.01)
    raise AssertionError("dashboard render did not settle")


@pytest.fixture(autouse=True)
def _renders_settle_after_each_test():
    yield
    _wait_for_renders()


def test_status_post_writes_exactly_one_status_event(api, jobs_db):
    before = len(_events(jobs_db, UID))
    status, _, body = _post(api, f"/api/postings/{UID.replace('/', '%2F')}/status",
                            json.dumps({"status": "applied"}).encode(),
                            {**_auth_header(), **JSON_HDR})
    assert status == 200
    data = json.loads(body)
    assert data == {"ok": True, "uid": UID, "status": "applied", "event_count": before + 1,
                    "allowed_next": ["interview", "rejected", "closed"]}
    new = _events(jobs_db, UID)[before:]
    assert [e["kind"] for e in new] == ["status"]
    assert new[0]["detail"] == "new->applied"
    assert _status_of(jobs_db, UID) == "applied"


def test_invalid_status_is_400_and_writes_nothing(api, jobs_db):
    before = _events(jobs_db, UID)
    status, _, body = _post(api, "/api/postings/" + UID.replace("/", "%2F") + "/status",
                            b"status=bogus", {**_auth_header(), **FORM})
    assert status == 400
    assert json.loads(body)["error"] == "unknown status"
    assert _events(jobs_db, UID) == before
    assert _status_of(jobs_db, UID) == "new"


def test_invalid_status_in_json_is_400(api, jobs_db):
    before = _events(jobs_db, UID)
    status, _, _ = _post(api, "/api/postings/" + UID.replace("/", "%2F") + "/status",
                         json.dumps({"status": "DROP TABLE"}).encode(),
                         {**_auth_header(), **JSON_HDR})
    assert status == 400
    assert _events(jobs_db, UID) == before


def test_unknown_uid_is_404_json_and_writes_nothing(api, jobs_db):
    status, headers, body = _post(api, "/api/postings/nope:none/status", b"status=applied",
                                  {**_auth_header(), **FORM})
    assert status == 404
    assert headers["Content-Type"] == "application/json"
    assert json.loads(body)["error"] == "unknown uid"
    assert _events(jobs_db, "nope:none") == []


def test_status_post_without_credentials_is_401(api, jobs_db):
    before = _events(jobs_db, UID)
    status, headers, _ = _post(api, "/api/postings/" + UID.replace("/", "%2F") + "/status",
                               b"status=applied", FORM)
    assert status == 401
    assert headers["WWW-Authenticate"].startswith("Basic ")
    assert _events(jobs_db, UID) == before


def test_status_post_with_foreign_origin_is_403(api, jobs_db):
    before = _events(jobs_db, UID)
    headers = {**_auth_header(), **FORM, "Origin": "https://evil.example"}
    status, _, body = _post(api, "/api/postings/" + UID.replace("/", "%2F") + "/status",
                            b"status=applied", headers)
    assert status == 403
    assert "error" in json.loads(body)
    assert _events(jobs_db, UID) == before


def test_status_post_with_matching_origin_is_allowed(api, jobs_db):
    headers = {**_auth_header(), **FORM, "Origin": f"http://127.0.0.1:{api}"}
    status, _, _ = _post(api, "/api/postings/" + UID.replace("/", "%2F") + "/status",
                         b"status=shortlisted", headers)
    assert status == 200
    assert _status_of(jobs_db, UID) == "shortlisted"


def test_status_post_with_null_origin_is_403(api):
    headers = {**_auth_header(), **FORM, "Origin": "null"}
    status, _, _ = _post(api, "/api/postings/" + UID.replace("/", "%2F") + "/status",
                         b"status=applied", headers)
    assert status == 403


def test_form_post_with_html_accept_redirects_303(api, jobs_db):
    headers = {**_auth_header(), **FORM, "Accept": "text/html,application/xhtml+xml"}
    status, resp_headers, _ = _post(api, "/api/postings/" + UID.replace("/", "%2F") + "/status",
                                    b"status=interview&note=phone+screen", headers)
    assert status == 303
    assert resp_headers["Location"] == POSTING_LOCATION
    assert "@" not in resp_headers["Location"]
    assert USER not in resp_headers["Location"] and PASSWORD not in resp_headers["Location"]
    assert _status_of(jobs_db, UID) == "interview"
    kinds = [e["kind"] for e in _events(jobs_db, UID)]
    assert "note" in kinds and kinds.count("status") == 1


def test_form_post_absolute_target_with_credentials_still_redirects_to_bare_slash(api, jobs_db):
    # A proxy-style absolute request target carries userinfo; the redirect must not echo it.
    target = (f"http://{USER}:{PASSWORD}@127.0.0.1:{api}/api/postings/"
              + UID.replace("/", "%2F") + "/status")
    headers = {**_auth_header(), **FORM, "Accept": "text/html"}
    status, resp_headers, _ = _post(api, target, b"status=prepped", headers)
    assert status == 303
    assert resp_headers["Location"] == POSTING_LOCATION
    assert "@" not in resp_headers["Location"] and PASSWORD not in resp_headers["Location"]
    assert _status_of(jobs_db, UID) == "prepped"


def test_form_post_without_html_accept_returns_json_200(api):
    status, _, body = _post(api, "/api/postings/" + UID.replace("/", "%2F") + "/status",
                            b"status=skipped", {**_auth_header(), **FORM})
    assert status == 200
    assert json.loads(body)["status"] == "skipped"


def test_status_post_other_path_is_405(api):
    for path in ("/", "/healthz", "/api/postings/" + UID.replace("/", "%2F"),
                 "/api/postings/x/status/extra"):
        status, headers, _ = _post(api, path, b"status=applied", {**_auth_header(), **FORM})
        assert status == 405, path
        assert headers["Allow"] == "GET"


def test_status_post_rejects_non_object_json_and_bad_content_type(api):
    auth = _auth_header()
    assert _post(api, f"/api/postings/{UID.replace('/', '%2F')}/status",
                 b'["applied"]', {**auth, **JSON_HDR})[0] == 400
    assert _post(api, f"/api/postings/{UID.replace('/', '%2F')}/status",
                 b"status=applied", {**auth, "Content-Type": "text/plain"})[0] == 415


def test_status_post_body_over_limit_is_413(api, jobs_db):
    before = _events(jobs_db, UID)
    big = b"status=applied&note=" + b"x" * (serve.MAX_BODY + 1)
    status, _, _ = _post(api, "/api/postings/" + UID.replace("/", "%2F") + "/status", big,
                         {**_auth_header(), **FORM})
    assert status == 413
    assert _events(jobs_db, UID) == before


def test_get_posting_reports_has_letter_with_and_without_cover_letter(api, tree):
    (tree / "packets" / UID_DIR).mkdir()
    (tree / "packets" / UID_DIR / "resume.pdf").write_bytes(b"%PDF")
    (tree / "packets" / UID_DIR / "cover_letter.pdf").write_bytes(b"%PDF")
    (tree / "packets" / OTHER_DIR).mkdir()
    (tree / "packets" / OTHER_DIR / "resume.pdf").write_bytes(b"%PDF")

    status, headers, body = _req(api, "GET", "/api/postings/" + UID.replace("/", "%2F"),
                                 _auth_header())
    assert status == 200
    assert headers["Content-Type"] == "application/json"
    data = json.loads(body)
    assert data["uid"] == UID
    assert data["company"] == "Acme" and data["title"] == "Engineer"
    assert data["status"] == "new" and data["score"] == 0.81 and data["variant"] == "A"
    assert data["packet"]["dir"] == UID_DIR
    assert data["packet"]["has_resume"] is True
    assert data["packet"]["has_letter"] is True
    assert [e["kind"] for e in data["events"]] == []

    status, _, body = _req(api, "GET", "/api/postings/" + OTHER_UID.replace("/", "%2F"),
                           _auth_header())
    assert status == 200
    other = json.loads(body)["packet"]
    assert other["has_resume"] is True
    assert other["has_letter"] is False


def test_get_posting_without_packet_dir(api):
    status, _, body = _req(api, "GET", "/api/postings/" + OTHER_UID.replace("/", "%2F"),
                           _auth_header())
    packet = json.loads(body)["packet"]
    assert status == 200
    assert packet["has_resume"] is False and packet["has_letter"] is False


def test_get_posting_events_follow_a_status_write(api, jobs_db):
    _post(api, "/api/postings/" + UID.replace("/", "%2F") + "/status", b"status=prepped",
          {**_auth_header(), **FORM})
    _, _, body = _req(api, "GET", "/api/postings/" + UID.replace("/", "%2F"), _auth_header())
    data = json.loads(body)
    assert data["status"] == "prepped"
    assert [e["kind"] for e in data["events"]] == ["status"]


def test_get_unknown_posting_is_404_json(api):
    status, headers, body = _req(api, "GET", "/api/postings/nope:none", _auth_header())
    assert status == 404
    assert headers["Content-Type"] == "application/json"
    assert json.loads(body)["error"] == "unknown uid"


def test_get_posting_requires_auth(api):
    assert _req(api, "GET", "/api/postings/" + UID.replace("/", "%2F"))[0] == 401


def test_get_api_subpaths_are_404(api):
    assert _req(api, "GET", f"/api/postings/{UID.replace('/', '%2F')}/status",
                _auth_header())[0] == 404
    assert _req(api, "GET", "/api/postings/", _auth_header())[0] == 404


def test_status_post_rerenders_dashboard(api, tree, jobs_db):
    dashboard = tree / "dashboard.html"
    before = dashboard.read_text(encoding="utf-8")
    status, _, _ = _post(api, "/api/postings/" + UID.replace("/", "%2F") + "/status",
                         b"status=applied", {**_auth_header(), **FORM,
                                             "Accept": "text/html"})
    assert status == 303
    _wait_for_renders()
    after = dashboard.read_text(encoding="utf-8")
    assert after != before
    assert "dash</p>" not in after
    assert UID in after


def test_dashboard_render_failure_keeps_write_and_still_303(api, tree, jobs_db, monkeypatch, caplog):
    def boom(*args, **kwargs):
        raise RuntimeError("render exploded")
    monkeypatch.setattr(serve, "render_dashboard", boom)
    headers = {**_auth_header(), **FORM, "Accept": "text/html"}
    status, resp_headers, _ = _post(api, "/api/postings/" + UID.replace("/", "%2F") + "/status",
                                    b"status=offer", headers)
    assert status == 303
    assert resp_headers["Location"] == POSTING_LOCATION
    assert _status_of(jobs_db, UID) == "offer"
    assert [e["kind"] for e in _events(jobs_db, UID)] == ["status"]
    _wait_for_renders()
    assert any(r.name == "jobsearch.serve" and r.levelno == logging.ERROR
               and "re-render failed" in r.getMessage() for r in caplog.records)


def _get_page(port, uid, step=""):
    path = "/posting/" + uid.replace("/", "%2F")
    if step:
        path += "?step=" + step
    return _req(port, "GET", path, _auth_header())


def test_posting_page_shows_company_timeline_newest_first_and_packet_links(api, tree, jobs_db):
    packet = tree / "packets" / UID_DIR
    packet.mkdir()
    for name in ("resume.pdf", "cover_letter.pdf", "posting.md"):
        (packet / name).write_bytes(b"x")
    auth = {**_auth_header(), **FORM}
    _post(api, "/api/postings/" + UID.replace("/", "%2F") + "/status",
          b"status=shortlisted&note=good+fit", auth)
    _post(api, "/api/postings/" + UID.replace("/", "%2F") + "/status", b"status=prepped", auth)

    status, headers, body = _get_page(api, UID, "packet")
    text = body.decode("utf-8")
    assert status == 200
    assert headers["Content-Type"].startswith("text/html")
    assert '<div class="tb-title">Engineer</div>' in text
    assert "Acme" in text and "Remote" in text and "score 0.81" in text
    assert "Open real posting to submit" in _get_page(api, UID, "submit")[2].decode("utf-8")
    assert 'href="https://example.invalid/job/4711"' in text
    assert f'href="/packets/{UID_DIR}/resume.pdf"' in text
    assert f'href="/packets/{UID_DIR}/cover_letter.pdf"' in text
    assert f'href="/packets/{UID_DIR}/posting.md"' in text
    assert "no letter yet" not in text.split("<script>", 1)[0]  # the script names it too
    # Timeline newest first: the prepped status event precedes the earlier shortlisted ones.
    i_prepped = text.index("shortlisted-&gt;prepped")
    i_note = text.index("good fit")
    i_short = text.index("new-&gt;shortlisted")
    assert i_prepped < i_note < i_short
    # Allowed moves only, from the same form the dashboard uses.
    from urllib.parse import quote as _q
    assert f'action="/api/postings/{_q(UID, safe="")}/status"' in text
    # Status is now 'prepped': applied and skipped are the "Next move" group, and every
    # other status is offered below it under "Any status" (the workspace can move anywhere).
    head, _, rest = text.partition('<optgroup label="Next move">')
    next_group = rest.split("</optgroup>")[0]
    assert "<option value=\"applied\">" in next_group and "<option value=\"skipped\">" in next_group
    assert "shortlisted" not in next_group and "interview" not in next_group
    assert '<optgroup label="Any status">' in rest


def test_posting_page_without_cover_letter_says_no_letter_yet(api, tree):
    (tree / "packets" / OTHER_DIR).mkdir()
    (tree / "packets" / OTHER_DIR / "resume.pdf").write_bytes(b"%PDF")
    status, _, body = _get_page(api, OTHER_UID, "packet")
    text = body.decode("utf-8")
    assert status == 200
    assert "no letter yet" in text
    assert f'href="/packets/{OTHER_DIR}/cover_letter.pdf"' not in text  # no letter link; the script text may name the file
    assert "no further moves" not in text  # 'new' still has moves


def _variant_of(db, uid):
    store = Store(str(db))
    try:
        return store.get(uid)["variant"]
    finally:
        store.close()


def test_variant_post_sets_the_row_and_logs_an_event(api, jobs_db):
    before = _variant_of(jobs_db, UID)
    target = "sec-content" if before != "sec-content" else "general"
    status, headers, _ = _post(
        api, "/api/postings/" + UID.replace("/", "%2F") + "/variant",
        f"variant={target}&return=/posting/x%3Fstep%3Dpacket".encode(),
        {**_auth_header(), **FORM, "Accept": "text/html"})
    assert status == 303
    assert _variant_of(jobs_db, UID) == target
    assert _events(jobs_db, UID)[-1]["kind"] == "variant"
    assert target in _events(jobs_db, UID)[-1]["detail"]
    _wait_for_renders()


def test_variant_post_refuses_a_name_that_is_not_one_of_the_six(api, jobs_db):
    before = _variant_of(jobs_db, UID)
    status, _, body = _post(api, "/api/postings/" + UID.replace("/", "%2F") + "/variant",
                            b"variant=resume-final-final", {**_auth_header(), **FORM})
    assert status == 400
    data = json.loads(body)
    assert "allowed" in data and len(data["allowed"]) == 6
    assert _variant_of(jobs_db, UID) == before


def test_variant_post_answers_json_and_lands_back_on_the_packet_step(api, jobs_db):
    status, _, body = _post(
        api, "/api/postings/" + UID.replace("/", "%2F") + "/variant",
        json.dumps({"variant": "ai-platform"}).encode(),
        {**_auth_header(), **JSON_HDR})
    assert status == 200 and json.loads(body)["variant"] == "ai-platform"
    assert _variant_of(jobs_db, UID) == "ai-platform"

    status, headers, _ = _post(
        api, "/api/postings/" + UID.replace("/", "%2F") + "/variant",
        b"variant=general&return=/posting/greenhouse%3Aacme%2F4711%3Fstep%3Dpacket",
        {**_auth_header(), **FORM, "Accept": "text/html"})
    assert status == 303
    assert headers["Location"].endswith("4711?step=packet")


def test_variant_post_refuses_a_cross_origin_request(api, jobs_db):
    before = _variant_of(jobs_db, UID)
    status, _, _ = _post(api, "/api/postings/" + UID.replace("/", "%2F") + "/variant",
                         b"variant=general",
                         {**_auth_header(), **FORM, "Origin": "https://evil.invalid"})
    assert status == 403
    assert _variant_of(jobs_db, UID) == before


def test_checks_endpoint_runs_the_same_validator_without_writing(body_api):
    body = "word " * 100
    status, _, raw = _post(
        body_api.port, "/api/postings/" + UID.replace("/", "%2F") + "/checks",
        json.dumps({"body": body}).encode(), {**_auth_header(), **JSON_HDR})
    assert status == 200
    data = json.loads(raw)
    assert data["words"] == 100
    labels = [c["label"] for c in data["checks"]]
    assert labels == ["240-320 words", "ASCII only", "No project names",
                      "No tech-list parentheses", "Every number traceable",
                      "No French requirement", "Names the company",
                      "No resume sentences reused", "No cliches"]
    assert next(c for c in data["checks"] if c["label"] == "240-320 words")["ok"] is False
    # read-only: no body file written
    assert not (body_api.cop.bodies_dir / (UID_DIR + ".txt")).exists()


def test_checks_endpoint_refuses_a_non_ascii_body_the_way_the_save_does(body_api):
    status, _, raw = _post(
        body_api.port, "/api/postings/" + UID.replace("/", "%2F") + "/checks",
        json.dumps({"body": "caf\u00e9 " * 60}).encode(), {**_auth_header(), **JSON_HDR})
    assert status == 200
    ascii_check = next(c for c in json.loads(raw)["checks"] if c["label"] == "ASCII only")
    assert ascii_check["ok"] is False


def test_checks_endpoint_404s_an_unknown_uid_and_415s_a_bad_type(api):
    status, _, _ = _post(api, "/api/postings/nope%3Anone%2F0/checks",
                         json.dumps({"body": "x"}).encode(), {**_auth_header(), **JSON_HDR})
    assert status == 404
    status, _, _ = _post(api, "/api/postings/" + UID.replace("/", "%2F") + "/checks",
                         b"body=x", {**_auth_header(), "Content-Type": "text/plain"})
    assert status == 415


def test_posting_page_unknown_uid_is_404(api):
    status, _, _ = _get_page(api, "nope:none/0000")
    assert status == 404


def test_posting_page_requires_auth(api):
    assert _req(api, "GET", "/posting/" + UID.replace("/", "%2F"))[0] == 401


def test_posting_page_escapes_note_text_and_url(api, jobs_db):
    hostile = "https://example.invalid/x?q=\"><script>alert(1)</script>"
    store = Store(str(jobs_db))
    store.upsert_postings([{"uid": "hostile:1", "source": "x", "company": "<b>Co</b>",
                            "title": "T", "location": "L", "url": hostile}])
    store.close()
    note = "<script>alert('xss')</script>"
    body = "status=shortlisted&note=" + note.replace("<", "%3C").replace(">", "%3E")
    _post(api, "/api/postings/" + UID.replace("/", "%2F") + "/status", body.encode(),
          {**_auth_header(), **FORM})
    _, _, page = _get_page(api, UID)
    text = page.decode("utf-8")
    # The page carries its own inline <script>; only the injected payload must be absent.
    assert "<script>alert" not in text
    assert "&lt;script&gt;alert(&#x27;xss&#x27;)&lt;/script&gt;" in text
    _, _, hostile_page = _get_page(api, "hostile:1")
    hostile_text = hostile_page.decode("utf-8")
    assert "<script>alert" not in hostile_text and "<b>Co</b>" not in hostile_text
    assert "&lt;b&gt;Co&lt;/b&gt;" in hostile_text
    assert "&quot;&gt;&lt;script&gt;" in hostile_text


def test_form_post_answers_before_a_slow_render_finishes(api, jobs_db, monkeypatch):
    def slow(*args, **kwargs):
        time.sleep(2)
    monkeypatch.setattr(serve, "render_dashboard", slow)
    headers = {**_auth_header(), **FORM, "Accept": "text/html"}
    started = time.monotonic()
    status, resp_headers, _ = _post(api, STATUS_PATH, b"status=applied", headers)
    elapsed = time.monotonic() - started
    assert status == 303
    assert resp_headers["Location"] == POSTING_LOCATION
    assert elapsed < 1.0, elapsed
    assert serve._render_running  # the render is still in flight after the reply
    assert _status_of(jobs_db, UID) == "applied"


def test_burst_of_status_posts_queues_at_most_one_follow_up_render(api, jobs_db, monkeypatch):
    calls = []
    started = threading.Event()
    gate = threading.Event()

    def gated(*args, **kwargs):
        calls.append(args)
        started.set()
        gate.wait(5)

    monkeypatch.setattr(serve, "render_dashboard", gated)
    headers = {**_auth_header(), **FORM, "Accept": "text/html"}
    # new -> shortlisted -> prepped -> applied are all legal moves in sequence.
    assert _post(api, STATUS_PATH, b"status=shortlisted", headers)[0] == 303
    assert started.wait(2)
    assert _post(api, STATUS_PATH, b"status=prepped", headers)[0] == 303
    assert _post(api, STATUS_PATH, b"status=applied", headers)[0] == 303
    assert len(calls) == 1  # clicks 2 and 3 did not start renders of their own
    gate.set()
    _wait_for_renders()
    assert len(calls) == 2  # exactly one follow-up pass
    assert serve._render_pending is False
    assert _status_of(jobs_db, UID) == "applied"


def test_render_thread_start_failure_is_logged_and_flags_reset(api, jobs_db, monkeypatch, caplog):
    def no_threads(*args, **kwargs):
        raise RuntimeError("can't start new thread")
    # Patch only serve's reference: the test server itself needs threading.Thread.
    fake = types.SimpleNamespace(Lock=threading.Lock, Thread=no_threads)
    monkeypatch.setattr(serve, "threading", fake)
    headers = {**_auth_header(), **FORM, "Accept": "text/html"}
    status, _, _ = _post(api, STATUS_PATH, b"status=applied", headers)
    assert status == 303
    assert serve._render_running is False and serve._render_pending is False
    assert "could not start dashboard render thread" in caplog.text


def test_text_packet_files_declare_utf8_charset(server):
    for name in ("cover_letter_body.txt", "posting.md"):
        status, headers, _ = _req(server, "GET", f"/packets/{PACKET}/{name}", _auth_header())
        assert status == 200
        assert headers["Content-Type"] == "text/plain; charset=utf-8"


# ---------- AI actions: POST /api/postings/UID/action and the posting page ----------

ACTION_PATH = "/api/postings/" + UID.replace("/", "%2F") + "/action"
REFRESH = '<meta http-equiv="refresh" content="15">'


@pytest.fixture
def ai_api(tree, jobs_db, tmp_path, monkeypatch):
    """Live server with a Copilot that is constructed but never started (no worker, no neon)."""
    monkeypatch.setenv("JOBSEARCH_SERVE_USER", USER)
    monkeypatch.setenv("JOBSEARCH_SERVE_PASSWORD", PASSWORD)
    facts = tmp_path / "facts.yaml"
    facts.write_text("version: 1\n", encoding="utf-8")
    cop = Copilot(jobs_path=tmp_path / "ai" / "jobs.json", packet_root=tree / "packets",
                  facts_path=facts, db_path=str(jobs_db),
                  neon_cmd=str(tmp_path / "no-such-neon"), state_dir=tmp_path / "ai" / "state",
                  bodies_dir=tmp_path / "bodies")
    srv = serve.make_server("127.0.0.1", 0, USER, PASSWORD, db_path=str(jobs_db), copilot=cop)
    thread = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    try:
        yield types.SimpleNamespace(port=srv.server_address[1], cop=cop)
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=5)


def _set_job(cop, jid, **fields):
    with cop._lock:
        cop._find(jid).update(**fields)


def _page(port, uid=UID, step=""):
    status, _, body = _get_page(port, uid, step)
    assert status == 200
    return body.decode("utf-8")


def test_action_form_post_queues_job_and_303s(ai_api):
    headers = {**_auth_header(), **FORM, "Accept": "text/html,application/xhtml+xml"}
    status, resp_headers, _ = _post(ai_api.port, ACTION_PATH, b"kind=critique", headers)
    assert status == 303
    assert resp_headers["Location"] == POSTING_LOCATION
    job = ai_api.cop.latest(UID)
    assert job["kind"] == "analysis" and job["state"] == "queued"  # critique is an alias
    assert len(ai_api.cop.jobs()) == 1


def test_action_non_html_post_returns_json_job(ai_api):
    status, headers, body = _post(ai_api.port, ACTION_PATH, b"kind=gaps",
                                  {**_auth_header(), **FORM})
    assert status == 200
    assert headers["Content-Type"] == "application/json"
    data = json.loads(body)
    assert data["ok"] is True
    assert data["job"]["uid"] == UID and data["job"]["kind"] == "analysis"  # gaps is an alias
    assert data["job"]["state"] == "queued"
    assert data["job"]["id"] == ai_api.cop.latest(UID)["id"]


def test_action_json_body_is_accepted(ai_api):
    status, _, body = _post(ai_api.port, ACTION_PATH, json.dumps({"kind": "letter"}).encode(),
                            {**_auth_header(), **JSON_HDR})
    assert status == 200 and json.loads(body)["job"]["kind"] == "letter"


@pytest.mark.parametrize("body", [b"kind=send", b"kind=", b"status=applied", b"kind=LETTER"])
def test_action_bad_kind_is_400_and_queues_nothing(ai_api, body):
    status, _, resp = _post(ai_api.port, ACTION_PATH, body, {**_auth_header(), **FORM,
                                                            "Accept": "text/html"})
    assert status == 400
    assert json.loads(resp)["error"] == "unknown kind"
    assert ai_api.cop.jobs() == []


def test_action_unknown_uid_is_404_and_queues_nothing(ai_api):
    status, headers, body = _post(ai_api.port, "/api/postings/nope%3Anone/action", b"kind=letter",
                                  {**_auth_header(), **FORM, "Accept": "text/html"})
    assert status == 404
    assert headers["Content-Type"] == "application/json"
    assert json.loads(body)["error"] == "unknown uid"
    assert ai_api.cop.jobs() == []


def test_action_requires_auth_and_same_origin(ai_api):
    assert _post(ai_api.port, ACTION_PATH, b"kind=letter", FORM)[0] == 401
    headers = {**_auth_header(), **FORM, "Origin": "https://evil.example"}
    assert _post(ai_api.port, ACTION_PATH, b"kind=letter", headers)[0] == 403
    assert ai_api.cop.jobs() == []


def test_action_without_copilot_is_503(api):
    status, _, body = _post(api, ACTION_PATH, b"kind=letter", {**_auth_header(), **FORM})
    assert status == 503 and "copilot" in json.loads(body)["error"]


def test_action_path_other_shapes_are_405(ai_api):
    for path in ("/api/postings/" + UID.replace("/", "%2F") + "/actions",
                 "/api/postings/" + UID.replace("/", "%2F") + "/action/extra"):
        assert _post(ai_api.port, path, b"kind=letter", {**_auth_header(), **FORM})[0] == 405, path


def test_posting_page_has_one_analysis_form_and_no_critique_form(ai_api):
    text = _page(ai_api.port, step="review")
    assert text.count('name="kind" value="analysis"') == 1
    assert 'name="kind" value="critique"' not in text
    assert 'name="kind" value="gaps"' not in text


def test_posting_page_shows_job_states_and_refresh_only_while_busy(ai_api, tree):
    cop = ai_api.cop
    assert REFRESH not in _page(ai_api.port)

    queued = cop.submit(UID, "letter")
    text = _page(ai_api.port, step="letter")
    assert REFRESH in text and text.index(REFRESH) < text.index("</head>")
    assert "letter queued" in text

    _set_job(cop, queued["id"], state="running",
             started=(datetime.now(timezone.utc) - timedelta(seconds=42)).isoformat(timespec="seconds"))
    text = _page(ai_api.port, step="letter")
    assert REFRESH in text
    assert "letter running:" in text and "min elapsed" in text

    (tree / "packets" / UID_DIR).mkdir(parents=True, exist_ok=True)
    (tree / "packets" / UID_DIR / "cover_letter.pdf").write_bytes(b"%PDF")
    _set_job(cop, queued["id"], state="done", finished=datetime.now(timezone.utc).isoformat(),
             artifact_path=str(tree / "packets" / UID_DIR / "cover_letter.pdf"))
    text = _page(ai_api.port, step="letter")
    assert REFRESH not in text
    assert "last letter done" in text
    assert f'href="/packets/{UID_DIR}/cover_letter.pdf"' in _page(ai_api.port, step="packet")

    # A busy job on another posting must not turn on refresh here.
    cop.submit(OTHER_UID, "gaps")
    assert REFRESH not in _page(ai_api.port)
    assert REFRESH in _page(ai_api.port, OTHER_UID)


def test_posting_page_shows_failed_and_french_blocked_jobs(ai_api):
    cop = ai_api.cop
    job = cop.submit(UID, "gaps")
    _set_job(cop, job["id"], state="failed", error="quota <gap>", finished="2026-11-23T10:00:00+00:00")
    text = _page(ai_api.port, step="letter")
    assert "last analysis failed" in text and "quota &lt;gap&gt;" in text
    assert REFRESH not in text

    _set_job(cop, job["id"], error="FRENCH_BLOCKER: must be bilingual in French", blocker="french")
    text = _page(ai_api.port, step="letter")
    assert "rejected: the posting asks for French" in text
    assert "must be bilingual in French" in text


def test_posting_page_shows_stored_critique_and_gaps_escaped(ai_api, tree):
    packet = tree / "packets" / UID_DIR
    packet.mkdir(parents=True, exist_ok=True)
    (packet / "critique.md").write_text(
        "VERDICT: <script>alert(1)</script> fit\nSTRONGEST_MATCHES: detection engineering\n",
        encoding="utf-8")
    text = _page(ai_api.port)
    assert "<script>alert(1)</script>" not in text
    assert "VERDICT: &lt;script&gt;alert(1)&lt;/script&gt; fit" in text
    assert "STRONGEST_MATCHES: detection engineering" in text
    assert "Critique" in text and "gaps.md" not in text


def test_critique_and_gaps_files_are_served_as_text(server, tree):
    (tree / "packets" / PACKET / "critique.md").write_text("VERDICT: fit", encoding="utf-8")
    (tree / "packets" / PACKET / "gaps.md").write_text("TRUE_GAPS: none", encoding="utf-8")
    for name in ("critique.md", "gaps.md"):
        status, headers, _ = _req(server, "GET", f"/packets/{PACKET}/{name}", _auth_header())
        assert status == 200 and headers["Content-Type"] == "text/plain; charset=utf-8"


# ---------- prep: the no-AI "Build packet" action ----------

def test_action_form_post_accepts_prep_and_303s(ai_api):
    headers = {**_auth_header(), **FORM, "Accept": "text/html,application/xhtml+xml"}
    status, resp_headers, _ = _post(ai_api.port, ACTION_PATH, b"kind=prep", headers)
    assert status == 303 and resp_headers["Location"] == POSTING_LOCATION
    job = ai_api.cop.latest(UID)
    assert job["kind"] == "prep" and job["state"] == "queued"


def test_action_json_prep_is_accepted(ai_api):
    status, _, body = _post(ai_api.port, ACTION_PATH, json.dumps({"kind": "prep"}).encode(),
                            {**_auth_header(), **JSON_HDR})
    assert status == 200 and json.loads(body)["job"]["kind"] == "prep"


def test_bad_kind_error_lists_prep_among_allowed(ai_api):
    status, _, body = _post(ai_api.port, ACTION_PATH, b"kind=send",
                            {**_auth_header(), **FORM, "Accept": "text/html"})
    assert status == 400 and "prep" in json.loads(body)["allowed"]


def test_the_packet_step_builds_and_the_letter_step_drafts_with_its_own_timing(ai_api):
    packet = _page(ai_api.port, step="packet")
    assert "Build packet" in packet and "seconds" in packet
    assert 'value="letter"' not in packet
    letter = _page(ai_api.port, step="letter")
    assert "Draft with AI" in letter and "2-3 min" in letter


def test_posting_page_prep_row_is_visually_separated(ai_api):
    text = _page(ai_api.port, step="packet")
    assert '<div class="prep-row">' in text and ".prep-row{" in text


def test_done_prep_job_shows_message_and_no_refresh(ai_api):
    cop = ai_api.cop
    job = cop.submit(UID, "prep")
    _set_job(cop, job["id"], state="done", finished=datetime.now(timezone.utc).isoformat(),
             message="packet dir: /tmp/x")
    text = _page(ai_api.port, step="letter")
    assert "last prep done" in text and "packet dir: /tmp/x" in text
    assert REFRESH not in text


def test_failed_prep_job_shows_error(ai_api):
    cop = ai_api.cop
    job = cop.submit(UID, "prep")
    _set_job(cop, job["id"], state="failed", error="no variant built",
             finished="2026-11-23T10:00:00+00:00")
    text = _page(ai_api.port, step="letter")
    assert "last prep failed" in text and "no variant built" in text


# ---------- hand edit: POST /api/postings/UID/body and the editor ----------

BODY_PATH = "/api/postings/" + UID.replace("/", "%2F") + "/body"


@pytest.fixture
def body_api(ai_api, monkeypatch):
    """ai_api with LaTeX and pdftotext replaced by fakes, so no tectonic runs."""
    def render(uid, body_path, out_dir=None, **kwargs):
        out = Path(out_dir)
        (out / "cover_letter.pdf").write_bytes(b"%PDF fake " + Path(body_path).read_bytes())
        (out / "cover_letter.tex").write_text("tex", encoding="utf-8")
        return str((out / "cover_letter.pdf").resolve())
    monkeypatch.setattr(copilot_mod, "render_letter", render)
    monkeypatch.setattr(copilot_mod, "_pdf_text", lambda pdf: "plain text\n")
    return ai_api


def _body_post(port, text, html=False):
    headers = {**_auth_header(), **FORM}
    if html:
        headers["Accept"] = "text/html,application/xhtml+xml"
    return _post(port, BODY_PATH, urlencode({"body": text}).encode(), headers)


def test_body_form_post_recompiles_and_303s(body_api, tree):
    status, headers, _ = _body_post(body_api.port, "Hand edited.\nSecond line.\n", html=True)
    assert status == 303 and headers["Location"] == POSTING_LOCATION
    pdf = tree / "packets" / UID_DIR / "cover_letter.pdf"
    assert pdf.read_bytes() == b"%PDF fake Hand edited.\nSecond line.\n"
    assert (body_api.cop.bodies_dir / f"{UID_DIR}.txt").read_text(encoding="utf-8") == \
        "Hand edited.\nSecond line.\n"


def test_body_non_html_post_returns_ok_and_path(body_api, tree):
    status, headers, body = _body_post(body_api.port, "ok text\n")
    assert status == 200 and headers["Content-Type"] == "application/json"
    assert json.loads(body) == {"ok": True, "path": str(tree / "packets" / UID_DIR / "cover_letter.pdf")}


def test_body_non_ascii_is_400_with_message_and_writes_nothing(body_api, tree):
    status, _, body = _body_post(body_api.port, "caf\u00e9 au lait\n")
    assert status == 400
    assert "non-ASCII" in json.loads(body)["error"]
    assert not (tree / "packets" / UID_DIR / "cover_letter.pdf").exists()
    assert not (body_api.cop.bodies_dir / f"{UID_DIR}.txt").exists()
    assert not (body_api.cop.bodies_dir / f"{UID_DIR}.candidate.txt").exists()


def test_body_empty_is_400_and_writes_nothing(body_api, tree):
    status, _, body = _body_post(body_api.port, "  \n")
    assert status == 400 and "empty" in json.loads(body)["error"]
    assert not (tree / "packets" / UID_DIR / "cover_letter.pdf").exists()


def test_body_unknown_uid_is_404_and_writes_nothing(body_api, tree):
    path = "/api/postings/nope%3Anone%2F0000/body"
    status, _, body = _post(body_api.port, path, urlencode({"body": "text\n"}).encode(),
                            {**_auth_header(), **FORM})
    assert status == 404 and json.loads(body)["error"] == "unknown uid"
    assert not (body_api.cop.bodies_dir / "nope_none_0000.txt").exists()


def test_body_render_failure_is_422_and_keeps_previous_pdf(body_api, tree, monkeypatch):
    assert _body_post(body_api.port, "first\n")[0] == 200
    pdf = tree / "packets" / UID_DIR / "cover_letter.pdf"
    old = pdf.read_bytes()

    def boom(uid, body_path, out_dir=None, **kwargs):
        raise RuntimeError("tectonic failed")
    monkeypatch.setattr(copilot_mod, "render_letter", boom)
    status, _, body = _body_post(body_api.port, "second\n")
    assert status == 422 and "render_failed" in json.loads(body)["error"]
    assert pdf.read_bytes() == old
    assert (body_api.cop.bodies_dir / f"{UID_DIR}.txt").read_text(encoding="utf-8") == "first\n"


def test_body_post_requires_auth_origin_and_urlencoded(body_api):
    data = urlencode({"body": "x\n"}).encode()
    assert _post(body_api.port, BODY_PATH, data, FORM)[0] == 401
    status, _, _ = _post(body_api.port, BODY_PATH, data,
                         {**_auth_header(), **FORM, "Origin": "https://evil.test"})
    assert status == 403
    status, _, _ = _post(body_api.port, BODY_PATH, json.dumps({"body": "x"}).encode(),
                         {**_auth_header(), **JSON_HDR})
    assert status == 415


def test_posting_page_shows_escaped_editor_when_body_exists(body_api):
    bodies = body_api.cop.bodies_dir
    bodies.mkdir(parents=True, exist_ok=True)
    (bodies / f"{UID_DIR}.txt").write_text('Dear <b>team</b> & "all"\n', encoding="utf-8")
    text = _page(body_api.port, step="letter")
    # The editor escapes the stored body; the markup around it is the workspace's.
    assert 'name="body"' in text
    assert 'Dear &lt;b&gt;team&lt;/b&gt; &amp; &quot;all&quot;' in text
    assert "<b>team</b>" not in text
    assert "Recompile PDF" in text and "Recompiles locally in about 2s" in text
    assert f'action="/api/postings/{quote(UID, safe="")}/body"' in text
    assert text.index("Cover letter") < text.index("Recompile PDF")
    assert text.index('name="body"') < text.index("Recompile PDF")


def test_posting_page_omits_editor_when_no_body(body_api):
    text = _page(body_api.port, step="letter")
    # The editor is always present now; with no letter it is empty and says so.
    assert 'name="body"' in text and "Recompile PDF" in text
    assert "No letter yet." in text


# ---------- progressive JS layer on the posting page ----------

def _inline_script(text):
    """The page's own script: the last inline block, after the pre-paint theme script."""
    blocks = re.findall(r"<script>(.*?)</script>", text, re.S)
    assert blocks, "posting page must carry an inline <script>"
    return blocks[-1]


def test_posting_page_body_carries_uid_for_the_script(api):
    text = _page(api)
    body_tag = text[text.index("<body"):text.index(">", text.index("<body")) + 1]
    assert f'data-uid="{UID}"' in body_tag


def test_posting_page_script_is_inline_and_last(api):
    text = _page(api)
    assert "<script src" not in text
    # The last script is the page's own, and it sits after the markup it drives.
    assert text.rindex("<script>") > text.index("</footer>")
    assert text.rindex("</script>") < text.index("</body>")


def test_posting_page_script_references_the_four_api_routes(api):
    js = _inline_script(_page(api))
    assert '"/api/postings/"' in js          # GET posting: polling and the status refresh
    assert '"/action"' in js and '"/status"' in js and '"/body"' in js
    assert 'Accept: "application/json"' in js


def test_posting_page_script_writes_api_data_with_textcontent_only(api):
    js = _inline_script(_page(api))
    assert "textContent" in js and "createTextNode" in js
    assert "innerHTML" not in js and "outerHTML" not in js
    assert "insertAdjacentHTML" not in js and "document.write" not in js


def test_posting_page_script_creates_no_globals(api):
    js = _inline_script(_page(api))
    assert js.lstrip().startswith("(function () {") and js.rstrip().endswith("})();")
    assert "window." not in js.replace("window.fetch", "").replace("window.URLSearchParams", "") \
        .replace("window.FormData", "").replace("window.addEventListener", "")


def test_no_js_case_keeps_meta_refresh_while_busy_inside_noscript(ai_api):
    cop = ai_api.cop
    cop.submit(UID, "letter")
    text = _page(ai_api.port)
    noscript = f"<noscript>{REFRESH}</noscript>"
    assert noscript in text
    assert text.index(noscript) < text.index("</head>")


def test_get_posting_api_reports_latest_job_for_polling(ai_api):
    status, _, body = _req(ai_api.port, "GET", "/api/postings/" + UID.replace("/", "%2F"), _auth_header())
    assert status == 200 and json.loads(body)["latest_job"] is None
    job = ai_api.cop.submit(UID, "gaps")
    status, _, body = _req(ai_api.port, "GET", "/api/postings/" + UID.replace("/", "%2F"), _auth_header())
    data = json.loads(body)
    assert data["latest_job"]["id"] == job["id"] and data["latest_job"]["state"] == "queued"


def test_posting_page_has_hooks_for_banner_buttons_status_and_timeline(ai_api):
    ai_api.cop.submit(UID, "gaps")  # the job line is only rendered once a job exists
    assert 'data-review' in _page(ai_api.port, step="review")
    assert 'data-queue-strip' in _page(ai_api.port, step="letter")
    for step in ("review", "packet", "letter", "submit"):
        text = _page(ai_api.port, step=step)
        assert 'id="status-text"' in text, step
    # the activity card exists only where there is something in it
    _status_json(ai_api.port, "shortlisted")
    assert 'class="timeline"' in _page(ai_api.port, step="review")


# ---------- allowed_next: the status POST reply carries the next moves ----------

def _status_json(api, status):
    status_code, _, body = _post(api, "/api/postings/" + UID.replace("/", "%2F") + "/status",
                                 json.dumps({"status": status}).encode(),
                                 {**_auth_header(), **JSON_HDR})
    assert status_code == 200
    return json.loads(body)


def test_status_json_allowed_next_lists_the_moves_for_a_status_with_moves(api, jobs_db):
    from jobsearch.dashboard import ALLOWED_NEXT
    data = _status_json(api, "shortlisted")
    assert data["allowed_next"] == list(ALLOWED_NEXT["shortlisted"]) == ["prepped", "applied", "skipped"]
    assert data["status"] == "shortlisted" and data["ok"] is True


def test_status_json_allowed_next_is_empty_for_a_terminal_status(api, jobs_db):
    data = _status_json(api, "skipped")
    assert data["allowed_next"] == []
    assert _status_of(jobs_db, UID) == "skipped"


def test_status_json_allowed_next_follows_the_new_status_not_the_old(api, jobs_db):
    _status_json(api, "shortlisted")
    data = _status_json(api, "prepped")
    assert data["allowed_next"] == ["applied", "skipped"]


def test_form_post_json_reply_also_carries_allowed_next(api, jobs_db):
    status, _, body = _post(api, "/api/postings/" + UID.replace("/", "%2F") + "/status",
                            b"status=interview", {**_auth_header(), **FORM})
    assert status == 200
    assert json.loads(body)["allowed_next"] == ["offer", "rejected", "closed"]


# ---------- the page script rebuilds the status select from allowed_next ----------

def test_posting_page_script_reads_allowed_next_from_the_status_reply(api):
    js = _inline_script(_page(api))
    assert "allowed_next" in js
    assert "rebuildNext(data.allowed_next, data.status)" in js


def test_posting_page_script_clears_options_inside_the_optgroups(api):
    js = _inline_script(_page(api))
    clear = js.index("o.parentNode.removeChild(o)")
    next_group = js.index('groupFor(sel, "Next move")')
    per_move = js.index("nextGroup.appendChild(optionFor(s))")
    assert clear < next_group < per_move
    assert "sel.appendChild(g)" in js  # a group is created only when the page lacks one


def test_posting_page_script_keeps_select_name_and_required_by_not_recreating_it(api):
    js = _inline_script(_page(api))
    assert 'createElement("select")' not in js and 'el("select"' not in js
    assert "No further moves for this status." in js


# ---------- live cover-letter editor: word count, non-ASCII line, copy, unload guard ----------

def test_live_checks_are_asked_of_the_server_not_reimplemented(api):
    js = _inline_script(_page(api))
    assert 'API + "/checks"' in js and "function paintChecks" in js
    assert "function liveChecks" in js and "setTimeout(liveChecks, 500)" in js
    # the rows that come back are the validator's own, painted as text
    assert 'el("div", "check " + (c.ok ? "ok" : "warn"))' in js
    assert "box.innerHTML" not in js and "row.innerHTML" not in js


def test_body_editor_script_writes_the_word_count_with_textcontent(api):
    js = _inline_script(_page(api))
    assert 'querySelectorAll("[data-word-count]")' in js
    assert "node.textContent = String(n)" in js
    assert "word.innerHTML" not in js


def test_body_editor_script_copy_uses_clipboard_writetext_first(api):
    js = _inline_script(_page(api))
    assert "navigator.clipboard" in js and "writeText" in js
    assert 'typeof clip.writeText === "function"' in js
    assert '"Copy plain text"' in js and '"copied"' in js
    # the modern path is tried before the legacy one
    assert js.index("navigator.clipboard") < js.index('document.execCommand("copy")')


def test_body_editor_script_copy_has_execcommand_textarea_fallback(api):
    js = _inline_script(_page(api))
    assert 'document.createElement("textarea")' in js
    assert "ta.value = text" in js and "ta.select()" in js
    assert 'document.execCommand("copy") === true' in js
    assert 'ta.style.left = "-9999px"' in js          # offscreen
    assert "ta.parentNode.removeChild(ta)" in js      # removed again, in a finally
    assert "legacyCopy(text)" in js


def test_body_editor_script_copy_failure_shows_message_and_never_throws(api):
    js = _inline_script(_page(api))
    assert '"copy failed"' in js
    assert "copyFailed()" in js
    assert 'flashCopy("copy failed")' in js
    # the click handler and the legacy helper are guarded so a throw cannot escape
    handler = js[js.index('copyBtn.addEventListener("click"'):]
    assert "try {" in handler and "catch (e)" in handler
    assert "try {" in js[js.index("function legacyCopy"):] and "finally {" in js[js.index("function legacyCopy"):]


def test_body_editor_copy_failure_selects_textarea_and_says_ctrl_c(api):
    js = _inline_script(_page(api))
    start = js.index("function copyFailed")
    body = js[start:js.index('copyBtn.addEventListener("click"')]
    # the failure branch focuses and selects the textarea, then names the next key press
    assert "bodyEdit.focus()" in body
    assert "bodyEdit.select()" in body
    assert 'flashCopy("select-all done, press Ctrl+C")' in body
    assert body.index("bodyEdit.select()") < body.index("select-all done")
    # still guarded: a throw here falls back to the plain failure message
    assert "try {" in body and "catch (e)" in body and 'flashCopy("copy failed")' in body
    # the modern path is tried first, the execCommand path second, copyFailed only after both
    handler = js[js.index('copyBtn.addEventListener("click"'):]
    assert (handler.index("clip.writeText(text)") < handler.index("legacyCopy(text)")
            < handler.index("copyFailed()"))
    # the success message is unchanged
    assert 'flashCopy("copied")' in handler


def test_body_editor_script_copy_button_is_type_button_and_only_button_created(api):
    js = _inline_script(_page(api))
    # the buttons the script builds: the copy button, and the 409 gate's Run anyway; both type=button
    assert js.count('el("button"') + js.count('createElement("button")') == 2
    assert 'copyBtn.type = "button";' in js and 'b.type = "button";' in js
    # the other controls it adds are a select and its options; none can submit a form
    assert 'createElement("input")' not in js and 'createElement("form")' not in js


def test_body_editor_script_has_a_beforeunload_guard_gated_on_edits_and_jobs(api):
    js = _inline_script(_page(api))
    assert 'window.addEventListener("beforeunload"' in js
    guard = js[js.index('addEventListener("beforeunload"'):]
    assert "bodyEdit.value === bodyBase" in guard
    assert "isActive(st.job)" in guard
    assert "ev.preventDefault()" in guard


def test_server_markup_has_no_counter_diagnostic_or_copy_button(body_api):
    bodies = body_api.cop.bodies_dir
    bodies.mkdir(parents=True, exist_ok=True)
    (bodies / f"{UID_DIR}.txt").write_text("Dear team,\n", encoding="utf-8")
    text = _page(body_api.port, step="letter")
    assert 'name="body"' in text  # the editor itself is rendered
    # the script and stylesheet name the classes; the markup itself must not carry the nodes
    markup = re.sub(r"<(script|style)>.*?</\1>", "", text, flags=re.S)
    for marker in ("body-count", "body-diag", "copy plain text", "words (target"):
        assert marker not in markup, marker


# ---------- queue API, analysis reach gate, AI card (added with the queue/reach work) ----------

OOR_UID = "greenhouse:acme/9001"
OOR_UID_Q = "/api/postings/" + OOR_UID.replace("/", "%2F")
OOR_ACTION = OOR_UID_Q + "/action"


def _ago(seconds: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat(timespec="seconds")


def _fj(jid, kind, state, *, created, started=None, est=200.0, stage="", uid=UID):
    return {"id": jid, "uid": uid, "kind": kind, "state": state, "created": created,
            "started": started, "finished": None, "error": "", "answer_path": "",
            "artifact_path": "", "blocker": "", "message": "", "stage": stage,
            "stage_secs": 0.0, "stages": [], "est_seconds": est}


class FakeCopilot:
    """Stands in for Copilot: jobs and latest only; submit records and queues nothing real."""

    def __init__(self, jobs=(), bodies_dir=None):
        self._jobs = list(jobs)
        self.bodies_dir = Path(bodies_dir) if bodies_dir else Path("/nonexistent-bodies")

    def jobs(self):
        return [dict(j) for j in self._jobs]

    def latest(self, uid):
        found = [dict(j) for j in self._jobs if j["uid"] == uid]
        return found[-1] if found else None

    def submit(self, uid, kind):
        job = _fj(f"new{len(self._jobs)}", copilot_mod.ALIASES.get(kind, kind), "queued",
                  created=_ago(0), uid=uid)
        self._jobs.append(job)
        return dict(job)


@pytest.fixture
def fake_api(jobs_db, monkeypatch):
    """Start a server on port 0 with the given copilot object; returns the port."""
    monkeypatch.setenv("JOBSEARCH_SERVE_USER", USER)
    monkeypatch.setenv("JOBSEARCH_SERVE_PASSWORD", PASSWORD)
    running = []

    def start(copilot):
        srv = serve.make_server("127.0.0.1", 0, USER, PASSWORD, db_path=str(jobs_db),
                                copilot=copilot)
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


def _add_out_of_reach(db):
    store = Store(str(db))
    try:
        store.upsert_postings([{
            "uid": OOR_UID, "source": "greenhouse", "company": "Acme",
            "title": "Principal Engineer", "location": "Remote",
            "url": "https://example.invalid/job/9001",
            "description": "We need 7+ years of experience in distributed systems.",
        }])
    finally:
        store.close()


def test_api_jobs_without_copilot_is_empty(api):
    status, _, body = _req(api, "GET", "/api/jobs", headers=_auth_header())
    assert status == 200
    assert json.loads(body) == {"ok": True, "jobs": [], "running": None, "queued": 0}


def test_api_jobs_derives_elapsed_eta_and_position(fake_api):
    jobs = [
        _fj("done1", "analysis", "done", created=_ago(400), started=_ago(380), est=200),
        _fj("run1", "analysis", "running", created=_ago(60), started=_ago(30), est=200,
            stage="send"),
        _fj("q1", "letter", "queued", created=_ago(50), est=200),
        _fj("q2", "analysis", "queued", created=_ago(20), est=200),
    ]
    port = fake_api(FakeCopilot(jobs))
    status, _, body = _req(port, "GET", "/api/jobs", headers=_auth_header())
    assert status == 200
    data = json.loads(body)
    assert data["ok"] is True and data["queued"] == 2
    assert data["running"]["id"] == "run1"
    assert [j["id"] for j in data["jobs"]] == ["q2", "q1", "run1", "done1"]
    by_id = {j["id"]: j for j in data["jobs"]}
    assert by_id["run1"]["position"] == 1
    assert 28 <= by_id["run1"]["elapsed_s"] <= 33 and 166 <= by_id["run1"]["eta_s"] <= 172
    assert by_id["q1"]["position"] == 2 and 148 <= by_id["q1"]["eta_s"] <= 152
    assert by_id["q2"]["position"] == 3 and 178 <= by_id["q2"]["eta_s"] <= 182
    assert by_id["done1"]["position"] == 0 and by_id["done1"]["eta_s"] == 0


def test_api_jobs_returns_only_newest_twenty(fake_api):
    jobs = [_fj(f"j{i}", "analysis", "done", created=_ago(100 - i)) for i in range(25)]
    data = json.loads(_req(fake_api(FakeCopilot(jobs)), "GET", "/api/jobs",
                           headers=_auth_header())[2])
    assert len(data["jobs"]) == 20
    assert data["jobs"][0]["id"] == "j24" and data["jobs"][-1]["id"] == "j5"


def test_posting_json_carries_analysis_reach_and_queue(ai_api, tree):
    packet = tree / "packets" / UID_DIR
    packet.mkdir(parents=True, exist_ok=True)
    (packet / "analysis.md").write_text("Fit: good.\n", encoding="utf-8")
    ai_api.cop.submit(UID, "critique")  # retired alias: queues analysis
    status, _, body = _req(ai_api.port, "GET", "/api/postings/" + UID.replace("/", "%2F"),
                           headers=_auth_header())
    assert status == 200
    data = json.loads(body)
    assert data["analysis"] == "Fit: good.\n"
    assert data["critique"] is None and data["gaps"] is None
    assert data["reach"]["verdict"] in ("in_reach", "stretch", "out_of_reach")
    assert data["queue"] == {"position": 1, "running": None, "queued": 1, "est_wait_s": 0}
    assert data["title"] == "Engineer"  # existing fields keep their names and values


def test_action_critique_queues_an_analysis_job(ai_api):
    status, _, body = _post(ai_api.port, ACTION_PATH, b"kind=critique", {**_auth_header(), **FORM})
    assert status == 200
    data = json.loads(body)
    assert data["ok"] is True and data["job"]["kind"] == "analysis"
    assert data["queue"]["position"] == 1


def test_out_of_reach_analysis_is_409_until_forced(ai_api, jobs_db):
    _add_out_of_reach(jobs_db)
    status, _, body = _post(ai_api.port, OOR_ACTION, b"kind=analysis", {**_auth_header(), **FORM})
    assert status == 409
    data = json.loads(body)
    assert data["error"] == "out of reach"
    assert data["reach"]["verdict"] == "out_of_reach" and data["reach"]["in_reach"] is False
    assert ai_api.cop.jobs() == []
    status, _, body = _post(ai_api.port, OOR_ACTION, b"kind=analysis&force=1",
                            {**_auth_header(), **FORM})
    assert status == 200
    assert json.loads(body)["job"]["kind"] == "analysis"
    assert len(ai_api.cop.jobs()) == 1


def test_out_of_reach_html_caller_gets_409_page_with_force_form(ai_api, jobs_db):
    _add_out_of_reach(jobs_db)
    headers = {**_auth_header(), **FORM, "Accept": "text/html"}
    status, _, body = _post(ai_api.port, OOR_ACTION, b"kind=analysis", headers)
    text = body.decode("utf-8")
    assert status == 409 and "Out of reach" in text
    assert 'name="force" value="1"' in text and 'name="kind" value="analysis"' in text
    assert ai_api.cop.jobs() == []


def test_prep_is_never_gated(ai_api, jobs_db):
    _add_out_of_reach(jobs_db)
    status, _, body = _post(ai_api.port, OOR_ACTION, b"kind=prep", {**_auth_header(), **FORM})
    assert status == 200
    assert json.loads(body)["job"]["kind"] == "prep"


def test_ai_block_html_does_not_raise_with_fake_copilot(tree, tmp_path):
    cop = FakeCopilot([_fj("r1", "analysis", "running", created=_ago(40), started=_ago(30),
                           stage="submit")], bodies_dir=tmp_path)
    html = serve._ai_block_html(UID, cop)
    assert 'name="kind" value="analysis"' in html and "Analyze fit" in html
    assert 'class="q-main"' in html and 'class="q-note"' in html
    assert "committing the task" in html


def test_ai_block_html_names_out_of_reach_with_force_and_queue_strip(tmp_path, jobs_db):
    _add_out_of_reach(jobs_db)
    store = Store(str(jobs_db))
    try:
        posting = store.get(OOR_UID)
    finally:
        store.close()
    cop = FakeCopilot([], bodies_dir=tmp_path)
    html = serve._ai_block_html(OOR_UID, cop, posting=posting)
    assert re.search(r'<form[^>]*data-action-form[^>]*>\s*<input type="hidden" name="kind" '
                     r'value="analysis">', html)
    assert 'data-queue-strip' in html and 'role="status"' in html
    assert 'data-reach="out_of_reach"' in html and 'name="force" value="1"' in html
    assert "Run anyway" in html


def test_backfill_skips_missing_db_and_never_creates_it(tmp_path):
    missing = tmp_path / "nope.db"
    assert serve._backfill_levels(str(missing)) == 0
    assert not missing.exists()
    serve.make_server("127.0.0.1", 0, USER, PASSWORD, db_path=str(missing)).server_close()
    assert not missing.exists()


# ---------- workspace ctx glue and the triage reach count ----------

WS_POSTING = {"uid": UID, "source": "greenhouse", "company": "Acme", "title": "ML Engineer",
              "location": "Remote", "url": "https://example.invalid/job/4711",
              "description": "Build pipelines.", "status": "new"}


def test_workspace_context_carries_analysis_reach_force_and_queue_place(tree, tmp_path):
    packet = tree / "packets" / UID_DIR
    packet.mkdir(parents=True, exist_ok=True)
    (packet / "analysis.md").write_text("Fit: good.\n", encoding="utf-8")
    cop = FakeCopilot([_fj("q1", "analysis", "queued", created=_ago(40))])
    ctx = serve.workspace_context(dict(WS_POSTING), [], cop, back_url="/")
    assert ctx["analysis_text"] == "Fit: good.\n"
    assert ctx["reach"]["verdict"] == "in_reach" and "force_reach" not in ctx
    assert ctx["latest_job"]["position"] == 1
    assert 0 < ctx["latest_job"]["eta_s"] <= 200
    assert cop._jobs[0].get("position") is None  # the copilot's own copy is untouched


def test_workspace_context_never_pre_sets_force_for_out_of_reach(tree):
    oor = dict(WS_POSTING, title="Principal Engineer",
               description="We need 7+ years of experience in distributed systems.")
    ctx = serve.workspace_context(oor, [], None, back_url="/")
    assert ctx["reach"]["verdict"] == "out_of_reach"
    assert "force_reach" not in ctx  # the page never supplies force=1; only the Run anyway click does
    assert ctx["analysis_text"] is None
    assert "latest_job" not in ctx or ctx["latest_job"] is None


def test_triage_ctx_carries_reach_hidden_when_reach_filter_hides_rows(api, jobs_db, monkeypatch):
    _add_out_of_reach(jobs_db)
    store = Store(str(jobs_db))
    try:
        store.set_score(OOR_UID, 90.0, "A", ["match"])  # above the inbox score bar, so reach decides
        store.backfill_levels(serve.reach.classify)  # the server does this at start
    finally:
        store.close()
    seen = []
    real = serve.triage.render_triage

    def spy(ctx):
        seen.append(ctx["reach_hidden"])
        return real(ctx)

    monkeypatch.setattr(serve.triage, "render_triage", spy)
    status, _, _ = _req(api, "GET", "/?view=inbox&reach=1", _auth_header())
    assert status == 200 and seen[-1] == 1
    status, _, _ = _req(api, "GET", "/?view=inbox&reach=0", _auth_header())
    assert status == 200 and seen[-1] == 0


def test_posting_js_has_no_banner_path_and_one_strip_surface():
    js = serve.POSTING_JS
    for dead in ("job-banner", "job-main", "job-note", "job-clock", "ensureBanner", "renderJob"):
        assert dead not in js
    assert "s.hidden = !st.job && !st.note" in js
    assert "jobLines" in js and "[data-job-line]" in js


def test_posting_js_sanitises_every_fetch_url():
    js = serve.POSTING_JS
    assert 'u.username = "";' in js and 'u.password = "";' in js
    assert "fetch(cleanUrl(href)" in js
    assert js.count("fetch(") == 1  # the one call() helper is the only caller of fetch


def test_posting_js_never_prints_about_zero_minutes():
    js = serve.POSTING_JS
    assert "under a minute" in js
    assert 'mins(left)' not in js and "about \" + mins(" in js  # only via leftText


def test_api_jobs_settled_job_reports_its_real_duration_not_time_since_it_settled(fake_api):
    base = datetime.now(timezone.utc).replace(microsecond=0)
    job = _fj("fin1", "prep", "done", created=(base - timedelta(seconds=300)).isoformat(),
              started=(base - timedelta(seconds=283)).isoformat())
    job["finished"] = (base - timedelta(seconds=266)).isoformat()
    failed = _fj("bad1", "prep", "failed", created=(base - timedelta(seconds=300)).isoformat(),
                 started=(base - timedelta(seconds=294)).isoformat())
    failed["finished"] = (base - timedelta(seconds=288)).isoformat()   # ran 6s, settled long ago
    port = fake_api(FakeCopilot([job, failed]))
    status, _, body = _req(port, "GET", "/api/jobs", headers=_auth_header())
    assert status == 200
    by_id = {j["id"]: j for j in json.loads(body)["jobs"]}
    assert by_id["fin1"]["elapsed_s"] == 17 and by_id["fin1"]["eta_s"] == 0
    assert by_id["bad1"]["elapsed_s"] == 6 and by_id["bad1"]["eta_s"] == 0


# ---------- posting page: job line, queue place, polling ceiling, 409 gate, status forms ----------

def test_job_line_html_carries_data_job_line_in_every_state():
    for state, extra in (("queued", {}), ("running", {"started": None}),
                         ("done", {"message": "ok"}), ("failed", {"error": "boom"})):
        html = serve._job_line_html({"kind": "prep", "state": state, **extra}, UID)
        assert html.startswith('<div class="meta job-line" data-job-line>'), state


def test_posting_api_latest_job_has_the_same_place_and_eta_as_api_jobs(ai_api):
    ai_api.cop.submit(UID, "gaps")
    status, _, body = _req(ai_api.port, "GET", "/api/postings/" + UID.replace("/", "%2F"),
                           headers=_auth_header())
    latest = json.loads(body)["latest_job"]
    assert latest["position"] == 1 and "eta_s" in latest
    status, _, body = _req(ai_api.port, "GET", "/api/jobs", headers=_auth_header())
    listed = next(j for j in json.loads(body)["jobs"] if j["id"] == latest["id"])
    assert (listed["position"], listed["eta_s"]) == (latest["position"], latest["eta_s"])


def test_polling_ceiling_is_above_the_call_timeout_and_the_script_agrees():
    from jobsearch import copilot
    assert serve.POLL_CEILING_S == 40 * 60
    assert serve.POLL_CEILING_S > copilot.CALL_TIMEOUT_S
    js = serve.POSTING_JS
    assert "CEILING_MS = 40 * 60 * 1000" in js and "WINDOW_MS = 10 * 60 * 1000" in js
    assert "Date.now() > st.ceilingAt" in js
    assert "Stopped live updates after 40 minutes" in js


def test_posting_js_turns_a_409_into_a_reason_and_a_run_anyway_with_force():
    js = serve.POSTING_JS
    assert "r.status === 409 && r.data.reach" in js
    assert "whyText(r.data.reach)" in js and '"Out of reach: "' in js
    assert 'force ? { force: "1" } : null' in js and "queueFrom(st.gate.form, true)" in js
    assert '"Run anyway"' in js and "queueFrom(form, false)" in js


def test_posting_js_binds_every_status_form_and_posts_json(api):
    js = serve.POSTING_JS
    assert "querySelectorAll('form[action$=\"/status\"]')" in js
    assert "each(statusForms, bindStatusForm)" in js and "sendJson(form)" in js
    assert "No further moves for this status." in js
    assert "form.remove" not in js and "removeChild(form)" not in js


def test_posting_js_applies_the_queue_from_the_action_reply_at_once():
    js = serve.POSTING_JS
    assert "st.queue = data.queue || {};   // place and estimate now" in js
    assert "function watch()" in js and "armCeiling(st.job);" in js


def test_out_of_reach_json_reply_carries_the_reason_the_gate_shows(ai_api, jobs_db):
    _add_out_of_reach(jobs_db)
    status, _, body = _post(ai_api.port, OOR_ACTION, b"kind=analysis", {**_auth_header(), **FORM})
    assert status == 409 and "why" in json.loads(body)["reach"]


def test_the_page_never_reloads_itself_for_a_job_that_finished_before_it_loaded(api):
    js = serve.POSTING_JS
    assert "firstRead: true" in js
    # the first read paints and returns before the finished-job handlers can fire
    first = js.index("var first = st.firstRead;")
    guard = js.index("if (!first) {", first)
    assert first < guard < js.index("packetWhenDone(st.job);", guard)
    # and a reload can only happen once per page load
    assert "var reloading = false;" in js
    assert js.index("if (reloading) { return; }") < js.index("location.replace(")


def test_a_finished_build_re_renders_the_step_instead_of_patching_rows(api):
    js = serve.POSTING_JS
    assert "packetWhenDone(st.job)" in js
    assert "location.replace(location.pathname + location.search)" in js
    # no client-side packet surgery is left to drift from the server markup
    assert "syncPacket" not in js and "data-packet-list" not in js


def test_api_jobs_carries_company_and_title_and_keeps_every_field(fake_api):
    jobs = [
        _fj("run1", "analysis", "running", created=_ago(60), started=_ago(30), est=200,
            stage="send", uid=UID),
        _fj("q1", "analysis", "queued", created=_ago(20), est=200, uid="ghost:nowhere/0"),
    ]
    data = json.loads(_req(fake_api(FakeCopilot(jobs)), "GET", "/api/jobs",
                           headers=_auth_header())[2])
    by_id = {j["id"]: j for j in data["jobs"]}
    assert by_id["run1"]["company"] == "Acme" and by_id["run1"]["title"] == "Engineer"
    assert by_id["q1"]["company"] is None and by_id["q1"]["title"] is None
    assert data["running"]["company"] == "Acme" and data["running"]["title"] == "Engineer"
    assert data["running"]["stage_label"] == "sending"
    for key in ("id", "uid", "kind", "state", "created", "started", "finished", "stage",
                "stage_secs", "stages", "est_seconds", "error", "message", "elapsed_s",
                "eta_s", "position"):
        assert key in by_id["run1"]
    assert by_id["run1"]["position"] == 1 and by_id["q1"]["position"] == 2


def test_queue_panel_counts_and_orders_like_api_jobs(jobs_db):
    jobs = [
        _fj("run1", "analysis", "running", created=_ago(60), started=_ago(30), est=200,
            stage="send", uid=UID),
        _fj("q1", "letter", "queued", created=_ago(50), est=200, uid=OTHER_UID),
        _fj("q2", "analysis", "queued", created=_ago(20), est=200, uid=UID),
        _fj("done1", "analysis", "done", created=_ago(400), started=_ago(380), est=200, uid=UID),
    ]
    store = Store(str(jobs_db))
    try:
        panel = serve.queue_panel(FakeCopilot(jobs), store=store)
    finally:
        store.close()
    assert panel["count"] == 3
    assert panel["running"]["id"] == "run1" and panel["running"]["position"] == 1
    assert panel["running"]["company"] == "Acme" and panel["running"]["stage_label"] == "sending"
    assert [j["id"] for j in panel["queued"]] == ["q1", "q2"]
    assert [j["position"] for j in panel["queued"]] == [2, 3]
    assert panel["queued"][0]["title"] == "Analyst" and panel["queued"][0]["kind_label"] == "Draft cover letter"


def test_queue_panel_is_empty_when_idle_or_no_copilot(jobs_db):
    assert serve.queue_panel(None) == {"running": None, "queued": [], "count": 0}
    assert serve.queue_panel(FakeCopilot([]))["count"] == 0
    done = [_fj("d", "analysis", "done", created=_ago(9), started=_ago(8), uid=UID)]
    assert serve.queue_panel(FakeCopilot(done)) == {"running": None, "queued": [], "count": 0}


def test_prep_job_with_no_stage_reads_building_the_packet(fake_api):
    jobs = [_fj("p1", "prep", "running", created=_ago(20), started=_ago(10), est=30, uid=UID),
            _fj("a1", "analysis", "running", created=_ago(60), started=_ago(30), est=200, uid=UID)]
    data = json.loads(_req(fake_api(FakeCopilot(jobs)), "GET", "/api/jobs",
                           headers=_auth_header())[2])
    by_id = {j["id"]: j for j in data["jobs"]}
    assert by_id["p1"]["stage_label"] == "building the packet"
    assert by_id["a1"]["stage_label"] == "starting"


def _live_of(db, uid):
    store = Store(str(db))
    try:
        row = store.get(uid)
        return row["live_status"], row["live_checked_at"]
    finally:
        store.close()


def test_post_liveness_endpoint_returns_json_and_303_for_html_and_rejects_cross_origin(
        api, jobs_db, monkeypatch):
    from jobsearch import sources
    monkeypatch.setattr(sources, "_fetch_snippet",
                        lambda url, timeout=10: (404, b""))  # never touches the network
    path = "/api/postings/" + UID.replace("/", "%2F") + "/liveness"

    status, _, body = _post(api, path, json.dumps({}).encode(),
                            {**_auth_header(), **JSON_HDR})
    data = json.loads(body)
    assert status == 200 and data["ok"] is True and data["live_status"] == "gone"
    assert data["http_code"] == 404 and data["reason"] == "HTTP 404" and data["checked_at"]
    assert _live_of(jobs_db, UID) == ("gone", data["checked_at"])
    assert _status_of(jobs_db, UID) == "new"  # status is the human's call, not the check's
    assert _events(jobs_db, UID)[-1]["kind"] == "liveness"

    status, headers, _ = _post(api, path, b"return=/posting/x%3Fstep%3Dsubmit",
                               {**_auth_header(), **FORM, "Accept": "text/html"})
    assert status == 303 and headers["Location"] == "/posting/x?step=submit"

    status, headers, _ = _post(api, path, b"",
                               {**_auth_header(), **FORM, "Accept": "text/html"})
    assert status == 303 and headers["Location"].endswith("?step=submit")

    before = _live_of(jobs_db, UID)
    status, _, _ = _post(api, path, b"", {**_auth_header(), **FORM,
                                          "Origin": "https://evil.invalid"})
    assert status == 403 and _live_of(jobs_db, UID) == before

    status, _, body = _post(api, "/api/postings/nope%3A1/liveness", b"",
                            {**_auth_header(), **JSON_HDR})
    assert status == 404 and json.loads(body)["error"] == "unknown uid"

    req = _req(api, "GET", "/api/postings/" + UID.replace("/", "%2F"), _auth_header())
    got = json.loads(req[2])
    # The form POST above ran a second check, so compare with the stored row rather
    # than the first response: the two differ if they land in different seconds.
    assert got["live_status"] == "gone" and got["live_checked_at"] == before[1]


def test_blockers_includes_gone_liveness_and_no_chance_verdict_as_hard():
    posting = {"title": "Staff ML Engineer", "location": "Remote, Canada", "remote": True,
               "live_status": "gone", "live_checked_at": "2026-11-24T11:00:00+00:00"}
    analysis = "VERDICT\n\nNo chance at this level.\n\nFIT\n\n- Python\n"
    rows = serve._blockers(posting, "Build Python services.", {"verdict": "in_reach"}, analysis)
    by_kind = {row["kind"]: row for row in rows}
    assert set(by_kind) == {"liveness", "verdict"}
    assert by_kind["liveness"]["severity"] == "hard"
    assert by_kind["verdict"]["severity"] == "hard"
    assert "Posting appears closed" in by_kind["liveness"]["title"]
    assert "no chance" in by_kind["verdict"]["title"]
    assert "No chance at this level." == by_kind["verdict"]["posting"]
    assert serve.apply_gate(rows)["decision"] == "blocked"

    # Only the gone status counts: a live or unknown posting adds no liveness row.
    for status in ("live", "unknown", None):
        open_posting = {**posting, "live_status": status}
        kinds = [r["kind"] for r in serve._blockers(open_posting, "", {}, analysis)]
        assert kinds == ["verdict"]
    # A strong verdict or no analysis text adds nothing.
    strong = "VERDICT\n\nStrong fit, apply.\n"
    assert serve._blockers({**posting, "live_status": None}, "", {}, strong) == []
    assert serve._blockers({**posting, "live_status": None}, "", {}, None) == []


def test_apply_gate_returns_blocked_caution_clear_correctly():
    hard = {"kind": "location", "severity": "hard", "title": "Location is not Winnipeg"}
    soft = {"kind": "verdict", "severity": "soft", "title": "Analysis verdict: stretch"}

    clear = serve.apply_gate([])
    assert clear == {"decision": "clear", "hard_count": 0, "soft_count": 0,
                     "summary": "Nothing blocking."}

    caution = serve.apply_gate([soft])
    assert (caution["decision"], caution["hard_count"], caution["soft_count"]) == (
        "caution", 0, 1)
    assert caution["summary"].startswith("Proceed with caution")

    blocked = serve.apply_gate([hard, soft])
    assert (blocked["decision"], blocked["hard_count"], blocked["soft_count"]) == (
        "blocked", 1, 1)
    assert blocked["summary"].startswith("Do not apply")
    assert serve.apply_gate([hard])["decision"] == "blocked"
    # A row with no severity fails closed.
    assert serve.apply_gate([{"kind": "x", "title": "untagged"}])["decision"] == "blocked"

    # Seniority: out_of_reach is hard, stretch is soft, each through _blockers.
    base = {"title": "ML", "location": "Remote, Canada", "remote": True}
    out_rows = serve._blockers(base, "", {"verdict": "out_of_reach", "why": "Principal"})
    assert [r["severity"] for r in out_rows] == ["hard"]
    assert serve.apply_gate(out_rows)["decision"] == "blocked"
    stretch_rows = serve._blockers(base, "", {"verdict": "stretch"})
    assert [r["severity"] for r in stretch_rows] == ["soft"]
    assert serve.apply_gate(stretch_rows)["decision"] == "caution"


def test_posting_json_exposes_apply_gate(ai_api, tree):
    packet = tree / "packets" / UID_DIR
    packet.mkdir(parents=True, exist_ok=True)
    (packet / "analysis.md").write_text("VERDICT\n\nNo chance at this level.\n", encoding="utf-8")
    status, _, body = _req(ai_api.port, "GET", "/api/postings/" + UID.replace("/", "%2F"),
                           headers=_auth_header())
    assert status == 200
    gate = json.loads(body)["apply_gate"]
    assert gate["decision"] == "blocked" and gate["hard_count"] == 1
    assert gate["summary"].startswith("Do not apply")


def test_action_route_rejects_questions_without_text(ai_api, tree):
    html = {**_auth_header(), **FORM, "Accept": "text/html"}
    long_text = "x" * (serve.QUESTIONS_MAX_CHARS + 1)
    bodies = [b"kind=questions", b"kind=questions&questions=+++",
              b"kind=questions&questions=%0D%0A",
              urlencode({"kind": "questions", "questions": long_text}).encode()]
    for body in bodies:
        status, _, resp = _post(ai_api.port, ACTION_PATH, body, html)
        assert status == 400, body[:40]
        assert "questions" in json.loads(resp)["error"]
    status, _, resp = _post(ai_api.port, ACTION_PATH, json.dumps({"kind": "questions"}).encode(),
                            {**_auth_header(), **JSON_HDR})
    assert status == 400 and "required" in json.loads(resp)["error"]
    assert ai_api.cop.jobs() == []
    qfile = tree / "packets" / serve.packet_dir_name(UID) / "questions.txt"
    assert not qfile.exists()
    # With text: queued, and the text is written to the packet dir before the job runs.
    text = "**Why Acme?**\nBecause of the SOC work.\n"
    body = urlencode({"kind": "questions", "questions": text}).encode()
    status, _, _ = _post(ai_api.port, ACTION_PATH, body, html)
    assert status == 303
    assert qfile.read_text(encoding="utf-8") == text
    job = ai_api.cop.latest(UID)
    assert job["kind"] == "questions" and job["state"] == "queued"
    # The field is ignored for other kinds: no text needed for a letter.
    status, _, _ = _post(ai_api.port, ACTION_PATH, b"kind=letter", html)
    assert status == 303


def _follow_up_of(db, uid):
    store = Store(str(db))
    try:
        return store.get(uid)["follow_up_at"]
    finally:
        store.close()


def test_stats_page_counts_the_funnel_and_variants(api, jobs_db):
    import sqlite3
    store = Store(str(jobs_db))
    store.upsert_postings([
        {"uid": "x:1", "source": "greenhouse", "company": "Beta", "title": "Dev",
         "location": "Remote", "url": "https://example.invalid/x1"},
        {"uid": "x:2", "source": "greenhouse", "company": "Gamma", "title": "Dev",
         "location": "Remote", "url": "https://example.invalid/x2"},
    ])
    store.close()
    db = sqlite3.connect(str(jobs_db))
    db.executemany("UPDATE postings SET status=?, variant=? WHERE uid=?", [
        ("interview", "A", UID), ("new", None, OTHER_UID),
        ("rejected", "A", "x:1"), ("applied", "A", "x:2")])
    db.commit()
    db.close()

    status, headers, body = _req(api, "GET", "/stats", _auth_header())
    assert status == 200 and headers["Content-Type"].startswith("text/html")
    page = body.decode("utf-8")
    assert "<title>Stats</title>" in page
    funnel = page[page.index("Funnel by status"):page.index("Variant performance")]
    assert '<td>interview</td><td class="num">1</td>' in funnel
    assert '<td>rejected</td><td class="num">1</td>' in funnel
    assert '<td>offer</td><td class="num">0</td>' in funnel
    assert "nothing yet" not in funnel
    variants = page[page.index("Variant performance"):page.index("Sessions per kind")]
    # A: 3 postings, 3 reached applied (interview/rejected/applied), 2 responded.
    assert '<td>A</td><td class="num">3</td><td class="num">3</td><td class="num">2</td>' \
        in variants
    assert '<td>none</td><td class="num">1</td><td class="num">0</td><td class="num">0</td>' \
        in variants
    sessions = page[page.index("Sessions per kind"):]
    assert "nothing yet" in sessions  # no copilot jobs, so the session table is empty
    assert page.count("nothing yet") == 1

    for path in ("/stats", "/stats.ics"):
        assert _req(api, "GET", path)[0] == 401
    assert _post(api, "/api/postings/" + UID.replace("/", "%2F") + "/followup", b"",
                 FORM)[0] == 401


def test_stats_ics_has_one_event_per_due_posting(api, jobs_db):
    from jobsearch import serve as serve_mod
    today = date.today()
    store = Store(str(jobs_db))
    store.upsert_postings([{"uid": "x:9", "source": "greenhouse", "company": "Later Co",
                            "title": "Dev", "location": "Remote",
                            "url": "https://example.invalid/x9"}])
    store.set_follow_up(UID, today.isoformat())
    store.set_follow_up(OTHER_UID, (today - timedelta(days=3)).isoformat())
    store.set_follow_up("x:9", (today + timedelta(days=5)).isoformat())
    store.close()

    status, headers, body = _req(api, "GET", "/stats.ics", _auth_header())
    assert status == 200 and headers["Content-Type"].startswith("text/calendar")
    assert body.endswith(b"END:VCALENDAR\r\n")
    assert body.replace(b"\r\n", b"").count(b"\n") == 0  # CRLF only, no bare LF
    lines = body.decode("utf-8").split("\r\n")
    assert lines.count("BEGIN:VEVENT") == 2 and lines.count("END:VEVENT") == 2
    assert "UID:" + UID in lines and "UID:" + OTHER_UID in lines
    assert "UID:x:9" not in lines
    day = today.strftime("%Y%m%d")
    assert f"DTSTART;VALUE=DATE:{day}" in lines
    assert "SUMMARY:Follow up: Acme - Engineer" in lines
    assert "URL:https://example.invalid/job/4711" in lines

    # Escaping and 75-octet folding for the rare cases the sample rows do not hit.
    row = {"uid": "a,b;c", "company": "Co, Inc", "title": "Dev " * 30,
           "url": "https://example.invalid/z", "follow_up_at": "2026-12-01"}
    text = serve_mod._render_followups_ics([row], datetime(2026, 11, 24, tzinfo=timezone.utc))
    assert "UID:a\\,b\\;c" in text and "SUMMARY:Follow up: Co\\, Inc - Dev" in text
    assert all(len(part.encode("utf-8")) <= 75 for part in text.split("\r\n"))
    empty = serve_mod._render_followups_ics([], datetime(2026, 11, 24, tzinfo=timezone.utc))
    assert empty.startswith("BEGIN:VCALENDAR\r\n") and empty.endswith("END:VCALENDAR\r\n")

    assert _req(api, "GET", "/stats.ics")[0] == 401


def test_followup_route_sets_and_clears(api, jobs_db):
    path = "/api/postings/" + UID.replace("/", "%2F") + "/followup"
    today = date.today()
    auth = _auth_header()

    status, _, body = _post(api, path, json.dumps({"days": 7}).encode(),
                            {**auth, **JSON_HDR})
    assert status == 200 and json.loads(body)["ok"] is True
    want = (today + timedelta(days=7)).isoformat()
    assert json.loads(body)["follow_up_at"] == want and _follow_up_of(jobs_db, UID) == want

    status, _, _ = _post(api, path, b"date=2026-12-01", {**auth, **FORM})
    assert status == 200 and _follow_up_of(jobs_db, UID) == "2026-12-01"

    status, headers, _ = _post(api, path, b"date=2026-12-02&return=%2Fposting%2Fx%3Fstep%3Dsubmit",
                               {**auth, **FORM, "Accept": "text/html"})
    assert status == 303 and headers["Location"] == "/posting/x?step=submit"
    assert _follow_up_of(jobs_db, UID) == "2026-12-02"

    status, headers, _ = _post(api, path, b"days=3", {**auth, **FORM, "Accept": "text/html"})
    assert status == 303 and headers["Location"] == POSTING_LOCATION + "?step=submit"
    assert _follow_up_of(jobs_db, UID) == (today + timedelta(days=3)).isoformat()

    status, headers, _ = _post(api, path, b"clear=1&return=%2Fposting%2Fx",
                               {**auth, **FORM, "Accept": "text/html"})
    assert status == 303 and headers["Location"] == "/posting/x"
    assert _follow_up_of(jobs_db, UID) is None
    assert _events(jobs_db, UID)[-1]["kind"] == "followup"
    assert _events(jobs_db, UID)[-1]["detail"] == "cleared"

    for bad in (b"days=0", b"days=121", b"days=abc", b"date=2026-02-30", b"date=12/01/2026",
                b"", b"clear=0"):
        status, _, body = _post(api, path, bad, {**auth, **FORM})
        assert status == 400 and "error" in json.loads(body), bad
    assert _follow_up_of(jobs_db, UID) is None

    status, _, body = _post(api, "/api/postings/nope%3A1/followup", b"days=2",
                            {**auth, **FORM})
    assert status == 404 and json.loads(body)["error"] == "unknown uid"

    status, _, _ = _post(api, path, b"days=2", {**auth, **FORM,
                                                  "Origin": "https://evil.invalid"})
    assert status == 403 and _follow_up_of(jobs_db, UID) is None
    assert _post(api, path, b"days=2", FORM)[0] == 401


# --- preferences consumed by the blockers -----------------------
# Every test passes a prefs dict or a tmp_path file; none reads the operator's real preferences.yaml.

_PREFS_CA_YAML = """\
version: 1
relocation:
  countries: [CA]
  cities: []
  exclude_regions: [{exclude}]
work_authorization:
"""

_PREFS_US_SPONSOR_YAML = """\
version: 1
relocation:
  countries: []
  cities: []
  exclude_regions: []
work_authorization:
  US:
    status: requires_sponsorship
    reason: "Require US visa sponsorship."
"""


def _prefs_file(tmp_path, text, name="preferences.yaml"):
    from jobsearch.match import load_preferences
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return load_preferences(str(p))


def _toronto(**extra):
    return {"title": "ML Engineer", "location": "Toronto, ON", "description": "Build Python.",
            **extra}


def test_toronto_still_hard_blocks_with_no_preferences_file(tmp_path):
    from jobsearch.match import load_preferences
    prefs = load_preferences(str(tmp_path / "absent.yaml"))
    row = serve._location_blocker(_toronto(), prefs)
    assert row == {"kind": "location", "title": "Location is not Winnipeg",
                   "posting": "Toronto, ON", "facts": "facts.yaml lists Winnipeg, MB"}
    assert serve._location_blocker(_toronto(), prefs=None) == row
    rows = serve._blockers(_toronto(), "Build Python.", {})
    assert [(r["kind"], r["severity"], r["title"]) for r in rows] == [
        ("location", "hard", "Location is not Winnipeg")]


def test_toronto_clears_when_ca_is_listed_in_preferences(tmp_path):
    prefs = _prefs_file(tmp_path, _PREFS_CA_YAML.format(exclude=""))
    assert prefs["countries"] == ["CA"]
    assert serve._location_blocker(_toronto(), prefs) is None
    assert serve._blockers(_toronto(), "Build Python.", {}, None, prefs) == []
    assert serve._location_blocker(_toronto(), {"countries": ["CA"], "exclude_regions": []}) is None


def test_excluded_province_keeps_the_location_blocker_and_names_region(tmp_path):
    prefs = _prefs_file(tmp_path, _PREFS_CA_YAML.format(exclude="ON"))
    row = serve._location_blocker(_toronto(), prefs)
    assert row["kind"] == "location" and row["title"] == "Location is not Winnipeg"
    assert "excludes ON" in row["facts"]
    rows = serve._blockers(_toronto(), "Build Python.", {}, None, prefs)
    assert [r["severity"] for r in rows] == ["hard"]
    assert serve.apply_gate(rows)["decision"] == "blocked"


def test_winnipeg_and_remote_postings_unchanged_by_preferences():
    prefs = {"countries": ["CA"], "exclude_regions": ["MB"]}
    assert serve._location_blocker({"location": "Winnipeg, MB"}, prefs) is None
    assert serve._location_blocker({"location": "Remote, Canada"}, prefs) is None
    assert serve._location_blocker({"location": ""}, prefs) is None


def test_us_posting_declining_sponsorship_gets_hard_sponsorship_row():
    prefs = {"work_authorization": {"US": {"status": "requires_sponsorship"}}}
    posting = {"title": "Backend Engineer", "location": "Seattle, WA",
               "description": "Great team. We will not sponsor visas for this role."}
    rows = serve._blockers(posting, "", {}, None, prefs)
    sponsor = [r for r in rows if r["kind"] == "sponsorship"]
    assert sponsor == [{"kind": "sponsorship", "severity": "hard",
                        "title": "US visa sponsorship not offered",
                        "posting": "will not sponsor",
                        "facts": "preferences.yaml records US work authorization as "
                                 "requires_sponsorship"}]
    assert serve.apply_gate(rows)["decision"] == "blocked"
    assert rows[-1] is sponsor[0]


def test_us_posting_silent_on_sponsorship_gets_soft_row():
    prefs = {"work_authorization": {"US": {"status": "requires_sponsorship"}}}
    posting = {"title": "Backend Engineer", "location": "Seattle, WA",
               "description": "Build Python services with a friendly team."}
    rows = serve._blockers(posting, "", {}, None, prefs)
    sponsor = [r for r in rows if r["kind"] == "sponsorship"]
    assert [(r["severity"], r["title"]) for r in sponsor] == [
        ("soft", "US role: confirm visa sponsorship")]
    assert [r["severity"] for r in rows if r["kind"] == "sponsorship"] == ["soft"]


def test_us_posting_with_country_authorized_gets_no_sponsorship_row(tmp_path):
    authorized = {"work_authorization": {"US": {"status": "authorized"}}}
    declining = {"title": "Backend Engineer", "location": "Seattle, WA",
                 "description": "We will not sponsor visas."}
    assert [r for r in serve._blockers(declining, "", {}, None, authorized)
            if r["kind"] == "sponsorship"] == []
    assert [r for r in serve._blockers(declining, "", {}, None, {})
            if r["kind"] == "sponsorship"] == []
    from_file = _prefs_file(tmp_path, _PREFS_US_SPONSOR_YAML)
    rows = serve._blockers(declining, "", {}, None, from_file)
    assert [r["severity"] for r in rows if r["kind"] == "sponsorship"] == ["hard"]


def test_sponsorship_kind_has_a_visa_tag():
    from jobsearch import workspace
    assert workspace._BLOCKER_TAGS["sponsorship"] == ("Visa", "tag-gap")


def test_triage_check_lines_show_relocation_open_only_when_ca_listed():
    from jobsearch import triage
    base = {"parts": {"eligible": True}, "posting": {"location": "Toronto, ON"},
            "french_quote": None, "french_text": False, "seniority": None, "reach": None}
    plain = triage._check_lines({**base, "prefs": {"countries": []}})
    assert "Relocation open (CA)" not in plain
    assert "Relocation open" not in triage._check_lines(base)
    with_ca = triage._check_lines({**base, "prefs": {"countries": ["CA"]}})
    assert "Relocation open (CA)" in with_ca
    # The line sits after the gate row and nothing else moves.
    assert with_ca.index("Location passes the gate") < with_ca.index("Relocation open (CA)")
    assert with_ca.replace(
        with_ca[with_ca.index('<div class="check ok"><span class="mk">&#10003;</span><span><strong>Relocation'):
                with_ca.index("</div>", with_ca.index("Relocation open")) + len("</div>")], "") == plain


# ---------- /facts page and its POST routes ----------

FACTS_ORIG = "version: 1\nname: Test Person\n"
FACTS_NEW = "version: 1\nname: Test Person\nlocation: Toronto\n"


@pytest.fixture
def facts_api(jobs_db, tmp_path, monkeypatch):
    """Live server with an unstarted Copilot whose facts and prefs live only under tmp_path."""
    monkeypatch.setenv("JOBSEARCH_SERVE_USER", USER)
    monkeypatch.setenv("JOBSEARCH_SERVE_PASSWORD", PASSWORD)
    resume = tmp_path / "resume"
    resume.mkdir()
    facts = resume / "facts.yaml"
    facts.write_text(FACTS_ORIG, encoding="utf-8")
    prefs = resume / "preferences.yaml"  # deliberately absent: the page must say so
    cop = Copilot(jobs_path=tmp_path / "ai" / "jobs.json", packet_root=tmp_path / "packets",
                  facts_path=facts, prefs_path=prefs, db_path=str(jobs_db),
                  neon_cmd=str(tmp_path / "no-such-neon"), state_dir=tmp_path / "ai" / "state",
                  bodies_dir=tmp_path / "bodies")
    srv = serve.make_server("127.0.0.1", 0, USER, PASSWORD, db_path=str(jobs_db), copilot=cop)
    thread = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    try:
        yield types.SimpleNamespace(port=srv.server_address[1], cop=cop, facts=facts, prefs=prefs)
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=5)


def _post_facts(port, path, fields=None, headers=None, auth=True):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        hdrs = {"Content-Type": "application/x-www-form-urlencoded", **(_auth_header() if auth else {}),
                **(headers or {})}
        conn.request("POST", path, body=urlencode(fields or {}), headers=hdrs)
        resp = conn.getresponse()
        return resp.status, dict(resp.getheaders()), resp.read().decode("utf-8", "replace")
    finally:
        conn.close()


def _same_origin(port):
    return {"Origin": f"http://127.0.0.1:{port}"}


def _facts_jobs(cop):
    return [j for j in cop.jobs() if j["kind"] == copilot_mod.FACTS_KIND]


def _get_facts(port):
    status, _, body = _req(port, "GET", "/facts", _auth_header())
    return status, body.decode("utf-8")


def _add_pending(cop, facts_path, *, jid="pend00000001", target="facts.yaml", base=None,
                 new_text=FACTS_NEW, diff="--- a\n+++ b\n+location: Toronto\n"):
    """A pending_review facts_update job, written straight into the copilot's job list."""
    base = base or copilot_mod._file_sha(facts_path)
    job = {"id": jid, "uid": copilot_mod.FACTS_UID, "kind": copilot_mod.FACTS_KIND,
           "state": "pending_review", "created": _ago(5), "started": _ago(4), "finished": None,
           "error": "", "answer_path": "", "artifact_path": "", "blocker": "", "message": "",
           "est_seconds": 60, "stage": "", "stage_secs": 0.0, "stages": [],
           "instruction": "I'm open to relocation in Canada.", "confirm_protected": False,
           "target": target, "base_sha256": base, "new_text": new_text,
           "summary": "Add Toronto as a location.", "diff": diff, "question": "",
           "reason": "", "proposal_state": "proposed", "commit_sha": ""}
    with cop._lock:
        cop._jobs.append(job)
        cop._save()
    return jid


def test_get_facts_is_server_rendered_with_textarea(facts_api):
    status, body = _get_facts(facts_api.port)
    assert status == 200
    assert '<textarea' in body and 'name="instruction"' in body
    assert 'action="/api/facts/propose"' in body
    assert 'name="return" value="/facts"' in body
    assert "does not exist yet" in body  # preferences.yaml is absent in this fixture
    assert "Current change" in body and "History" in body


def test_get_facts_shows_both_files_escaped(facts_api):
    facts_api.prefs.write_text("version: 1\nnote: <b>x</b>\n", encoding="utf-8")
    _, body = _get_facts(facts_api.port)
    assert "note: &lt;b&gt;x&lt;/b&gt;" in body
    assert "name: Test Person" in body


def test_get_facts_without_copilot_renders_and_writes_nothing(server):
    status, _, body = _req(server, "GET", "/facts", _auth_header())
    assert status == 200
    assert "not running" in body.decode("utf-8")


def test_facts_routes_require_auth(facts_api):
    assert _req(facts_api.port, "GET", "/facts")[0] == 401
    assert _post_facts(facts_api.port, "/api/facts/propose", {"instruction": "x"}, auth=False)[0] == 401
    assert _facts_jobs(facts_api.cop) == []


def test_propose_cross_origin_is_403_and_creates_no_job(facts_api):
    status, _, _ = _post_facts(facts_api.port, "/api/facts/propose", {"instruction": "Move to Ottawa."},
                               headers={"Origin": "http://evil.example"})
    assert status == 403
    assert _facts_jobs(facts_api.cop) == []


def test_accept_and_reject_cross_origin_are_403_and_leave_file_alone(facts_api):
    jid = _add_pending(facts_api.cop, facts_api.facts)
    before = facts_api.facts.read_bytes()
    for action in ("accept", "reject"):
        status, _, _ = _post_facts(facts_api.port, f"/api/facts/{jid}/{action}",
                                   {"seen_sha256": copilot_mod._file_sha(facts_api.facts)},
                                   headers={"Origin": "http://evil.example"})
        assert status == 403
    assert facts_api.facts.read_bytes() == before
    assert facts_api.cop.job(jid)["state"] == "pending_review"


def test_propose_valid_303_and_one_queued_job_even_when_repeated(facts_api):
    status, headers, _ = _post_facts(facts_api.port, "/api/facts/propose",
                                     {"instruction": "I'm open to relocation in Canada.",
                                      "return": "/facts"}, headers=_same_origin(facts_api.port))
    assert status == 303
    assert headers["Location"] == "/facts"
    jobs = _facts_jobs(facts_api.cop)
    assert len(jobs) == 1 and jobs[0]["state"] == "queued"
    assert jobs[0]["uid"] == copilot_mod.FACTS_UID
    status, headers, _ = _post_facts(facts_api.port, "/api/facts/propose",
                                     {"instruction": "I'm open to relocation in Canada."},
                                     headers=_same_origin(facts_api.port))
    assert status == 303 and headers["Location"] == "/facts"
    assert len(_facts_jobs(facts_api.cop)) == 1


def test_propose_rejects_empty_and_overlong_instructions(facts_api):
    origin = _same_origin(facts_api.port)
    status, _, body = _post_facts(facts_api.port, "/api/facts/propose", {"instruction": "   "}, headers=origin)
    assert status == 400 and "sentence" in body
    status, _, body = _post_facts(facts_api.port, "/api/facts/propose",
                                  {"instruction": "x" * (serve.FACTS_INSTRUCTION_LIMIT + 1)}, headers=origin)
    assert status == 400 and "characters" in body
    assert _facts_jobs(facts_api.cop) == []


def test_propose_over_copilot_cap_is_400_not_500(facts_api):
    # The route and copilot.submit share one 1000-character cap. Surface the refusal as 400.
    status, _, body = _post_facts(facts_api.port, "/api/facts/propose", {"instruction": "y" * 1500},
                                  headers=_same_origin(facts_api.port))
    assert status == 400
    assert "1000" in body
    assert _facts_jobs(facts_api.cop) == []


def test_queued_job_page_has_noscript_refresh_and_disabled_form(facts_api):
    _post_facts(facts_api.port, "/api/facts/propose", {"instruction": "Need US visa sponsorship."},
                headers=_same_origin(facts_api.port))
    _, body = _get_facts(facts_api.port)
    assert serve.NOSCRIPT_REFRESH in body
    assert "Working..." in body
    assert "disabled" in body
    assert '<a href="/facts">Refresh now</a>' in body


def test_settled_job_page_has_no_refresh(facts_api):
    jid = _add_pending(facts_api.cop, facts_api.facts)
    with facts_api.cop._lock:
        facts_api.cop._find(jid).update(state="done", proposal_state="rejected", reason="off-topic")
    _, body = _get_facts(facts_api.port)
    assert "http-equiv=\"refresh\"" not in body
    assert serve.NOSCRIPT_REFRESH not in body
    assert "off-topic" in body


def test_pending_review_page_shows_summary_target_escaped_diff_and_accept_reject(facts_api):
    jid = _add_pending(facts_api.cop, facts_api.facts, diff="+ <script>alert(1)</script>")
    _, body = _get_facts(facts_api.port)
    assert "Add Toronto as a location." in body
    assert "<pre" in body and "&lt;script&gt;alert(1)&lt;/script&gt;" in body
    assert "<script>alert(1)" not in body
    assert f'action="/api/facts/{jid}/accept"' in body
    assert f'action="/api/facts/{jid}/reject"' in body
    assert f'name="seen_sha256" value="{copilot_mod._file_sha(facts_api.facts)}"' in body
    assert body.count("facts.yaml") >= 3  # target shown next to both buttons


def test_accept_with_wrong_sha_is_409_and_file_byte_identical(facts_api):
    jid = _add_pending(facts_api.cop, facts_api.facts)
    before = facts_api.facts.read_bytes()
    status, _, body = _post_facts(facts_api.port, f"/api/facts/{jid}/accept",
                                  {"seen_sha256": "0" * 64}, headers=_same_origin(facts_api.port))
    assert status == 409
    assert "propose the change again" in body.lower()
    assert "Traceback" not in body
    assert facts_api.facts.read_bytes() == before
    assert facts_api.cop.job(jid)["state"] == "pending_review"


def test_accept_after_file_changed_on_disk_is_409_and_writes_nothing(facts_api):
    jid = _add_pending(facts_api.cop, facts_api.facts)
    seen = copilot_mod._file_sha(facts_api.facts)
    facts_api.facts.write_text("version: 1\nname: Someone Else\n", encoding="utf-8")
    before = facts_api.facts.read_bytes()
    status, _, _ = _post_facts(facts_api.port, f"/api/facts/{jid}/accept", {"seen_sha256": seen},
                               headers=_same_origin(facts_api.port))
    assert status == 409
    assert facts_api.facts.read_bytes() == before


def test_accept_with_right_sha_writes_and_303s(facts_api, monkeypatch):
    jid = _add_pending(facts_api.cop, facts_api.facts)
    # The real verify and git-commit steps run scripts and git in the resume dir; stub them out.
    monkeypatch.setattr(facts_api.cop, "_verify_written", lambda target, resume: "")
    monkeypatch.setattr(facts_api.cop, "_commit_written",
                        lambda target, resume, instruction: ("abc1234def5678", ""))
    seen = copilot_mod._file_sha(facts_api.facts)
    status, headers, _ = _post_facts(facts_api.port, f"/api/facts/{jid}/accept", {"seen_sha256": seen},
                                     headers=_same_origin(facts_api.port))
    assert status == 303 and headers["Location"] == "/facts"
    assert facts_api.facts.read_text(encoding="utf-8") == FACTS_NEW
    assert facts_api.cop.job(jid)["state"] == "applied"
    _, body = _get_facts(facts_api.port)
    assert "abc1234" in body  # history and current-change show the short sha


def test_applied_job_short_sha_in_history(facts_api):
    jid = _add_pending(facts_api.cop, facts_api.facts)
    with facts_api.cop._lock:
        facts_api.cop._find(jid).update(state="applied", proposal_state="accepted",
                                        commit_sha="0123456789abcdef")
    _, body = _get_facts(facts_api.port)
    assert "0123456" in body
    assert "0123456789abcdef" not in body  # only the short sha is shown


def test_reject_303s_and_writes_nothing(facts_api):
    jid = _add_pending(facts_api.cop, facts_api.facts)
    before = facts_api.facts.read_bytes()
    status, headers, _ = _post_facts(facts_api.port, f"/api/facts/{jid}/reject", {},
                                     headers=_same_origin(facts_api.port))
    assert status == 303 and headers["Location"] == "/facts"
    assert facts_api.cop.job(jid)["state"] == "rejected"
    assert facts_api.facts.read_bytes() == before
    assert not facts_api.prefs.exists()


def test_accept_and_reject_unknown_job_are_404(facts_api):
    origin = _same_origin(facts_api.port)
    assert _post_facts(facts_api.port, "/api/facts/nope/accept", {"seen_sha256": "x"}, headers=origin)[0] == 404
    assert _post_facts(facts_api.port, "/api/facts/nope/reject", {}, headers=origin)[0] == 404


def test_reject_of_settled_job_is_409_not_500(facts_api):
    jid = _add_pending(facts_api.cop, facts_api.facts)
    _post_facts(facts_api.port, f"/api/facts/{jid}/reject", {}, headers=_same_origin(facts_api.port))
    status, _, _ = _post_facts(facts_api.port, f"/api/facts/{jid}/reject", {},
                               headers=_same_origin(facts_api.port))
    assert status == 409


def test_unknown_facts_subpath_is_404(facts_api):
    status, _, _ = _post_facts(facts_api.port, "/api/facts/a/b/c", {}, headers=_same_origin(facts_api.port))
    assert status == 404
