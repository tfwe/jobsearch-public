"""jobsearch CLI: scan, rescore, list, show, status, stats, sources, prep, letter, dashboard.

Prep-only: nothing here sends email or submits a form.

jobsearch.letter and jobsearch.dashboard are imported lazily, inside the functions that use
them, so this module loads whether or not those siblings exist yet.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from jobsearch.match import dedupe_postings, is_eligible, load_preferences, load_profile, score_posting
from jobsearch.registry import load_registry, verify_registry
from jobsearch.sources import _AGGREGATORS, fetch_all
from jobsearch.store import Store, default_db_path

STATUSES = (
    "new", "shortlisted", "prepped", "applied", "interview",
    "offer", "rejected", "closed", "skipped",
)

# The optional resume project this tool can build PDFs from. Set JOBS_RESUME_DIR to
# point it elsewhere, or leave it: only the `letter` and `prep` commands use it.
RESUME_DIR = Path(os.environ.get("JOBS_RESUME_DIR") or (Path.home() / "resume"))
BUILD_SH = RESUME_DIR / "build.sh"
RESUME_OUT = RESUME_DIR / "out"
# Runtime files sit beside the database (JOBS_DB), the same convention dashboard.py uses for
# its default packets directory, so the links it writes point at the packets written here.
_DATA_DIR = Path(default_db_path()).parent
PACKETS_DIR = _DATA_DIR / "packets"
DASHBOARD_PATH = _DATA_DIR / "dashboard.html"
LAST_SCAN_PATH = _DATA_DIR / "last_scan.json"
BOARD_HEALTH_PATH = _DATA_DIR / "board_health.json"

# Build target -> PDF it leaves in RESUME_OUT. The build script prints no path, so this
# is the map. Edit the names to match the PDFs your own build produces.
PDF_NAMES = {
    "main": "Resume_Main.pdf",
    "general": "Resume_General.pdf",
    "ai-security": "Resume_AI_Security.pdf",
    "sec-content": "Resume_Sec_Content.pdf",
    "ai-platform": "Resume_AI_Platform.pdf",
}

# Every variant the board offers for a packet, in the order the workspace menu lists them.
# "main" is the source build, not a choice: the four PDF_NAMES targets that are choices plus
# the two ladder-only variants.
VARIANTS = ("general", "ai-security", "sec-content", "ai-platform", "ml-engineer",
            "sec-automation")

# Variants with no build.sh target, mapped to the ladder of targets to try in order.
# Ladder rungs must be keys of PDF_NAMES.
FALLBACK_LADDER = {
    "ml-engineer": ["ai-platform", "general"],
    "sec-automation": ["sec-content", "general"],
}


def _build_ladder(variant: str) -> list[str]:
    """Build targets to try, in order, for a posting's variant."""
    if variant in PDF_NAMES:
        return [variant] if variant == "general" else [variant, "general"]
    return FALLBACK_LADDER.get(variant, ["general"])


# ---------- helpers ----------

def _err(msg: str) -> None:
    print(msg, file=sys.stderr)


def _positive_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected an integer, got {text!r}")
    if value <= 0:
        raise argparse.ArgumentTypeError(f"must be > 0, got {value}")
    return value


def _reasons(row: dict) -> list[str]:
    raw = row.get("reasons")
    if isinstance(raw, list):
        return [str(r) for r in raw]
    if isinstance(raw, str) and raw:
        try:
            val = json.loads(raw)
            return [str(r) for r in val] if isinstance(val, list) else [raw]
        except json.JSONDecodeError:
            return [raw]
    return []


def _print_table(headers: list[str], rows: list[list[str]]) -> None:
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    fmt = " | ".join(f"{{:<{w}}}" for w in widths)
    print(fmt.format(*headers))
    print("-+-".join("-" * w for w in widths))
    for row in rows:
        print(fmt.format(*row))


def _slug(uid: str) -> str:
    return uid.replace("/", "_").replace(":", "_")


def _run_build(variant: str) -> tuple[int, str, str]:
    """Run the resume build script for one variant. Returns (returncode, stdout, stderr)."""
    try:
        proc = subprocess.run(
            [str(BUILD_SH), variant],
            capture_output=True, text=True, cwd=str(RESUME_DIR), timeout=900,
        )
    except subprocess.TimeoutExpired as exc:
        return 1, exc.stdout or "", f"build.sh {variant} timed out"
    except FileNotFoundError:
        return 1, "", f"build.sh not found at {BUILD_SH}; set JOBS_RESUME_DIR to the resume project that has it"
    except OSError as exc:
        return 1, "", f"build.sh {variant} could not run: {exc.strerror or exc}"
    return proc.returncode, proc.stdout, proc.stderr


def _posting_md(row: dict, variant: str) -> str:
    reasons = _reasons(row)
    lines = [
        f"# {row.get('title') or '(untitled)'}",
        "",
        f"- Company: {row.get('company') or '(unknown)'}",
        f"- Location: {row.get('location') or '(none given)'}",
        f"- URL: {row.get('url') or '(none)'}",
        f"- Resume variant: {variant}",
        f"- Score: {float(row.get('score') or 0):.1f}",
        "- Reasons: " + ("; ".join(reasons) if reasons else "(none)"),
        "",
        "## Description",
        "",
        row.get("description") or "(no description)",
        "",
    ]
    return "\n".join(lines)


_NO_LIMIT = 10 ** 9  # Store.list needs an int limit; effectively unbounded.


def _eligibility_kwargs(include_us: bool) -> dict:
    """Extra kwargs for is_eligible. Passed only when --include-us is set, so the default call is unchanged."""
    return {"include_us": True} if include_us else {}


def _source_health(entries: list[dict], postings: list[dict], errors: list[str]) -> list[dict]:
    """One row per board: ok, and a detail that is the error text or the posting count."""
    counts = Counter(":".join(str(p.get("uid", "")).split(":")[:2]) for p in postings)
    out = []
    for e in entries:
        ats = str(e.get("ats") or "")
        token = str(e.get("token") or e.get("name") or "")
        label = f"{ats}:{token}"
        errs = [x for x in errors if x.startswith(label + ":")]
        count = counts.get(label, 0)
        if errs:
            detail = errs[0][len(label) + 2:]
        else:
            detail = f"{count} postings"
        out.append({"name": str(e.get("name") or token), "ats": ats,
                    "ok": (not errs) and count > 0, "detail": detail})
    return out


def _load_board_health() -> dict[str, int]:
    """'<ats>:<token>' -> consecutive failed-or-empty scans. Missing, unreadable or corrupt -> empty."""
    try:
        raw = json.loads(BOARD_HEALTH_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {str(k): v for k, v in raw.items() if isinstance(v, int) and not isinstance(v, bool) and v >= 0}


def _record_board_health(entries: list[dict], postings: list[dict], errors: list[str]) -> dict[str, int]:
    """Advance the per-board dead-scan counter for every registry entry. A board that failed or
    returned 0 postings gets +1; one that returned postings resets to 0 (key kept, so flapping shows).
    Writes BOARD_HEALTH_PATH (created if absent). Returns only the keys with a count >= 1."""
    counts = _load_board_health()
    per_board = Counter(":".join(str(p.get("uid", "")).split(":")[:2]) for p in postings)
    for e in entries:
        label = f"{str(e.get('ats') or '')}:{str(e.get('token') or e.get('name') or '')}"
        failed = any(x.startswith(label + ":") for x in errors)
        if failed or per_board.get(label, 0) == 0:
            counts[label] = counts.get(label, 0) + 1
        else:
            counts[label] = 0
    try:
        BOARD_HEALTH_PATH.parent.mkdir(parents=True, exist_ok=True)
        BOARD_HEALTH_PATH.write_text(json.dumps(counts, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except OSError as exc:
        _err(f"warning: could not write {BOARD_HEALTH_PATH}: {exc}")
    return {k: v for k, v in counts.items() if v >= 1}


def _write_last_scan(last_scan: dict) -> None:
    LAST_SCAN_PATH.parent.mkdir(parents=True, exist_ok=True)
    LAST_SCAN_PATH.write_text(json.dumps(last_scan, indent=2) + "\n", encoding="utf-8")


def _load_last_scan() -> dict | None:
    try:
        return json.loads(LAST_SCAN_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _render_dashboard(last_scan: dict | None, out_path: str | None = None) -> str:
    from jobsearch.dashboard import render_dashboard  # lazy: sibling module
    return render_dashboard(default_db_path(), str(out_path or DASHBOARD_PATH), last_scan=last_scan)


def _refresh_dashboard_quietly(last_scan: dict | None) -> None:
    """Auto-refresh after scan/rescore. A failure here warns on stderr; the run itself still succeeds."""
    try:
        _render_dashboard(last_scan)
    except Exception as exc:
        _err(f"warning: dashboard not refreshed: {exc}")


def _publish_scan(last_scan: dict) -> None:
    try:
        _write_last_scan(last_scan)
    except OSError as exc:
        _err(f"warning: could not write {LAST_SCAN_PATH}: {exc}")
    _refresh_dashboard_quietly(last_scan)


# ---------- commands ----------

def cmd_scan(args: argparse.Namespace) -> int:
    registry = load_registry()
    postings, errors = fetch_all(registry, include_aggregators=not args.no_aggregators)
    profile = load_profile()
    ekw = _eligibility_kwargs(args.include_us)
    prefs = load_preferences()  # once per run: every row reuses it instead of re-reading the file

    scored = []
    n_eligible = 0
    for p in postings:
        eligible, why = is_eligible(p, prefs=prefs, **ekw)
        if eligible:
            n_eligible += 1
            score, variant, reasons = score_posting(p, profile, prefs=prefs, **ekw)
        else:
            score, variant, reasons = 0.0, "general", [why]
        scored.append((p["uid"], score, variant, reasons))

    store = Store()
    try:
        backfilled = store.backfill_dedupe_keys()
        new, seen = store.upsert_postings(postings)
        backfilled += store.backfill_dedupe_keys()
        for uid, score, variant, reasons in scored:
            store.set_score(uid, score, variant, reasons)
        # Fetch unlimited so the repost collapse runs before --limit; a repost cannot take a slot.
        rows = store.list(status="new", min_score=args.min_score, limit=_NO_LIMIT)
        rows = [
            r for r in rows
            if r.get("status") == "new" and float(r.get("score") or 0) >= args.min_score
        ]
        rows.sort(key=lambda r: (-float(r.get("score") or 0), r.get("company") or "", r.get("title") or ""))
        rows = dedupe_postings(rows)[: args.limit]
    finally:
        store.close()

    sources_entries = list(registry) + ([] if args.no_aggregators else list(_AGGREGATORS))
    last_scan = {
        "scanned_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "fetched": len(postings),
        "new": new,
        "updated": seen,
        "eligible": n_eligible,
        "sources": _source_health(sources_entries, postings, errors),
        "problems": list(errors),
        "dead_scans": _record_board_health(registry, postings, errors),
    }
    _publish_scan(last_scan)

    if args.json:
        out = [
            {
                "score": float(r.get("score") or 0), "variant": r.get("variant") or "",
                "company": r.get("company") or "", "title": r.get("title") or "",
                "location": r.get("location") or "", "uid": r["uid"], "url": r.get("url") or "",
            }
            for r in rows
        ]
        print(json.dumps(out, indent=2))
        for e in errors:
            _err(e)
        return 0

    if rows:
        _print_table(
            ["score", "variant", "company", "title", "location", "uid"],
            [
                [f"{float(r.get('score') or 0):.1f}", r.get("variant") or "", r.get("company") or "",
                 r.get("title") or "", r.get("location") or "", r["uid"]]
                for r in rows
            ],
        )
    else:
        print(f"no new postings at or above score {args.min_score:g}")
    backfill_note = f", {backfilled} dedupe keys backfilled" if backfilled else ""
    print(f"fetched {len(postings)} postings, {new} new, {seen} already seen{backfill_note}")
    if errors:
        print("sources with problems:")
        for e in errors:
            print(e)
    return 0


def cmd_rescore(args: argparse.Namespace) -> int:
    """Re-run the current scorer over every stored row. Fetches nothing; touches only score,
    variant and reasons. --dry-run computes the same counts and writes nothing."""
    profile = load_profile()
    prefs = load_preferences()  # once per run; passed to score_posting for every row
    ekw = _eligibility_kwargs(args.include_us)
    store = Store()
    try:
        # limit=-1: SQLite treats a negative LIMIT as no limit, so every row is loaded.
        rows = store.list(limit=-1)
        rescored = eligible = flipped = 0
        for row in rows:
            ok, why = is_eligible(row, prefs=prefs, **ekw)
            if ok:
                score, variant, reasons = score_posting(row, profile, prefs=prefs, **ekw)
                eligible += 1
            else:
                score, variant, reasons = 0.0, "general", [why]
            old = float(row.get("score") or 0)
            if old > 0 and not ok:
                flipped += 1
            if not args.dry_run:
                store.set_score(row["uid"], score, variant, reasons)
            rescored += 1
    finally:
        store.close()
    prefix = "dry run, nothing written: " if args.dry_run else ""
    print(f"{prefix}rescored {rescored} postings, {eligible} eligible, "
          f"{flipped} flipped from positive score to ineligible")
    if not args.dry_run:
        _refresh_dashboard_quietly(_load_last_scan())
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    if args.status is not None and args.status not in STATUSES:
        _err(f"unknown status {args.status!r}; expected one of: {', '.join(STATUSES)}")
        return 1
    store = Store()
    try:
        rows = store.list(status=args.status, min_score=args.min_score,
                          query=args.q, limit=args.limit)
    finally:
        store.close()
    if args.json:
        print(json.dumps(rows, indent=2, default=str))
        return 0
    if not rows:
        print("no matching postings")
        return 0
    _print_table(
        ["score", "status", "company", "title", "location", "uid"],
        [
            [f"{float(r.get('score') or 0):.1f}", r.get("status") or "", r.get("company") or "",
             r.get("title") or "", r.get("location") or "", r["uid"]]
            for r in rows
        ],
    )
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    store = Store()
    try:
        row = store.get(args.uid)
        if row is None:
            _err(f"no such posting: {args.uid}")
            return 1
        events = store.events(args.uid)
    finally:
        store.close()
    reasons = _reasons(row)
    print(f"uid:        {row['uid']}")
    print(f"company:    {row.get('company') or ''}")
    print(f"title:      {row.get('title') or ''}")
    print(f"location:   {row.get('location') or ''}")
    print(f"url:        {row.get('url') or ''}")
    print(f"source:     {row.get('source') or ''}")
    print(f"posted:     {row.get('posted_at') or ''}")
    print(f"remote:     {bool(row.get('remote'))}")
    print(f"status:     {row.get('status') or ''}")
    print(f"score:      {float(row.get('score') or 0):.1f}")
    print(f"variant:    {row.get('variant') or ''}")
    print(f"reasons:    {'; '.join(reasons)}")
    print(f"first seen: {row.get('first_seen') or ''}")
    print(f"last seen:  {row.get('last_seen') or ''}")
    if row.get("notes"):
        print(f"notes:      {row['notes']}")
    print("")
    print(row.get("description") or "(no description)")
    if events:
        print("")
        print("events:")
        for ev in events:
            detail = f" {ev.get('detail')}" if ev.get("detail") else ""
            print(f"  {ev.get('ts') or ''} {ev.get('kind') or ''}{detail}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    if args.state not in STATUSES:
        _err(f"unknown status {args.state!r}; expected one of: {', '.join(STATUSES)}")
        return 1
    store = Store()
    try:
        row = store.get(args.uid)
        if row is None:
            _err(f"no such posting: {args.uid}")
            return 1
        old = row.get("status") or ""
        try:
            store.set_status(args.uid, args.state, note=args.note or "")
        except ValueError as exc:
            _err(str(exc))
            return 1
    finally:
        store.close()
    print(f"{args.uid}: {old} -> {args.state}")
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    store = Store()
    try:
        s = store.stats()
    finally:
        store.close()
    by = s.get("by_status") or {}
    print(f"total:          {s.get('total', 0)}")
    print(f"new in last 24h: {s.get('new_24h', 0)}")
    print("by status:")
    names = list(STATUSES) + [k for k in by if k not in STATUSES]
    for name in names:
        print(f"  {name:<12} {by.get(name, 0)}")
    return 0


def cmd_sources(args: argparse.Namespace) -> int:
    entries = load_registry()
    if not args.check:
        _print_table(
            ["name", "ats", "token", "verified", "tags"],
            [[e["name"], e["ats"], e["token"], "yes" if e["verified"] else "no",
              ",".join(e["tags"])] for e in entries],
        )
        return 0
    results = verify_registry(entries)
    _print_table(
        ["ok", "name", "ats", "token", "count", "detail"],
        [["OK" if r["ok"] else "FAIL", r["name"], r["ats"], r["token"],
          str(r["count"]), r["detail"]] for r in results],
    )
    bad = sum(1 for r in results if not r["ok"])
    print(f"{len(results) - bad}/{len(results)} boards OK")
    return 0


def cmd_prep(args: argparse.Namespace) -> int:
    letter_body = None
    if getattr(args, "letter", None):
        letter_body = Path(args.letter).expanduser()
        if not letter_body.is_file():
            _err(f"body file not found: {letter_body}")
            return 1
    store = Store()
    try:
        row = store.get(args.uid)
        if row is None:
            _err(f"no such posting: {args.uid}")
            return 1
        variant = row.get("variant") or "general"
        ladder = _build_ladder(variant)

        used = None
        for target in ladder:
            rc, _out, errtxt = _run_build(target)
            if rc == 0:
                used = target
                break
            _err(f"build.sh {target} failed (exit {rc})")
            if errtxt:
                _err(errtxt.rstrip())
        if used is None:
            return 1

        pdf_src = RESUME_OUT / PDF_NAMES[used]
        if not pdf_src.is_file():
            _err(f"build reported success but {pdf_src} is missing")
            return 1

        packet = PACKETS_DIR / _slug(args.uid)
        packet.mkdir(parents=True, exist_ok=True)
        pdf_dst = packet / "resume.pdf"
        shutil.copyfile(pdf_src, pdf_dst)
        (packet / "posting.md").write_text(_posting_md(row, used), encoding="utf-8")
        letter_pdf = None
        if letter_body is not None:
            from jobsearch.letter import render_letter  # lazy: sibling module
            # Any failure here propagates to main(): exit 1, status left unchanged.
            letter_pdf = render_letter(args.uid, str(letter_body), out_dir=str(packet))
        store.set_status(args.uid, "prepped", note=f"packet staged ({used} resume)")
    finally:
        store.close()

    print(f"packet dir: {packet}")
    print(f"resume pdf: {pdf_dst}")
    if letter_pdf is not None:
        print(f"cover letter: {letter_pdf}")
    if used != variant:
        print(f"note: no {variant} build; used {used}")
    print("nothing was sent or submitted; apply yourself from the packet")
    return 0


def cmd_letter(args: argparse.Namespace) -> int:
    body = Path(args.body_file).expanduser()
    if not body.is_file():
        _err(f"body file not found: {body}")
        return 1
    store = Store()
    try:
        row = store.get(args.uid)
    finally:
        store.close()
    if row is None:
        _err(f"no such posting: {args.uid}")
        return 1
    from jobsearch.letter import render_letter  # lazy: sibling module
    print(render_letter(args.uid, str(body)))
    return 0


def cmd_dashboard(args: argparse.Namespace) -> int:
    out = Path(args.out).expanduser() if args.out else DASHBOARD_PATH
    print(_render_dashboard(_load_last_scan(), out_path=str(out)))
    return 0


# ---------- entry point ----------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="jobsearch", description="Prep-only job discovery tool.")
    sub = p.add_subparsers(dest="cmd", required=True,
                           metavar="{scan,list,show,status,stats,sources,prep,rescore,letter,dashboard}")

    s = sub.add_parser("scan", help="fetch boards, score, and show new ranked postings")
    s.add_argument("--limit", type=_positive_int, default=40)
    s.add_argument("--min-score", type=float, default=15.0,
                   help="minimum score to show (default: %(default)s)")
    s.add_argument("--no-aggregators", action="store_true")
    s.add_argument("--include-us", action="store_true",
                   help="also accept US postings (relocation assumed, +1)")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_scan)

    rs = sub.add_parser("rescore", help="re-score every stored posting with the current rules (no fetch)")
    rs.add_argument("--dry-run", action="store_true",
                    help="report the counts without writing anything")
    rs.add_argument("--include-us", action="store_true",
                    help="also accept US postings (relocation assumed, +1)")
    rs.set_defaults(func=cmd_rescore)

    ls = sub.add_parser("list", help="list stored postings")
    ls.add_argument("--status")
    ls.add_argument("--min-score", type=float)
    ls.add_argument("--q", metavar="TEXT")
    ls.add_argument("--limit", type=_positive_int, default=50)
    ls.add_argument("--json", action="store_true")
    ls.set_defaults(func=cmd_list)

    sh = sub.add_parser("show", help="show one posting in full")
    sh.add_argument("uid")
    sh.set_defaults(func=cmd_show)

    st = sub.add_parser("status", help="set a posting's status")
    st.add_argument("uid")
    st.add_argument("state")
    st.add_argument("--note", default="")
    st.set_defaults(func=cmd_status)

    sub.add_parser("stats", help="counts by status").set_defaults(func=cmd_stats)

    so = sub.add_parser("sources", help="show the company registry")
    so.add_argument("--check", action="store_true", help="live-validate every board")
    so.set_defaults(func=cmd_sources)

    pr = sub.add_parser("prep", help="build resume and stage a packet (never sends)")
    pr.add_argument("uid")
    pr.add_argument("--letter", metavar="PATH",
                    help="plain-text cover-letter body; also renders cover_letter.pdf into the packet")
    pr.set_defaults(func=cmd_prep)

    lt = sub.add_parser("letter", help="render a cover-letter PDF from a body file (never sends)")
    lt.add_argument("uid")
    lt.add_argument("--body-file", required=True, metavar="PATH")
    lt.set_defaults(func=cmd_letter)

    db = sub.add_parser("dashboard", help="write the HTML dashboard and print its path")
    db.add_argument("--out", metavar="PATH", help="default: ~/jobsearch/dashboard.html")
    db.set_defaults(func=cmd_dashboard)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except Exception as exc:  # real failure: report on stderr, exit 1
        _err(f"error: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
