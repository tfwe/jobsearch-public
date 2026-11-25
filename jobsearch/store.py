"""SQLite store for jobsearch postings. """
from __future__ import annotations

import json
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

STATUSES = ("new", "shortlisted", "prepped", "applied", "interview",
            "offer", "rejected", "closed", "skipped")
# Terminal statuses. set_status never reads this constant, so nothing stops a row being
# moved out of one. Upsert never touches status at all.
TERMINAL_STATUSES = ("skipped", "closed", "rejected")
# What a liveness check can conclude. 'unknown' covers bot walls, rate limits and
# timeouts, which say nothing about whether the job itself is still open.
LIVE_STATUSES = ("live", "gone", "unknown")
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
# Score decays with age so a fresh posting beats a marginally higher-scoring stale one.
# Effective score = score / (1 + age_days / FRESHNESS_DAYS).
FRESHNESS_DAYS = 30.0


def rank_score(score: float | None, age_days: float | None) -> float:
    """The rank a postings row gets, mirroring the ORDER BY in browse(). None score -> -1.0
    (sorts last). None or negative age counts as 0."""
    if score is None:
        return -1.0
    age = max(0.0, age_days or 0.0)
    return score / (1.0 + age / FRESHNESS_DAYS)


# SQL form of rank_score(). Age runs from posted_at, then first_seen, then 'now' (age 0).
_RANK_SQL = (f"score / (1.0 + MAX(0.0, julianday('now') - "
             f"julianday(COALESCE(NULLIF(posted_at, ''), NULLIF(first_seen, ''), 'now')))"
             f" / {FRESHNESS_DAYS})")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS postings (
  uid TEXT PRIMARY KEY, source TEXT, company TEXT, title TEXT, location TEXT,
  url TEXT, description TEXT, posted_at TEXT, remote INTEGER DEFAULT 0,
  first_seen TEXT, last_seen TEXT, score REAL, variant TEXT, reasons TEXT,
  status TEXT NOT NULL DEFAULT 'new', notes TEXT DEFAULT '', dedupe_key TEXT);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, uid TEXT, ts TEXT, kind TEXT, detail TEXT);
"""


def default_db_path() -> str:
    env = os.environ.get("JOBS_DB")
    if env:
        return env
    return str(Path.home() / "jobsearch" / "jobsearch.db")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _row_to_dict(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    d = dict(row)
    d["remote"] = bool(d.get("remote"))
    d["reasons"] = json.loads(d["reasons"]) if d.get("reasons") else []
    return d


class Store:
    def __init__(self, path: str | None = None):
        self.path = path or default_db_path()
        if self.path != ":memory:":
            Path(self.path).expanduser().parent.mkdir(parents=True, exist_ok=True)
            self.path = str(Path(self.path).expanduser())
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA)
        # Idempotent migration: the schema has no migration system, so add the reach columns
        # to a pre-existing postings table that lacks them.
        have = {r[1] for r in self._conn.execute("PRAGMA table_info(postings)").fetchall()}
        if "level" not in have:
            self._conn.execute("ALTER TABLE postings ADD COLUMN level TEXT")
        if "level_years" not in have:
            self._conn.execute("ALTER TABLE postings ADD COLUMN level_years INTEGER")
        # Liveness columns, added the same way so a live jobsearch.db upgrades in place.
        if "live_status" not in have:
            self._conn.execute("ALTER TABLE postings ADD COLUMN live_status TEXT")
        if "live_checked_at" not in have:
            self._conn.execute("ALTER TABLE postings ADD COLUMN live_checked_at TEXT")
        # Follow-up reminder date (YYYY-MM-DD), same additive migration as the liveness columns.
        if "follow_up_at" not in have:
            self._conn.execute("ALTER TABLE postings ADD COLUMN follow_up_at TEXT")
        # Cross-source dedupe key (see sources.canonical_key). The index follows the column,
        # so it is created here rather than in _SCHEMA, which runs before the migration.
        if "dedupe_key" not in have:
            self._conn.execute("ALTER TABLE postings ADD COLUMN dedupe_key TEXT")
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_postings_dedupe ON postings(dedupe_key)")
        self._conn.commit()

    # -- writes -----------------------------------------------------------
    def upsert_postings(self, postings: list[dict]) -> tuple[int, int]:
        """Insert or refresh postings. Returns (new, updated).

        Refreshes title/location/url/description/posted_at/remote/source/company
        and last_seen. Never touches score, variant, reasons, status, notes or
        first_seen of an existing row, so skipped/closed/rejected rows stay put.
        level and level_years are reset to NULL on insert and on update, so the
        next backfill_levels() reclassifies the row against its current title.

        A new uid whose canonical key matches a row already stored is not inserted: the
        stored row keeps its place, gets a fresh last_seen and a 'duplicate' event, and
        counts as updated. Batch order decides: whichever source arrives first is kept.
        """
        from jobsearch.sources import canonical_key  # lazy: keep the network layer out of import

        new = updated = 0
        now = _now()
        cur = self._conn.cursor()
        for p in postings:
            uid = p["uid"]
            key = canonical_key(p)
            exists = cur.execute("SELECT 1 FROM postings WHERE uid=?", (uid,)).fetchone()
            if exists:
                cur.execute(
                    """UPDATE postings SET source=?, company=?, title=?, location=?,
                       url=?, description=?, posted_at=?, remote=?, last_seen=?,
                       level=NULL, level_years=NULL, dedupe_key=?
                       WHERE uid=?""",
                    (p.get("source", ""), p.get("company", ""), p.get("title", ""),
                     p.get("location", ""), p.get("url", ""), p.get("description", ""),
                     p.get("posted_at", ""), int(bool(p.get("remote"))), now, key, uid))
                updated += 1
                continue
            if key:
                kept = cur.execute(
                    "SELECT uid FROM postings WHERE dedupe_key=? AND uid<>? LIMIT 1",
                    (key, uid)).fetchone()
                if kept:
                    kept_uid = kept["uid"]
                    cur.execute("UPDATE postings SET last_seen=? WHERE uid=?", (now, kept_uid))
                    detail = f"{p.get('source', '')} also carries this as {uid}"
                    cur.execute(
                        "INSERT INTO events (uid, ts, kind, detail) VALUES (?,?,?,?)",
                        (kept_uid, now, "duplicate", detail))
                    updated += 1
                    continue
            cur.execute(
                """INSERT INTO postings (uid, source, company, title, location, url,
                   description, posted_at, remote, first_seen, last_seen, status,
                   level, level_years, dedupe_key)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?, 'new', NULL, NULL, ?)""",
                (uid, p.get("source", ""), p.get("company", ""), p.get("title", ""),
                 p.get("location", ""), p.get("url", ""), p.get("description", ""),
                 p.get("posted_at", ""), int(bool(p.get("remote"))), now, now, key))
            new += 1
        self._conn.commit()
        return new, updated

    def backfill_dedupe_keys(self) -> int:
        """Fill dedupe_key on rows inserted before the column existed. Second call returns 0.

        A row that cannot be keyed gets '' rather than staying NULL, so it is not re-read
        on every call.
        """
        from jobsearch.sources import canonical_key  # lazy: keep the network layer out of import

        rows = self._conn.execute(
            "SELECT uid, source, company, title, location, url FROM postings "
            "WHERE dedupe_key IS NULL").fetchall()
        for r in rows:
            self._conn.execute("UPDATE postings SET dedupe_key=? WHERE uid=?",
                               (canonical_key(dict(r)), r["uid"]))
        self._conn.commit()
        return len(rows)

    def set_score(self, uid: str, score: float, variant: str, reasons: list[str]) -> None:
        self._conn.execute(
            "UPDATE postings SET score=?, variant=?, reasons=? WHERE uid=?",
            (float(score), variant, json.dumps(list(reasons)), uid))
        self._conn.commit()

    def set_status(self, uid: str, status: str, note: str = "") -> None:
        if status not in STATUSES:
            raise ValueError(f"unknown status {status!r}; expected one of {STATUSES}")
        row = self._conn.execute("SELECT status FROM postings WHERE uid=?", (uid,)).fetchone()
        if row is None:
            raise KeyError(f"no posting with uid {uid!r}")
        old = row["status"]
        ts = _now()
        self._conn.execute("UPDATE postings SET status=? WHERE uid=?", (status, uid))
        self._conn.execute(
            "INSERT INTO events (uid, ts, kind, detail) VALUES (?,?,?,?)",
            (uid, ts, "status", f"{old}->{status}"))
        if note:
            self._conn.execute(
                "INSERT INTO events (uid, ts, kind, detail) VALUES (?,?,?,?)",
                (uid, ts, "note", note))
        self._conn.commit()

    def set_liveness(self, uid: str, status: str, reason: str = "",
                     checked_at: str | None = None) -> None:
        """Record the outcome of a posting-URL liveness check.

        Only the postings row's live columns change; postings.status is left alone,
        because closing a posting is the owner's decision. The event carries the reason
        so the timeline shows why the page was judged gone.
        """
        if status not in LIVE_STATUSES:
            raise ValueError(f"unknown liveness {status!r}; expected one of {LIVE_STATUSES}")
        row = self._conn.execute("SELECT 1 FROM postings WHERE uid=?", (uid,)).fetchone()
        if row is None:
            raise KeyError(f"no posting with uid {uid!r}")
        ts = checked_at or _now()
        self._conn.execute("UPDATE postings SET live_status=?, live_checked_at=? WHERE uid=?",
                           (status, ts, uid))
        self._conn.execute(
            "INSERT INTO events (uid, ts, kind, detail) VALUES (?,?,?,?)",
            (uid, ts, "liveness", f"{status}: {reason}".strip(": ")))
        self._conn.commit()

    def set_variant(self, uid: str, variant: str) -> None:
        """Record which resume variant the next packet build should use.

        The scorer writes this at score time; this is the one place a person can
        overrule it, so the change goes into the event log beside every status move.
        """
        row = self._conn.execute("SELECT variant FROM postings WHERE uid=?", (uid,)).fetchone()
        if row is None:
            raise KeyError(f"no posting with uid {uid!r}")
        old = row["variant"]
        self._conn.execute("UPDATE postings SET variant=? WHERE uid=?", (variant, uid))
        self._conn.execute(
            "INSERT INTO events (uid, ts, kind, detail) VALUES (?,?,?,?)",
            (uid, _now(), "variant", f"{old or 'none'}->{variant}"))
        self._conn.commit()

    def set_follow_up(self, uid: str, value: str | None) -> None:
        """Set the date a follow-up is due (YYYY-MM-DD), or clear it with None or ''.

        Raises ValueError for any other text and KeyError for an unknown uid. The event
        log records each change, so the timeline shows when a reminder was set or dropped.
        """
        if value:
            if not _ISO_DATE.fullmatch(value):
                raise ValueError(f"invalid date {value!r}; use YYYY-MM-DD")
            try:
                datetime.strptime(value, "%Y-%m-%d")
            except ValueError:
                raise ValueError(f"invalid date {value!r}; use YYYY-MM-DD") from None
            stored = value
        else:
            stored = None
        row = self._conn.execute("SELECT 1 FROM postings WHERE uid=?", (uid,)).fetchone()
        if row is None:
            raise KeyError(f"no posting with uid {uid!r}")
        self._conn.execute("UPDATE postings SET follow_up_at=? WHERE uid=?", (stored, uid))
        self._conn.execute(
            "INSERT INTO events (uid, ts, kind, detail) VALUES (?,?,?,?)",
            (uid, _now(), "followup", stored or "cleared"))
        self._conn.commit()

    def backfill_levels(self, classify) -> int:
        """Set level/level_years for rows where level IS NULL. classify(title, description) returns
        a dict with 'level' and 'years'. Rows that already have a level are never touched.
        Returns the number of rows updated; a second call returns 0."""
        rows = self._conn.execute(
            "SELECT uid, title, description FROM postings WHERE level IS NULL").fetchall()
        n = 0
        for r in rows:
            verdict = classify(r["title"] or "", r["description"] or "")
            cur = self._conn.execute(
                "UPDATE postings SET level=?, level_years=? WHERE uid=? AND level IS NULL",
                (verdict["level"], verdict["years"], r["uid"]))
            n += cur.rowcount
        self._conn.commit()
        return n

    # -- reads ------------------------------------------------------------
    def get(self, uid: str) -> dict | None:
        row = self._conn.execute("SELECT * FROM postings WHERE uid=?", (uid,)).fetchone()
        return _row_to_dict(row)

    def list(self, status: str | None = None, min_score: float | None = None,
             query: str | None = None, limit: int = 50,
             exclude_status: tuple[str, ...] = ()) -> list[dict]:
        clauses, args = [], []
        if status is not None:
            clauses.append("status=?")
            args.append(status)
        if min_score is not None:
            clauses.append("score >= ?")
            args.append(float(min_score))
        if query:
            clauses.append("(company || ' ' || title) LIKE ?")
            args.append(f"%{query}%")
        if exclude_status:
            clauses.append("status NOT IN (%s)" % ",".join("?" * len(exclude_status)))
            args.extend(exclude_status)
        sql = "SELECT * FROM postings"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY score IS NULL, score DESC, first_seen DESC, uid ASC LIMIT ?"
        args.append(int(limit))
        return [_row_to_dict(r) for r in self._conn.execute(sql, args).fetchall()]

    def events(self, uid: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT id, uid, ts, kind, detail FROM events WHERE uid=? ORDER BY id ASC",
            (uid,)).fetchall()
        return [dict(r) for r in rows]

    # -- browsable list for the triage screen (read-only, no description) -----
    def _browse_where(self, statuses, min_score, query, source, remote_canada,
                      exclude_french, french_sql, posted_after, hide_zero,
                      reach_only=False) -> tuple[str, list]:
        """The one WHERE clause both browse() and browse_count() use, so the count and
        the rows can never disagree about which postings a filter selects."""
        clauses, args = [], []
        if statuses:
            clauses.append("status IN (%s)" % ",".join("?" * len(statuses)))
            args.extend(statuses)
        if min_score is not None:
            clauses.append("score >= ?")
            args.append(float(min_score))
        if hide_zero:
            clauses.append("score > 0")
        if query:
            like = f"%{query}%"
            clauses.append("(company LIKE ? OR title LIKE ? OR location LIKE ?)")
            args.extend([like, like, like])
        if source:
            clauses.append("source = ?")
            args.append(source)
        if remote_canada:
            clauses.append("(remote = 1 OR lower(location) LIKE '%canada%' "
                           "OR lower(location) LIKE '%remote%')")
        if exclude_french:
            clauses.append(f"NOT ({french_sql})")
        if posted_after:
            clauses.append("posted_at >= ?")
            args.append(posted_after)
        if reach_only:
            # A NULL level is never hidden: unclassified is not the same as out of reach.
            clauses.append("(level IS NULL OR (level NOT IN ('staff','principal','lead','manager',"
                           "'director','architect') AND COALESCE(level_years, 0) < 5))")
        return (" WHERE " + " AND ".join(clauses)) if clauses else "", args

    BROWSABLE = ("uid", "source", "company", "title", "location", "url", "posted_at",
                 "first_seen", "remote", "score", "variant", "status", "level", "level_years",
                 "live_status", "live_checked_at")

    def browse(self, statuses=None, min_score=None, query=None, source=None,
               remote_canada=False, exclude_french=False, french_sql="0",
               posted_after=None, hide_zero=False, reach_only=False,
               limit=100, offset=0) -> list[dict]:
        """Slim rows for the list pane, highest freshness-adjusted score first (see rank_score).
        Description is not read, but the French predicate is returned as needs_french so a
        flagged row never needs its 18 KB description pulled into the page."""
        where, args = self._browse_where(statuses, min_score, query, source, remote_canada,
                                         exclude_french, french_sql, posted_after, hide_zero,
                                         reach_only)
        cols = ", ".join(self.BROWSABLE)
        if french_sql and french_sql != "0":
            cols += f", CASE WHEN {french_sql} THEN 1 ELSE 0 END AS needs_french"
        sql = (f"SELECT {cols} FROM postings{where}"
               f" ORDER BY score IS NULL, {_RANK_SQL} DESC, company ASC, title ASC"
               " LIMIT ? OFFSET ?")
        rows = self._conn.execute(sql, args + [int(limit), int(offset)]).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["remote"] = bool(d.get("remote"))
            d["needs_french"] = bool(d.get("needs_french"))
            out.append(d)
        return out

    def browse_count(self, **kw) -> int:
        """How many postings the same filters select."""
        limit = kw.pop("limit", None)
        offset = kw.pop("offset", None)
        where, args = self._browse_where(
            kw.get("statuses"), kw.get("min_score"), kw.get("query"), kw.get("source"),
            kw.get("remote_canada", False), kw.get("exclude_french", False),
            kw.get("french_sql", "0"), kw.get("posted_after"), kw.get("hide_zero", False),
            kw.get("reach_only", False))
        del limit, offset
        return int(self._conn.execute(f"SELECT COUNT(*) FROM postings{where}", args).fetchone()[0])

    def sources(self) -> list[str]:
        """The distinct source names, for the source filter."""
        return [r[0] for r in self._conn.execute(
            "SELECT DISTINCT source FROM postings WHERE source IS NOT NULL "
            "ORDER BY source ASC").fetchall() if r[0]]



    def stats(self) -> dict:
        by_status = {s: 0 for s in STATUSES}
        for r in self._conn.execute(
                "SELECT status, COUNT(*) AS n FROM postings GROUP BY status").fetchall():
            by_status[r["status"]] = r["n"]
        total = sum(by_status.values())
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(timespec="seconds")
        new_24h = self._conn.execute(
            "SELECT COUNT(*) FROM postings WHERE first_seen >= ?", (cutoff,)).fetchone()[0]
        return {"by_status": by_status, "total": total, "new_24h": new_24h}

    # -- funnel and follow-up reads (read-only; empty tables give empty results) ------
    def funnel_counts(self) -> dict:
        """{status: count} over every posting; every STATUSES key is present, even at 0."""
        counts = {s: 0 for s in STATUSES}
        for r in self._conn.execute(
                "SELECT status, COUNT(*) AS n FROM postings GROUP BY status").fetchall():
            counts[r["status"]] = r["n"]
        return counts

    def variant_stats(self) -> list[dict]:
        """Per resume variant: total postings, 'applied' (applied or any later outcome),
        and 'responded' (interview, offer or rejected). Variant None is kept as None."""
        rows = self._conn.execute(
            "SELECT variant, COUNT(*) AS total,"
            " SUM(CASE WHEN status IN ('applied','interview','offer','rejected')"
            "     THEN 1 ELSE 0 END) AS applied,"
            " SUM(CASE WHEN status IN ('interview','offer','rejected')"
            "     THEN 1 ELSE 0 END) AS responded"
            " FROM postings GROUP BY variant ORDER BY total DESC, variant ASC").fetchall()
        return [{"variant": r["variant"], "total": r["total"],
                 "applied": r["applied"] or 0, "responded": r["responded"] or 0}
                for r in rows]

    def due_follow_ups(self, through_date: str) -> list[dict]:
        """Postings whose follow_up_at is on or before through_date (YYYY-MM-DD)."""
        rows = self._conn.execute(
            "SELECT * FROM postings WHERE follow_up_at IS NOT NULL AND follow_up_at <= ?"
            " ORDER BY follow_up_at ASC, uid ASC", (through_date,)).fetchall()
        return [_row_to_dict(r) for r in rows]

    def close(self) -> None:
        self._conn.close()
