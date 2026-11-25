"""Offline tests for jobsearch.registry and jobsearch.cli (no network, no real DB, no real build)."""

from __future__ import annotations

import json
import os
import sys
import types
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from jobsearch import cli
from jobsearch.registry import load_registry, save_registry, verify_registry

REPO = Path(__file__).resolve().parents[1]
REAL_REGISTRY = REPO / "companies.yaml"


# ---------- isolation: every test writes only under tmp_path ----------

def _install_module(monkeypatch, name: str, **attrs) -> None:
    """Put a stand-in module in sys.modules so lazy `from jobsearch.x import y` resolves to it,
    whether or not the real sibling file exists yet."""
    mod = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(mod, key, value)
    monkeypatch.setitem(sys.modules, name, mod)


@pytest.fixture(autouse=True)
def iso(monkeypatch, tmp_path):
    monkeypatch.setenv("JOBS_DB", str(tmp_path / "jobs.db"))
    monkeypatch.setattr(cli, "LAST_SCAN_PATH", tmp_path / "last_scan.json")
    monkeypatch.setattr(cli, "BOARD_HEALTH_PATH", tmp_path / "board_health.json")
    monkeypatch.setattr(cli, "DASHBOARD_PATH", tmp_path / "dashboard.html")
    monkeypatch.setattr(cli, "PACKETS_DIR", tmp_path / "packets")
    monkeypatch.setattr(cli, "RESUME_OUT", tmp_path / "out")

    dash_calls: list[dict] = []
    letter_calls: list[dict] = []

    def render_dashboard(db_path, out_path, last_scan=None, top_n=40):
        dash_calls.append({"db_path": db_path, "out_path": out_path,
                           "last_scan": last_scan, "top_n": top_n})
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        Path(out_path).write_text("<html></html>", encoding="utf-8")
        return out_path

    def render_letter(uid, body_path, out_dir=None, **kwargs):
        letter_calls.append({"uid": uid, "body_path": body_path, "out_dir": out_dir})
        base = Path(out_dir) if out_dir else tmp_path / "letters"
        base.mkdir(parents=True, exist_ok=True)
        pdf = base / "cover_letter.pdf"
        pdf.write_bytes(b"%PDF-1.4 letter")
        return str(pdf)

    _install_module(monkeypatch, "jobsearch.dashboard", render_dashboard=render_dashboard)
    _install_module(monkeypatch, "jobsearch.letter", render_letter=render_letter)
    return SimpleNamespace(dash=dash_calls, letter=letter_calls, tmp=tmp_path)


def _set_module_attr(monkeypatch, name: str, attr: str, value) -> None:
    monkeypatch.setattr(sys.modules[name], attr, value)


# ---------- registry ----------

def test_example_registry_loads():
    entries = load_registry(str(REAL_REGISTRY))
    assert len(entries) >= 5
    for e in entries:
        assert e["name"] and e["token"]
        assert e["ats"] in {"greenhouse", "lever", "ashby"}
        assert isinstance(e["tags"], list)
        assert isinstance(e["verified"], bool)


def test_registry_save_roundtrip(tmp_path):
    entries = load_registry(str(REAL_REGISTRY))
    dest = tmp_path / "companies.yaml"
    save_registry(entries, str(dest))
    assert load_registry(str(dest)) == entries


def test_verify_registry_reports_failures(monkeypatch):
    import jobsearch.sources as sources

    def fake_fetch_board(entry):
        if entry["token"] == "dead":
            raise sources.FetchError("greenhouse:dead: HTTP 404")
        return [] if entry["token"] == "empty" else [{"uid": "x"}, {"uid": "y"}]

    monkeypatch.setattr(sources, "fetch_board", fake_fetch_board)
    entries = [
        {"name": "A", "ats": "greenhouse", "token": "live", "tags": [], "verified": True},
        {"name": "B", "ats": "lever", "token": "dead", "tags": [], "verified": True},
        {"name": "C", "ats": "ashby", "token": "empty", "tags": [], "verified": True},
    ]
    res = {r["name"]: r for r in verify_registry(entries)}
    assert res["A"]["ok"] and res["A"]["count"] == 2
    assert not res["B"]["ok"] and "HTTP 404" in res["B"]["detail"]
    assert not res["C"]["ok"] and res["C"]["count"] == 0


# ---------- fakes ----------

ROWS = [
    # uid, status, score, company, title
    ("greenhouse:a:1", "new", 55.0, "Alpha", "Detection Engineer"),
    ("greenhouse:b:2", "new", 30.0, "Beta", "SOC Analyst"),          # below min-score
    ("lever:c:3", "shortlisted", 90.0, "Gamma", "AI Security Eng"),  # not new
    ("ashby:d:4", "new", 72.0, "Delta", "LLM Security Engineer"),
    ("ashby:e:5", "new", 41.0, "Epsilon", "Threat Detection"),
]


def _fake_store(monkeypatch, rows=ROWS, upsert=(2, 3)):
    class FakeStore:
        def __init__(self):
            self.calls = []
            self.rows = {
                uid: {"uid": uid, "status": st, "score": sc, "company": co, "title": ti,
                      "location": "Remote", "url": f"https://x/{uid}", "variant": "ai-security",
                      "reasons": ["r1"], "description": f"desc for {uid}", "source": "greenhouse"}
                for uid, st, sc, co, ti in rows
            }

        def upsert_postings(self, postings):
            self.calls.append(("upsert", len(postings)))
            return upsert

        def backfill_dedupe_keys(self):
            self.calls.append(("backfill",))
            return 0

        def set_score(self, uid, score, variant, reasons):
            self.calls.append(("set_score", uid))
            self.rows[uid].update(score=score, variant=variant, reasons=reasons)

        def list(self, status=None, min_score=None, query=None, limit=50, exclude_status=()):
            # Deliberately ignores filters: the CLI must filter on its own.
            self.calls.append(("list", status, min_score, limit))
            return list(self.rows.values())

        def get(self, uid):
            return self.rows.get(uid)

        def set_status(self, uid, status, note=""):
            self.calls.append(("set_status", uid, status, note))
            if status not in cli.STATUSES:
                raise ValueError(f"bad status {status}")
            self.rows[uid]["status"] = status

        def events(self, uid):
            return []

        def stats(self):
            return {"by_status": {"new": 4, "shortlisted": 1}, "total": 5, "new_24h": 2}

        def close(self):
            self.calls.append(("close",))

    store = FakeStore()
    monkeypatch.setattr(cli, "Store", lambda *a, **k: store)
    return store


def _fake_scan_inputs(monkeypatch, errors=("greenhouse:cloudflare: HTTP 404",)):
    postings = [{"uid": uid, "source": "greenhouse", "company": co, "title": ti,
                 "location": "Remote", "url": "", "description": "", "posted_at": "",
                 "remote": True} for uid, _st, _sc, co, ti in ROWS]
    scores = {uid: sc for uid, _st, sc, _co, _ti in ROWS}

    monkeypatch.setattr(cli, "load_registry", lambda *a, **k: [])
    monkeypatch.setattr(cli, "load_profile", lambda *a, **k: {})
    monkeypatch.setattr(cli, "fetch_all", lambda reg, include_aggregators=True: (postings, list(errors)))
    monkeypatch.setattr(cli, "is_eligible", lambda p, **kw: (True, ""))
    monkeypatch.setattr(cli, "score_posting", lambda p, prof, **kw: (scores[p["uid"]], "ai-security", ["why"]))


# ---------- scan ----------

def test_scan_ranks_only_new_rows_above_min_score(monkeypatch, capsys):
    _fake_scan_inputs(monkeypatch)
    store = _fake_store(monkeypatch)
    rc = cli.main(["scan", "--min-score", "40"])
    out = capsys.readouterr().out
    assert rc == 0
    # Only new rows >= 40, score-sorted desc: Delta 72, Alpha 55, Epsilon 41.
    assert "Beta" not in out and "Gamma" not in out
    i_d, i_a, i_e = out.index("Delta"), out.index("Alpha"), out.index("Epsilon")
    assert i_d < i_a < i_e
    assert "fetched 5 postings, 2 new, 3 already seen" in out
    assert "sources with problems:" in out
    assert "greenhouse:cloudflare: HTTP 404" in out
    assert ("upsert", 5) in store.calls
    assert ("close",) in store.calls


def test_scan_json_output(monkeypatch, capsys):
    _fake_scan_inputs(monkeypatch, errors=())
    _fake_store(monkeypatch)
    assert cli.main(["scan", "--json", "--min-score", "40"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert [r["uid"] for r in rows] == ["ashby:d:4", "greenhouse:a:1", "ashby:e:5"]


# ---------- bad usage -> exit 2 ----------

@pytest.mark.parametrize("argv", [
    [],
    ["nope"],
    ["scan", "--limit", "0"],
    ["scan", "--min-score", "high"],
    ["show"],
    ["status", "only-uid"],
])
def test_bad_usage_exits_2(argv):
    with pytest.raises(SystemExit) as exc:
        cli.main(argv)
    assert exc.value.code == 2


def test_help_lists_all_ten_subcommands(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    for name in ["scan", "list", "show", "status", "stats", "sources", "prep",
                 "rescore", "letter", "dashboard"]:
        assert name in out


# ---------- status ----------

def test_unknown_status_exits_1_with_stderr(monkeypatch, capsys):
    store = _fake_store(monkeypatch)
    rc = cli.main(["status", "greenhouse:a:1", "bogus"])
    captured = capsys.readouterr()
    assert rc == 1
    assert "unknown status" in captured.err
    assert captured.out == ""
    assert not [c for c in store.calls if c[0] == "set_status"]


def test_status_sets_and_reports(monkeypatch, capsys):
    store = _fake_store(monkeypatch)
    assert cli.main(["status", "greenhouse:a:1", "skipped", "--note", "too senior"]) == 0
    assert ("set_status", "greenhouse:a:1", "skipped", "too senior") in store.calls
    assert "new -> skipped" in capsys.readouterr().out


# ---------- prep ----------

def _prep_env(monkeypatch, tmp_path):
    packets = tmp_path / "packets"
    out = tmp_path / "out"
    out.mkdir()
    monkeypatch.setattr(cli, "PACKETS_DIR", packets)
    monkeypatch.setattr(cli, "RESUME_OUT", out)
    return packets, out


def test_prep_build_failure_touches_nothing(monkeypatch, tmp_path, capsys):
    packets, _out = _prep_env(monkeypatch, tmp_path)
    store = _fake_store(monkeypatch)
    builds = []

    def failing_build(variant):
        builds.append(variant)
        return 1, "", f"BUILD FAILED: {variant} boom"

    monkeypatch.setattr(cli, "_run_build", failing_build)
    rc = cli.main(["prep", "ashby:d:4"])
    captured = capsys.readouterr()
    assert rc == 1
    assert builds == ["ai-security", "general"]          # fallback attempted
    assert "BUILD FAILED: general boom" in captured.err
    assert not packets.exists()                           # nothing copied or written
    assert not [c for c in store.calls if c[0] == "set_status"]
    assert store.rows["ashby:d:4"]["status"] == "new"    # status unchanged


def test_prep_success_stages_packet(monkeypatch, tmp_path, capsys):
    packets, out = _prep_env(monkeypatch, tmp_path)
    store = _fake_store(monkeypatch)
    pdf = out / cli.PDF_NAMES["ai-security"]
    pdf.write_bytes(b"%PDF-1.4 fake")
    monkeypatch.setattr(cli, "_run_build", lambda v: (0, f"built {v}", ""))

    rc = cli.main(["prep", "ashby:d:4"])
    assert rc == 0
    slug = packets / "ashby_d_4"
    assert (slug / "resume.pdf").read_bytes() == b"%PDF-1.4 fake"
    md = (slug / "posting.md").read_text(encoding="utf-8")
    assert "LLM Security Engineer" in md and "desc for ashby:d:4" in md
    assert ("set_status", "ashby:d:4", "prepped", "packet staged (ai-security resume)") in store.calls
    assert str(slug) in capsys.readouterr().out


def test_prep_unknown_uid_exits_1(monkeypatch, tmp_path):
    _prep_env(monkeypatch, tmp_path)
    _fake_store(monkeypatch)
    assert cli.main(["prep", "nope:nope:0"]) == 1


def _set_variant(store, uid, variant):
    store.rows[uid]["variant"] = variant


def test_prep_ml_engineer_uses_ai_platform_build(monkeypatch, tmp_path, capsys):
    packets, out = _prep_env(monkeypatch, tmp_path)
    store = _fake_store(monkeypatch)
    _set_variant(store, "ashby:d:4", "ml-engineer")
    (out / cli.PDF_NAMES["ai-platform"]).write_bytes(b"%PDF-1.4 ap")
    builds = []

    def build(variant):
        builds.append(variant)
        return (0, "", "") if variant == "ai-platform" else (1, "", "boom")

    monkeypatch.setattr(cli, "_run_build", build)
    rc = cli.main(["prep", "ashby:d:4"])
    captured = capsys.readouterr()
    assert rc == 0
    assert builds == ["ai-platform"]                      # no build.sh target for ml-engineer
    assert (packets / "ashby_d_4" / "resume.pdf").read_bytes() == b"%PDF-1.4 ap"
    assert ("set_status", "ashby:d:4", "prepped", "packet staged (ai-platform resume)") in store.calls
    assert "no ml-engineer build; used ai-platform" in captured.out


def test_prep_sec_automation_falls_back_to_sec_content(monkeypatch, tmp_path, capsys):
    packets, out = _prep_env(monkeypatch, tmp_path)
    store = _fake_store(monkeypatch)
    _set_variant(store, "ashby:d:4", "sec-automation")
    (out / cli.PDF_NAMES["sec-content"]).write_bytes(b"%PDF-1.4 sc")
    builds = []

    def build(variant):
        builds.append(variant)
        return (0, "", "") if variant == "sec-content" else (1, "", "boom")

    monkeypatch.setattr(cli, "_run_build", build)
    rc = cli.main(["prep", "ashby:d:4"])
    assert rc == 0
    assert builds == ["sec-content"]
    assert ("set_status", "ashby:d:4", "prepped", "packet staged (sec-content resume)") in store.calls


def test_prep_all_rungs_fail_exits_1_and_leaves_status(monkeypatch, tmp_path, capsys):
    packets, _out = _prep_env(monkeypatch, tmp_path)
    store = _fake_store(monkeypatch)
    _set_variant(store, "ashby:d:4", "ml-engineer")
    builds = []

    def build(variant):
        builds.append(variant)
        return 1, "", f"BUILD FAILED: {variant} boom"

    monkeypatch.setattr(cli, "_run_build", build)
    rc = cli.main(["prep", "ashby:d:4"])
    captured = capsys.readouterr()
    assert rc == 1
    assert builds == ["ai-platform", "general"]
    assert "BUILD FAILED: general boom" in captured.err
    assert not packets.exists()
    assert not [c for c in store.calls if c[0] == "set_status"]
    assert store.rows["ashby:d:4"]["status"] == "new"


def test_scan_min_score_default_is_15(capsys):
    parser = cli.build_parser()
    args = parser.parse_args(["scan"])
    assert args.min_score == 15.0
    assert args.limit == 40


def test_scan_help_shows_15_default(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["scan", "--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "--min-score" in out
    assert "default: 15.0" in out
    assert "default: 25" not in out


# ---------- stats / list smoke ----------

def test_stats_prints_counts(monkeypatch, capsys):
    _fake_store(monkeypatch)
    assert cli.main(["stats"]) == 0
    out = capsys.readouterr().out
    assert "total:" in out and "5" in out and "new" in out


def test_list_rejects_unknown_status(monkeypatch, capsys):
    _fake_store(monkeypatch)
    assert cli.main(["list", "--status", "bogus"]) == 1
    assert "unknown status" in capsys.readouterr().err


def _table_rows(out: str) -> list[list[str]]:
    lines = [ln for ln in out.splitlines() if ln.strip()]
    return [[cell.strip() for cell in ln.split(" | ")] for ln in lines[2:]]


def test_list_json_outputs_valid_json_with_same_rows_as_table(monkeypatch, capsys):
    _fake_store(monkeypatch)
    assert cli.main(["list"]) == 0
    table = _table_rows(capsys.readouterr().out)
    assert cli.main(["list", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert isinstance(rows, list) and len(rows) == len(table) == len(ROWS)
    assert [r["uid"] for r in rows] == [t[5] for t in table]
    for r, t in zip(rows, table):
        assert f"{float(r['score']):.1f}" == t[0]
        assert (r["status"], r["company"], r["title"], r["location"]) == (t[1], t[2], t[3], t[4])


def test_list_json_respects_status_filter_args(monkeypatch, capsys):
    store = _fake_store(monkeypatch)
    assert cli.main(["list", "--json", "--status", "new", "--min-score", "40", "--limit", "7"]) == 0
    assert ("list", "new", 40.0, 7) in store.calls
    json.loads(capsys.readouterr().out)


def test_list_json_empty_prints_empty_array(monkeypatch, capsys):
    _fake_store(monkeypatch, rows=[])
    assert cli.main(["list", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == []


def test_list_help_mentions_json(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["list", "--help"])
    assert exc.value.code == 0
    assert "--json" in capsys.readouterr().out


# ---------- rescore ----------

RESCORE_PROFILE = {"tokens": {"python", "sigma"}, "skills": {"python"}, "titles": set()}


def _posting(uid, title, location, description="Build detection content in Python."):
    return {"uid": uid, "source": "greenhouse", "company": "Acme", "title": title,
            "location": location, "url": f"https://x/{uid}", "description": description,
            "posted_at": "", "remote": False}


def _seed_rescore_db(monkeypatch, tmp_path):
    """Real Store on a tmp DB via JOBS_DB; fixed profile so the scorer is deterministic."""
    db = tmp_path / "jobs.db"
    monkeypatch.setenv("JOBS_DB", str(db))
    monkeypatch.setattr(cli, "load_profile", lambda *a, **k: RESCORE_PROFILE)
    from jobsearch.store import Store
    store = Store(str(db))
    stale = _posting("greenhouse:sf:1", "Detection Engineer", "San Francisco, CA")
    paris = _posting("greenhouse:paris:2", "Detection Engineer", "Paris, France")
    good = _posting("greenhouse:to:3", "Detection Engineer", "Toronto, ON")
    store.upsert_postings([stale, paris, good])
    store.set_score(stale["uid"], 35.5, "ai-security", ["old rule"])
    store.set_score(paris["uid"], 37.0, "ai-security", ["old rule"])
    expected_good = cli.score_posting(good, RESCORE_PROFILE)
    store.set_score(good["uid"], *expected_good)
    store.set_status("greenhouse:to:3", "shortlisted", note="keep me")
    store.set_status("greenhouse:sf:1", "skipped", note="not in canada")
    store.close()
    return db


def _raw(db):
    import sqlite3
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute("SELECT * FROM postings ORDER BY uid").fetchall()]
    conn.close()
    return rows


def _get(db, uid):
    return next(r for r in _raw(db) if r["uid"] == uid)


def test_rescore_zeroes_stale_ineligible_rows(monkeypatch, tmp_path, capsys):
    db = _seed_rescore_db(monkeypatch, tmp_path)
    assert cli.main(["rescore"]) == 0
    sf = _get(db, "greenhouse:sf:1")
    assert sf["score"] == 0.0
    assert sf["variant"] == "general"
    assert json.loads(sf["reasons"]) == ["outside Canada (US-only)"]
    paris = _get(db, "greenhouse:paris:2")
    assert paris["score"] == 0.0
    assert json.loads(paris["reasons"])[0].startswith("outside Canada")
    out = capsys.readouterr().out
    assert out.strip() == ("rescored 3 postings, 1 eligible, "
                           "2 flipped from positive score to ineligible")


def test_rescore_leaves_correct_row_unchanged(monkeypatch, tmp_path, capsys):
    db = _seed_rescore_db(monkeypatch, tmp_path)
    before = _get(db, "greenhouse:to:3")
    assert cli.main(["rescore"]) == 0
    after = _get(db, "greenhouse:to:3")
    assert after["score"] == before["score"] and after["score"] > 0
    assert after["variant"] == before["variant"]
    assert after["reasons"] == before["reasons"]


def test_rescore_keeps_status_notes_first_seen_and_identity_fields(monkeypatch, tmp_path, capsys):
    db = _seed_rescore_db(monkeypatch, tmp_path)
    keys = ("uid", "status", "notes", "first_seen", "last_seen",
            "title", "company", "location", "url", "description")
    before = [{k: r[k] for k in keys} for r in _raw(db)]
    assert cli.main(["rescore"]) == 0
    after = [{k: r[k] for k in keys} for r in _raw(db)]
    assert repr(after) == repr(before)  # byte-identical rendering of every field
    assert {r["uid"]: r["status"] for r in after}["greenhouse:to:3"] == "shortlisted"


def test_rescore_dry_run_reports_same_counts_and_writes_nothing(monkeypatch, tmp_path, capsys):
    db = _seed_rescore_db(monkeypatch, tmp_path)
    before = _raw(db)
    assert cli.main(["rescore", "--dry-run"]) == 0
    dry_out = capsys.readouterr().out
    assert _raw(db) == before
    assert _get(db, "greenhouse:sf:1")["score"] == 35.5
    assert _get(db, "greenhouse:paris:2")["score"] == 37.0

    assert cli.main(["rescore"]) == 0
    real_out = capsys.readouterr().out
    assert dry_out.replace("dry run, nothing written: ", "") == real_out


def test_rescore_is_idempotent(monkeypatch, tmp_path, capsys):
    db = _seed_rescore_db(monkeypatch, tmp_path)
    assert cli.main(["rescore"]) == 0
    first = _raw(db)
    capsys.readouterr()
    assert cli.main(["rescore"]) == 0
    assert _raw(db) == first
    assert capsys.readouterr().out.strip().endswith("0 flipped from positive score to ineligible")


def test_rescore_is_in_help(capsys):
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    assert "rescore" in capsys.readouterr().out


# ---------- include-us, last_scan.json, dashboard refresh ----------

def _elig_kwargs(kwargs: dict) -> dict:
    """The recorded kwargs minus the preferences dict, which the caller now always passes."""
    return {k: v for k, v in kwargs.items() if k != "prefs"}


def _recording_is_eligible(calls):
    def fake(posting, **kwargs):
        calls.append(kwargs)
        return True, ""
    return fake


def test_scan_include_us_reaches_is_eligible(monkeypatch, capsys):
    _fake_scan_inputs(monkeypatch, errors=())
    _fake_store(monkeypatch)
    calls: list[dict] = []
    monkeypatch.setattr(cli, "is_eligible", _recording_is_eligible(calls))
    assert cli.main(["scan", "--include-us"]) == 0
    assert calls and all(_elig_kwargs(kw) == {"include_us": True} for kw in calls)
    calls.clear()
    assert cli.main(["scan"]) == 0
    assert calls and all(_elig_kwargs(kw) == {} for kw in calls)   # default is off


def test_rescore_include_us_reaches_is_eligible(monkeypatch, tmp_path, capsys):
    _seed_rescore_db(monkeypatch, tmp_path)
    calls: list[dict] = []
    monkeypatch.setattr(cli, "is_eligible", _recording_is_eligible(calls))
    assert cli.main(["rescore", "--include-us"]) == 0
    assert len(calls) == 3 and all(_elig_kwargs(kw) == {"include_us": True} for kw in calls)
    assert capsys.readouterr().out.startswith("rescored 3 postings, 3 eligible")


def test_scan_writes_last_scan_json_and_renders_dashboard_once(monkeypatch, capsys, iso):
    _fake_scan_inputs(monkeypatch)
    _fake_store(monkeypatch)
    assert cli.main(["scan", "--min-score", "40"]) == 0
    saved = json.loads(cli.LAST_SCAN_PATH.read_text(encoding="utf-8"))
    assert set(saved) == {"scanned_at", "fetched", "new", "updated", "eligible", "sources", "problems",
                          "dead_scans"}
    datetime.fromisoformat(saved["scanned_at"])
    assert (saved["fetched"], saved["new"], saved["updated"], saved["eligible"]) == (5, 2, 3, 5)
    assert saved["problems"] == ["greenhouse:cloudflare: HTTP 404"]
    assert all(set(s) == {"name", "ats", "ok", "detail"} for s in saved["sources"])
    assert len(iso.dash) == 1
    assert iso.dash[0]["last_scan"] == saved
    assert iso.dash[0]["out_path"] == str(cli.DASHBOARD_PATH)
    assert iso.dash[0]["db_path"] == os.environ["JOBS_DB"]
    # the stdout contract is unchanged
    out = capsys.readouterr().out
    assert "fetched 5 postings, 2 new, 3 already seen" in out
    assert "sources with problems:" in out


def test_scan_survives_dashboard_failure(monkeypatch, capsys, iso):
    _fake_scan_inputs(monkeypatch, errors=())
    _fake_store(monkeypatch)

    def boom(*a, **k):
        raise RuntimeError("dashboard broke")

    _set_module_attr(monkeypatch, "jobsearch.dashboard", "render_dashboard", boom)
    assert cli.main(["scan"]) == 0
    captured = capsys.readouterr()
    assert "fetched 5 postings" in captured.out
    assert "dashboard not refreshed" in captured.err


def test_source_health_marks_dead_and_live_boards():
    entries = [{"name": "Cloudflare", "ats": "greenhouse", "token": "cloudflare"},
               {"name": "Wiz", "ats": "ashby", "token": "wiz"}]
    postings = [{"uid": "greenhouse:cloudflare:1"}, {"uid": "greenhouse:cloudflare:2"}]
    health = cli._source_health(entries, postings, ["ashby:wiz: 0 postings (dead board?)"])
    assert health == [
        {"name": "Cloudflare", "ats": "greenhouse", "ok": True, "detail": "2 postings"},
        {"name": "Wiz", "ats": "ashby", "ok": False, "detail": "0 postings (dead board?)"},
    ]


def test_rescore_refreshes_dashboard_with_saved_last_scan(monkeypatch, tmp_path, capsys, iso):
    _seed_rescore_db(monkeypatch, tmp_path)
    saved = {"scanned_at": "2026-11-23T12:00:00+00:00", "fetched": 1, "new": 0, "updated": 1,
             "eligible": 1, "sources": [], "problems": []}
    cli.LAST_SCAN_PATH.write_text(json.dumps(saved), encoding="utf-8")
    assert cli.main(["rescore"]) == 0
    assert len(iso.dash) == 1
    assert iso.dash[0]["last_scan"] == saved
    assert iso.dash[0]["db_path"] == os.environ["JOBS_DB"]


def test_rescore_without_last_scan_refreshes_dashboard_with_none(monkeypatch, tmp_path, capsys, iso):
    _seed_rescore_db(monkeypatch, tmp_path)
    assert cli.main(["rescore"]) == 0
    assert len(iso.dash) == 1 and iso.dash[0]["last_scan"] is None


def test_rescore_dry_run_does_not_refresh_dashboard(monkeypatch, tmp_path, capsys, iso):
    _seed_rescore_db(monkeypatch, tmp_path)
    assert cli.main(["rescore", "--dry-run"]) == 0
    assert iso.dash == []


def test_dashboard_subcommand_writes_to_out_and_prints_path(tmp_path, capsys, iso):
    target = tmp_path / "d.html"
    assert cli.main(["dashboard", "--out", str(target)]) == 0
    assert capsys.readouterr().out.strip() == str(target)
    assert iso.dash[0]["out_path"] == str(target)


# ---------- letter subcommand ----------

def test_letter_renders_and_prints_path(monkeypatch, tmp_path, capsys, iso):
    _fake_store(monkeypatch)
    body = tmp_path / "body.txt"
    body.write_text("Dear team,\n\nA paragraph.\n", encoding="utf-8")
    assert cli.main(["letter", "greenhouse:a:1", "--body-file", str(body)]) == 0
    printed = capsys.readouterr().out.strip()
    assert printed.endswith("cover_letter.pdf") and Path(printed).is_file()
    assert iso.letter == [{"uid": "greenhouse:a:1", "body_path": str(body), "out_dir": None}]


def test_letter_missing_body_exits_1_and_creates_nothing(monkeypatch, tmp_path, capsys, iso):
    store = _fake_store(monkeypatch)
    rc = cli.main(["letter", "greenhouse:a:1", "--body-file", str(tmp_path / "nope.txt")])
    captured = capsys.readouterr()
    assert rc == 1
    assert "body file not found" in captured.err
    assert captured.out == ""
    assert iso.letter == [] and iso.dash == []
    assert store.calls == []                              # no store opened, no status touched
    assert not (tmp_path / "packets").exists()
    assert not (tmp_path / "jobs.db").exists()
    assert not list(tmp_path.glob("**/cover_letter.pdf"))


def test_letter_unknown_uid_exits_1(monkeypatch, tmp_path, capsys, iso):
    _fake_store(monkeypatch)
    body = tmp_path / "body.txt"
    body.write_text("x", encoding="utf-8")
    assert cli.main(["letter", "nope:nope:0", "--body-file", str(body)]) == 1
    assert iso.letter == []


# ---------- prep --letter ----------

def test_prep_letter_passes_body_path_and_sets_prepped(monkeypatch, tmp_path, capsys, iso):
    packets, out = _prep_env(monkeypatch, tmp_path)
    store = _fake_store(monkeypatch)
    (out / cli.PDF_NAMES["ai-security"]).write_bytes(b"%PDF-1.4 resume")
    monkeypatch.setattr(cli, "_run_build", lambda v: (0, "", ""))
    body = tmp_path / "body.txt"
    body.write_text("Dear team,\n\nHello.\n", encoding="utf-8")

    rc = cli.main(["prep", "ashby:d:4", "--letter", str(body)])
    captured = capsys.readouterr()
    assert rc == 0
    slug = packets / "ashby_d_4"
    assert iso.letter == [{"uid": "ashby:d:4", "body_path": str(body), "out_dir": str(slug)}]
    assert (slug / "resume.pdf").is_file() and (slug / "posting.md").is_file()
    assert ("set_status", "ashby:d:4", "prepped", "packet staged (ai-security resume)") in store.calls
    assert "cover letter:" in captured.out


def test_prep_letter_build_failure_sets_no_status_and_renders_nothing(monkeypatch, tmp_path, capsys, iso):
    _prep_env(monkeypatch, tmp_path)
    store = _fake_store(monkeypatch)
    monkeypatch.setattr(cli, "_run_build", lambda v: (1, "", "boom"))
    body = tmp_path / "body.txt"
    body.write_text("x", encoding="utf-8")
    assert cli.main(["prep", "ashby:d:4", "--letter", str(body)]) == 1
    assert iso.letter == []
    assert not [c for c in store.calls if c[0] == "set_status"]
    assert store.rows["ashby:d:4"]["status"] == "new"


def test_prep_letter_missing_body_runs_no_build(monkeypatch, tmp_path, capsys, iso):
    packets, _out = _prep_env(monkeypatch, tmp_path)
    store = _fake_store(monkeypatch)
    builds = []
    monkeypatch.setattr(cli, "_run_build", lambda v: builds.append(v) or (0, "", ""))
    rc = cli.main(["prep", "ashby:d:4", "--letter", str(tmp_path / "missing.txt")])
    assert rc == 1
    assert "body file not found" in capsys.readouterr().err
    assert builds == [] and not packets.exists()
    assert not [c for c in store.calls if c[0] == "set_status"]


def test_prep_letter_render_failure_leaves_status_unchanged(monkeypatch, tmp_path, capsys, iso):
    packets, out = _prep_env(monkeypatch, tmp_path)
    store = _fake_store(monkeypatch)
    (out / cli.PDF_NAMES["ai-security"]).write_bytes(b"%PDF-1.4 resume")
    monkeypatch.setattr(cli, "_run_build", lambda v: (0, "", ""))

    def broken(*a, **k):
        raise RuntimeError("tectonic exploded")

    _set_module_attr(monkeypatch, "jobsearch.letter", "render_letter", broken)
    body = tmp_path / "body.txt"
    body.write_text("x", encoding="utf-8")
    assert cli.main(["prep", "ashby:d:4", "--letter", str(body)]) == 1
    assert "tectonic exploded" in capsys.readouterr().err
    assert not [c for c in store.calls if c[0] == "set_status"]


def test_prep_without_letter_never_renders_letter(monkeypatch, tmp_path, capsys, iso):
    _prep_env(monkeypatch, tmp_path)
    store = _fake_store(monkeypatch)
    (tmp_path / "out" / cli.PDF_NAMES["ai-security"]).write_bytes(b"%PDF-1.4 resume")
    monkeypatch.setattr(cli, "_run_build", lambda v: (0, "", ""))
    assert cli.main(["prep", "ashby:d:4"]) == 0
    assert iso.letter == []
    assert ("set_status", "ashby:d:4", "prepped", "packet staged (ai-security resume)") in store.calls


# ---------- board health (dead_scans) and include-us scoring ----------

def _health_registry():
    return [{"name": "Cloudflare", "ats": "greenhouse", "token": "cloudflare"},
            {"name": "Wiz", "ats": "ashby", "token": "wiz"}]


def _scan_with_registry(monkeypatch, registry, postings, errors):
    monkeypatch.setattr(cli, "load_registry", lambda *a, **k: registry)
    monkeypatch.setattr(cli, "load_profile", lambda *a, **k: {})
    monkeypatch.setattr(cli, "fetch_all", lambda reg, include_aggregators=True: (postings, list(errors)))
    monkeypatch.setattr(cli, "is_eligible", lambda p, **kw: (True, ""))
    monkeypatch.setattr(cli, "score_posting", lambda p, prof, **kw: (10.0, "general", ["why"]))
    store = _fake_store(monkeypatch, rows=[], upsert=(0, 0))
    store.set_score = lambda *a, **k: None
    assert cli.main(["scan"]) == 0
    return json.loads(cli.LAST_SCAN_PATH.read_text(encoding="utf-8"))


def test_dead_scans_counts_consecutive_empty_scans_and_resets(monkeypatch, capsys, iso):
    wiz_hit = [{"uid": "ashby:wiz:1", "source": "ashby", "company": "Wiz", "title": "T",
                "location": "Remote", "url": "", "description": "", "posted_at": "", "remote": True}]
    cf_hit = [{"uid": "greenhouse:cloudflare:1", "source": "greenhouse", "company": "CF", "title": "T",
               "location": "Remote", "url": "", "description": "", "posted_at": "", "remote": True}]
    # scan 1: Wiz empty (no postings, no error)
    s1 = _scan_with_registry(monkeypatch, _health_registry(), cf_hit, [])
    assert s1["dead_scans"] == {"ashby:wiz": 1}
    # scan 2: Wiz fails with an error, still empty -> 2 consecutive
    s2 = _scan_with_registry(monkeypatch, _health_registry(), cf_hit,
                             ["ashby:wiz: HTTP 500"])
    assert s2["dead_scans"] == {"ashby:wiz": 2}
    stored = json.loads((iso.tmp / "board_health.json").read_text(encoding="utf-8"))
    assert stored == {"ashby:wiz": 2, "greenhouse:cloudflare": 0}
    # scan 3: Wiz returns postings -> reset to 0; key kept, dead_scans omits it
    s3 = _scan_with_registry(monkeypatch, _health_registry(), cf_hit + wiz_hit, [])
    assert s3["dead_scans"] == {}
    stored = json.loads((iso.tmp / "board_health.json").read_text(encoding="utf-8"))
    assert stored == {"ashby:wiz": 0, "greenhouse:cloudflare": 0}


def test_corrupt_board_health_file_is_treated_as_empty(monkeypatch, capsys, iso):
    (iso.tmp / "board_health.json").write_text("{not json", encoding="utf-8")
    s = _scan_with_registry(monkeypatch, _health_registry(), [], [])
    assert s["dead_scans"] == {"greenhouse:cloudflare": 1, "ashby:wiz": 1}
    assert json.loads((iso.tmp / "board_health.json").read_text(encoding="utf-8"))["ashby:wiz"] == 1


def test_board_health_created_on_first_scan_when_absent(monkeypatch, capsys, iso):
    assert not (iso.tmp / "board_health.json").exists()
    _scan_with_registry(monkeypatch, [], [], [])
    assert json.loads((iso.tmp / "board_health.json").read_text(encoding="utf-8")) == {}


def test_scan_include_us_stores_positive_score_for_us_posting(monkeypatch, capsys):
    us = [{"uid": "ramp:us:1", "source": "ashby", "company": "Ramp", "title": "Software Engineer, Security, Cloud",
           "location": "New York, NY (HQ)", "url": "", "description": "Build security tooling in Python.",
           "posted_at": "", "remote": False}]
    monkeypatch.setattr(cli, "load_registry", lambda *a, **k: [])
    monkeypatch.setattr(cli, "load_profile", lambda *a, **k: {"tokens": {"python"}, "skills": set(), "titles": set()})
    monkeypatch.setattr(cli, "fetch_all", lambda reg, include_aggregators=True: (us, []))
    stored = {}
    store = _fake_store(monkeypatch, rows=[], upsert=(1, 0))
    store.rows["ramp:us:1"] = {"uid": "ramp:us:1", "status": "new", "score": 0.0, "company": "Ramp",
                               "title": "Software Engineer, Security, Cloud", "location": "New York, NY (HQ)",
                               "url": "", "variant": "general", "reasons": [], "description": "", "source": "ashby"}

    def record_set_score(uid, score, variant, reasons):
        stored[uid] = score
    store.set_score = record_set_score
    assert cli.main(["scan", "--include-us"]) == 0
    assert stored["ramp:us:1"] > 0
    stored.clear()
    assert cli.main(["scan"]) == 0
    assert stored["ramp:us:1"] == 0.0


def test_scan_line_reports_backfilled_dedupe_keys_only_when_nonzero(monkeypatch, capsys):
    _fake_scan_inputs(monkeypatch, errors=())
    store = _fake_store(monkeypatch)
    counts = iter([3, 0])                      # before-upsert backfill fills 3, after-upsert 0
    store.backfill_dedupe_keys = lambda: next(counts)
    assert cli.main(["scan", "--min-score", "40"]) == 0
    assert "fetched 5 postings, 2 new, 3 already seen, 3 dedupe keys backfilled" in capsys.readouterr().out


def test_scan_line_has_no_backfill_clause_when_nothing_to_backfill(monkeypatch, capsys):
    _fake_scan_inputs(monkeypatch, errors=())
    _fake_store(monkeypatch)
    assert cli.main(["scan", "--min-score", "40"]) == 0
    out = capsys.readouterr().out
    assert "fetched 5 postings, 2 new, 3 already seen\n" in out


def test_scan_collapses_bilingual_repost_before_limit(monkeypatch, capsys):
    hop_en = "Forward Deployed AI Engineer, Travel Intelligence"
    hop_bi = "Ingénieur(e) IA en déploiement avancé, Travel Intelligence// " + hop_en
    rows = [
        ("hop:en", "new", 40.5, "Hopper", hop_en),
        ("hop:bi", "new", 40.5, "Hopper", hop_bi),
        ("oth:1", "new", 20.0, "Acme", "Platform Engineer"),
    ]
    postings = [{"uid": uid, "source": "greenhouse", "company": co, "title": ti, "location": "Remote",
                 "url": "", "description": "", "posted_at": "", "remote": True}
                for uid, _st, _sc, co, ti in rows]
    scores = {uid: sc for uid, _st, sc, _co, _ti in rows}
    monkeypatch.setattr(cli, "load_registry", lambda *a, **k: [])
    monkeypatch.setattr(cli, "load_profile", lambda *a, **k: {})
    monkeypatch.setattr(cli, "fetch_all", lambda reg, include_aggregators=True: (postings, []))
    monkeypatch.setattr(cli, "is_eligible", lambda p, **kw: (True, ""))
    monkeypatch.setattr(cli, "score_posting", lambda p, prof, **kw: (scores[p["uid"]], "ai-platform", []))
    store = _fake_store(monkeypatch, rows=rows)
    # --limit 2: without collapsing, the repost would take one of the two slots.
    rc = cli.main(["scan", "--min-score", "15", "--limit", "2"])
    out = capsys.readouterr().out
    assert rc == 0
    assert out.count("Hopper") == 1
    assert "Platform Engineer" in out
    assert ("list", "new", 15.0, cli._NO_LIMIT) in store.calls
    # both reposts stay stored
    assert store.get("hop:bi") is not None and store.get("hop:en") is not None
