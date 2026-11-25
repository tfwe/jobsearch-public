import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from jobsearch.sources import canonical_key
from jobsearch.store import FRESHNESS_DAYS, STATUSES, Store, rank_score


def _p(uid, **kw):
    base = {"uid": uid, "source": "greenhouse", "company": "Acme", "title": "AI Security Engineer",
            "location": "Remote", "url": f"https://x/{uid}", "description": "desc",
            "posted_at": "2026-11-01", "remote": True}
    base.update(kw)
    return base


@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path / "t.db"))
    yield s
    s.close()


def test_first_insert_is_new_second_is_updated_no_duplicate(store):
    assert store.upsert_postings([_p("a:1")]) == (1, 0)
    assert store.upsert_postings([_p("a:1", title="Renamed")]) == (0, 1)
    assert store.stats()["total"] == 1
    assert store.get("a:1")["title"] == "Renamed"


def test_upsert_preserves_score_status_notes(store):
    store.upsert_postings([_p("a:1")])
    store.set_score("a:1", 77.5, "ai-security", ["title hit"])
    store.set_status("a:1", "shortlisted", "good one")
    store.upsert_postings([_p("a:1", description="new text")])
    row = store.get("a:1")
    assert row["score"] == 77.5
    assert row["variant"] == "ai-security"
    assert row["reasons"] == ["title hit"]
    assert row["status"] == "shortlisted"
    assert row["description"] == "new text"


@pytest.mark.parametrize("terminal", ["skipped", "closed", "rejected"])
def test_terminal_status_stays_after_reupsert(store, terminal):
    store.upsert_postings([_p("a:1")])
    store.set_status("a:1", terminal)
    assert store.upsert_postings([_p("a:1")]) == (0, 1)
    assert store.get("a:1")["status"] == terminal


def test_set_status_rejects_unknown_state(store):
    store.upsert_postings([_p("a:1")])
    with pytest.raises(ValueError):
        store.set_status("a:1", "banana")
    assert store.get("a:1")["status"] == "new"


def test_set_status_logs_transition_and_events_oldest_first(store):
    store.upsert_postings([_p("a:1")])
    store.set_status("a:1", "shortlisted")
    store.set_status("a:1", "applied", "sent via portal")
    ev = store.events("a:1")
    status_ev = [e for e in ev if e["kind"] == "status"]
    assert [e["detail"] for e in status_ev] == ["new->shortlisted", "shortlisted->applied"]
    assert [e["id"] for e in ev] == sorted(e["id"] for e in ev)
    assert any(e["kind"] == "note" and e["detail"] == "sent via portal" for e in ev)


def test_list_filters(store):
    store.upsert_postings([
        _p("a:1", company="Alpha", title="Detection Engineer"),
        _p("a:2", company="Beta", title="Sales Lead"),
        _p("a:3", company="Gamma", title="SOC Analyst"),
    ])
    store.set_score("a:1", 80, "sec-content", [])
    store.set_score("a:2", 30, "general", [])
    store.set_score("a:3", 55, "sec-content", [])
    store.set_status("a:3", "skipped")
    assert [r["uid"] for r in store.list()] == ["a:1", "a:3", "a:2"]
    assert [r["uid"] for r in store.list(min_score=50)] == ["a:1", "a:3"]
    assert [r["uid"] for r in store.list(query="beta")] == ["a:2"]
    assert [r["uid"] for r in store.list(exclude_status=("skipped",))] == ["a:1", "a:2"]
    assert [r["uid"] for r in store.list(status="skipped")] == ["a:3"]
    assert len(store.list(limit=1)) == 1


def test_stats_by_status_totals_match_rows(store):
    store.upsert_postings([_p(f"a:{i}") for i in range(5)])
    store.set_status("a:0", "applied")
    store.set_status("a:1", "applied")
    store.set_status("a:2", "rejected")
    s = store.stats()
    assert s["total"] == 5
    assert sum(s["by_status"].values()) == 5
    assert s["by_status"]["applied"] == 2
    assert s["by_status"]["rejected"] == 1
    assert s["by_status"]["new"] == 2
    assert s["new_24h"] == 5


def test_default_path_honours_env(tmp_path, monkeypatch):
    target = tmp_path / "env.db"
    monkeypatch.setenv("JOBS_DB", str(target))
    s = Store()
    s.upsert_postings([_p("a:1")])
    s.close()
    assert target.exists()
    conn = sqlite3.connect(target)
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    conn.close()


LEGACY_SCHEMA = """
CREATE TABLE postings (
  uid TEXT PRIMARY KEY, source TEXT, company TEXT, title TEXT, location TEXT,
  url TEXT, description TEXT, posted_at TEXT, remote INTEGER DEFAULT 0,
  first_seen TEXT, last_seen TEXT, score REAL, variant TEXT, reasons TEXT,
  status TEXT NOT NULL DEFAULT 'new', notes TEXT DEFAULT '');
CREATE TABLE events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, uid TEXT, ts TEXT, kind TEXT, detail TEXT);
"""


def _cols(store):
    return {r[1] for r in store._conn.execute("PRAGMA table_info(postings)").fetchall()}


def test_legacy_db_gains_level_columns(tmp_path):
    path = str(tmp_path / "legacy.db")
    conn = sqlite3.connect(path)
    conn.executescript(LEGACY_SCHEMA)
    conn.execute("INSERT INTO postings (uid, title, status) VALUES ('old:1', 'Analyst', 'new')")
    conn.commit()
    conn.close()

    s = Store(path)
    assert {"level", "level_years"} <= _cols(s)
    assert s.get("old:1")["title"] == "Analyst"
    assert s.get("old:1")["level"] is None
    s.close()
    # reopening is idempotent
    s = Store(path)
    assert {"level", "level_years"} <= _cols(s)
    s.close()


def test_backfill_levels_sets_once_and_second_call_is_zero(store):
    from jobsearch.reach import classify
    store.upsert_postings([_p("a:1", title="Staff Engineer", description=""),
                           _p("a:2", title="Security Analyst", description="")])
    assert store.backfill_levels(classify) == 2
    assert store.get("a:1")["level"] == "staff"
    assert store.backfill_levels(classify) == 0
    # a row whose level is already set is never rewritten
    store._conn.execute("UPDATE postings SET level='mid' WHERE uid='a:1'")
    store._conn.commit()
    assert store.backfill_levels(lambda t, d: {"level": "intern", "years": None}) == 0
    assert store.get("a:1")["level"] == "mid"


def test_upsert_does_not_write_level(store):
    store.upsert_postings([_p("a:1", title="Staff Engineer")])
    store.upsert_postings([_p("a:1", title="Staff Engineer")])
    assert store.get("a:1")["level"] is None


def _reach_fixture(store):
    from jobsearch.reach import classify
    rows = [
        ("ok", "Security Analyst", "desc", 90.0),
        ("staff", "Staff Engineer", "desc", 80.0),
        ("eight", "Security Engineer", "Requires 8+ years of experience in security.", 70.0),
        ("nul", "Detection Engineer", "desc", 60.0),
        ("junior", "Junior Analyst", "desc", 50.0),
        ("four", "Detection Engineer", "Minimum 4 years of experience.", 40.0),
    ]
    store.upsert_postings([_p(u, title=t, description=d) for u, t, d, _ in rows])
    for u, _, _, sc in rows:
        store.set_score(u, sc, "general", [])
    store.backfill_levels(classify)
    store._conn.execute("UPDATE postings SET level=NULL, level_years=NULL WHERE uid='nul'")
    store._conn.commit()


def test_reach_only_keeps_null_and_drops_out_of_reach(store):
    _reach_fixture(store)
    kept = {r["uid"] for r in store.browse(limit=100, reach_only=True)}
    assert "nul" in kept
    assert "staff" not in kept
    assert "eight" not in kept
    assert {"ok", "junior", "four"} <= kept
    assert store.browse_count(reach_only=True) == len(kept)
    assert store.browse_count() == 6
    assert store.browse_count(reach_only=False) == 6


def test_reach_only_false_leaves_rows_alone(store):
    _reach_fixture(store)
    assert {r["uid"] for r in store.browse(limit=100)} == {
        "ok", "staff", "eight", "nul", "junior", "four"}


def test_reach_only_keeps_score_order(store):
    _reach_fixture(store)
    everything = [r["uid"] for r in store.browse(limit=100)]
    filtered = [r["uid"] for r in store.browse(limit=100, reach_only=True)]
    assert filtered == [u for u in everything if u in set(filtered)]
    assert filtered == ["ok", "nul", "junior", "four"]
    row = store.browse(limit=100)[0]
    assert "level" in row and "level_years" in row


def test_new_row_level_is_null_until_backfill(store):
    from jobsearch.reach import classify
    store.upsert_postings([_p("n:1", title="Staff Engineer", description="")])
    assert store.get("n:1")["level"] is None
    assert store.get("n:1")["level_years"] is None
    assert store.backfill_levels(classify) == 1
    assert store.get("n:1")["level"] == "staff"


def test_updated_senior_title_is_hidden_by_reach_only_after_backfill(store):
    from jobsearch.reach import classify
    store.upsert_postings([_p("u:1", title="Security Analyst", description="desc")])
    store.set_score("u:1", 90.0, "general", [])
    store.backfill_levels(classify)
    assert store.get("u:1")["level"] == "mid"
    assert "u:1" in {r["uid"] for r in store.browse(limit=100, reach_only=True)}

    # re-scan with a senior-group title: the stale level is cleared, not kept
    store.upsert_postings([_p("u:1", title="VP of Security", description="desc")])
    assert store.get("u:1")["level"] is None and store.get("u:1")["level_years"] is None
    store.backfill_levels(classify)
    assert store.get("u:1")["level"] == "director"
    assert "u:1" not in {r["uid"] for r in store.browse(limit=100, reach_only=True)}
    assert store.backfill_levels(classify) == 0


def test_store_set_liveness_migrates_old_db_and_logs_event(tmp_path):
    """A database written before the liveness columns gains them in place, twice-openable."""
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE postings (uid TEXT PRIMARY KEY, source TEXT, company TEXT, title TEXT,"
        " location TEXT, url TEXT, description TEXT, posted_at TEXT, remote INTEGER,"
        " first_seen TEXT, last_seen TEXT, score REAL, variant TEXT, reasons TEXT,"
        " status TEXT NOT NULL DEFAULT 'new', notes TEXT DEFAULT '', level TEXT);"
        "CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, uid TEXT, ts TEXT,"
        " kind TEXT, detail TEXT);"
        "INSERT INTO postings (uid, source, company, title, url, status)"
        " VALUES ('a:1', 'greenhouse', 'Acme', 'Engineer', 'https://x/1', 'new');")
    conn.commit()
    conn.close()

    store = Store(str(path))
    store.close()
    store = Store(str(path))  # second open on the migrated file must not raise
    try:
        cols = {r[1] for r in store._conn.execute("PRAGMA table_info(postings)")}
        assert {"live_status", "live_checked_at", "level_years"} <= cols
        assert store.get("a:1")["live_status"] is None
        store.set_liveness("a:1", "gone", "HTTP 404", "2026-11-24T12:00:00+00:00")
        row = store.get("a:1")
        assert row["live_status"] == "gone"
        assert row["live_checked_at"] == "2026-11-24T12:00:00+00:00"
        assert row["status"] == "new"  # a gone page never changes the human's status
        [event] = [e for e in store.events("a:1") if e["kind"] == "liveness"]
        assert event["detail"] == "gone: HTTP 404"
        assert event["ts"] == "2026-11-24T12:00:00+00:00"
        with pytest.raises(ValueError):
            store.set_liveness("a:1", "closed")
        with pytest.raises(KeyError):
            store.set_liveness("nope:1", "live")
    finally:
        store.close()


def test_follow_up_round_trip_and_clear(store):
    # Empty table: every read is safe and empty.
    assert store.funnel_counts() == {s: 0 for s in STATUSES}
    assert store.variant_stats() == []
    assert store.due_follow_ups("2026-12-31") == []

    store.upsert_postings([_p("a:1")])
    store.set_follow_up("a:1", "2026-12-01")
    assert store.get("a:1")["follow_up_at"] == "2026-12-01"
    assert [r["uid"] for r in store.due_follow_ups("2026-12-01")] == ["a:1"]
    assert store.due_follow_ups("2026-11-30") == []

    store.set_follow_up("a:1", None)
    assert store.get("a:1")["follow_up_at"] is None
    assert store.events("a:1")[-1]["kind"] == "followup"
    assert store.events("a:1")[-1]["detail"] == "cleared"

    with pytest.raises(KeyError):
        store.set_follow_up("nope:1", "2026-12-01")
    with pytest.raises(ValueError):
        store.set_follow_up("a:1", "2026-13-01")
    with pytest.raises(ValueError):
        store.set_follow_up("a:1", "next week")
    assert store.get("a:1")["follow_up_at"] is None  # rejected values change nothing


# -- freshness-first ordering ------------------------------------------------

def _ago(days: float) -> str:
    """A posted_at date-only string `days` before now (UTC). Computed, never hardcoded."""
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")


def _ids(store, **kw):
    return [r["uid"] for r in store.browse(limit=100, **kw)]


def test_fresh_posting_outranks_an_older_one_with_a_marginally_higher_score(store):
    store.upsert_postings([_p("f:old", posted_at=_ago(60)), _p("f:new", posted_at=_ago(0))])
    store.set_score("f:old", 32.0, "general", [])
    store.set_score("f:new", 30.0, "general", [])
    assert _ids(store) == ["f:new", "f:old"]


def test_a_much_higher_score_still_outranks_a_fresh_one(store):
    store.upsert_postings([_p("h:old", posted_at=_ago(60)), _p("h:new", posted_at=_ago(0))])
    store.set_score("h:old", 200.0, "general", [])
    store.set_score("h:new", 30.0, "general", [])
    assert _ids(store) == ["h:old", "h:new"]


def test_sql_and_python_rank_agree(store):
    now = datetime.now(timezone.utc)
    spec = [("m:1", 90.0, 40), ("m:2", 35.0, 0), ("m:3", 80.0, 45), ("m:4", 25.0, 5),
            ("m:5", 60.0, 20), ("m:6", 12.0, 90), ("m:7", 99.0, 120)]
    store.upsert_postings([_p(u, posted_at=_ago(d), company=f"Co{i}")
                           for i, (u, _, d) in enumerate(spec)])
    for u, sc, _ in spec:
        store.set_score(u, sc, "general", [])
    rows = [store.get(u) for u, _, _ in spec]
    for r in rows:
        posted = datetime.strptime(r["posted_at"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        r["age"] = (now - posted).total_seconds() / 86400.0
    expected = [r["uid"] for r in sorted(rows, key=lambda r: -rank_score(r["score"], r["age"]))]
    assert _ids(store) == expected


def test_null_score_still_sorts_last(store):
    store.upsert_postings([_p("n:null", posted_at=_ago(0)), _p("n:old", posted_at=_ago(400))])
    store.set_score("n:old", 1.0, "general", [])
    assert _ids(store) == ["n:old", "n:null"]


def test_missing_posted_at_falls_back_to_first_seen(store):
    store.upsert_postings([_p("fb:today", posted_at=_ago(0)), _p("fb:seen", posted_at="")])
    store.set_score("fb:today", 30.0, "general", [])
    store.set_score("fb:seen", 30.0, "general", [])
    old = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat(timespec="seconds")
    store._conn.execute("UPDATE postings SET first_seen=? WHERE uid='fb:seen'", (old,))
    store._conn.commit()
    assert _ids(store) == ["fb:today", "fb:seen"]


def test_missing_posted_at_and_first_seen_counts_as_fresh(store):
    store.upsert_postings([_p("u:none", posted_at=_ago(0)), _p("u:blank", posted_at="")])
    store.set_score("u:none", 30.0, "general", [])
    store.set_score("u:blank", 30.0, "general", [])
    store._conn.execute("UPDATE postings SET posted_at=NULL, first_seen=NULL WHERE uid='u:blank'")
    store._conn.commit()
    # unknown age counts as 0, so the undated row keeps its full score and beats a row
    # posted today (whose age is a fraction of a day), rather than being punished for it
    assert _ids(store) == ["u:blank", "u:none"]


def test_rank_score_unit_cases():
    assert rank_score(None, 5) == -1.0
    assert rank_score(40.0, 0) == 40.0
    assert rank_score(60.0, FRESHNESS_DAYS) == pytest.approx(30.0)
    assert rank_score(40.0, -7) == 40.0
    assert rank_score(40.0, None) == 40.0


# ---------------------------------------------------------------- cross-source dedupe

def _dup(uid, **kw):
    """The same job as _p's default, under a different uid, as another source would carry it."""
    base = {"company": "Acme Inc.", "title": "Production Engineer (Remote)",
            "location": "Remote", "url": "https://jobs.acme.example/apply/42"}
    base.update(kw)
    return _p(uid, **base)


def test_same_posting_from_two_sources_is_stored_once(store):
    first = _p("remotive:remotive:1", source="remotive", company="Acme Inc.",
               title="Production Engineer", location="Remote",
               url="https://jobs.acme.example/apply/42")
    second = _p("greenhouse:acme:9", source="greenhouse", company="Acme",
                title="Production Engineer", location="Remote",
                url="https://jobs.acme.example/apply/42")
    assert store.upsert_postings([first]) == (1, 0)
    assert store.upsert_postings([second]) == (0, 1)      # counted as updated, never new
    assert store.stats()["total"] == 1
    assert store.get("greenhouse:acme:9") is None
    kept = store.get("remotive:remotive:1")
    assert kept["source"] == "remotive"                   # first to arrive keeps the row
    kinds = [e["kind"] for e in store.events("remotive:remotive:1")]
    assert "duplicate" in kinds
    dup = next(e for e in store.events("remotive:remotive:1") if e["kind"] == "duplicate")
    assert dup["detail"] == "greenhouse also carries this as greenhouse:acme:9"


def test_dedupe_key_missing_url_never_merges(store):
    a = _dup("a:1", url="")
    b = _dup("b:1", url="")
    assert store.upsert_postings([a, b]) == (2, 0)
    assert store.stats()["total"] == 2


def test_dedupe_key_is_stable_across_cosmetic_differences():
    a = {"company": "Acme, Inc.", "title": "Production Engineer (Remote)",
         "location": "Remote", "url": "https://Www.Acme.example/apply/42/?utm=1"}
    b = {"company": "acme", "title": "Production Engineer", "location": "remote!",
         "url": "https://acme.example/apply/42"}
    assert canonical_key(a) == canonical_key(b) != ""
    assert canonical_key(a) == "acme|production engineer|remote|https://acme.example/apply/42"


def test_backfill_dedupe_keys_fills_old_rows_and_is_idempotent(store):
    store.upsert_postings([_dup("a:1"), _p("b:1", company="Other", url="https://x/b")])
    store._conn.execute("UPDATE postings SET dedupe_key=NULL")
    store._conn.commit()
    assert store.backfill_dedupe_keys() == 2
    assert store.backfill_dedupe_keys() == 0
    keys = {r["uid"]: r["dedupe_key"] for r in
            store._conn.execute("SELECT uid, dedupe_key FROM postings").fetchall()}
    assert keys["a:1"] == canonical_key(_dup("a:1"))
    assert keys["b:1"] == canonical_key(_p("b:1", company="Other", url="https://x/b"))
