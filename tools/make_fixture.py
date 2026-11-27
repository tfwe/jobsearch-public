#!/usr/bin/env python3
"""Build a synthetic SQLite store from the captured board payloads in samples/, offline.

Each sample is a response body captured from one public board. The fixture replays those
bodies through the repository's own parse-and-normalise path by replacing the single
network call the fetch layer makes (sources._get_json), so what lands in the store is
exactly what a live scan would have written from the same bytes. The synthetic corpus in
demo/corpus/ (built by tools/build_demo_corpus.py) is added on top, so the store has
in-scope postings to rank. Every stored row is then scored by the same pass `rescore`
runs, against the synthetic profile in tests/fixture_facts.yaml -- the same file the
scoring tests use.

    python tools/make_fixture.py [out.db]      # default: fixture/jobsearch-fixture.db

Deterministic: same samples in, same rows out. No network, no API key, and no dependency
on the machine's own profile or preferences files.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from jobsearch import match, sources  # noqa: E402
from jobsearch import store as store_module  # noqa: E402
from jobsearch.store import Store  # noqa: E402

SAMPLES = ROOT / "samples"
CORPUS = ROOT / "demo" / "corpus" / "corpus.json"
DEFAULT_OUT = ROOT / "fixture" / "jobsearch-fixture.db"

# (payload, second payload, fetch call). Each sample file holds one captured record; the
# envelope below is the shape the corresponding fetch function reads, since the raw
# response body is not the record. The second payload is the per-posting detail body
# smartrecruiters fetches separately.
REPLAY = [
    ("greenhouse.json", None, "jobs", lambda: sources.fetch_greenhouse("sample", "Sample Corp")),
    ("lever.json", None, "list", lambda: sources.fetch_lever("sample", "Sample Corp")),
    ("ashby.json", None, "jobs", lambda: sources.fetch_ashby("sample", "Sample Corp")),
    ("workable.json", None, "raw", lambda: sources.fetch_workable("sample", "Sample Corp")),
    ("smartrecruiters.json", "smartrecruiters_detail.json", "raw",
     lambda: sources.fetch_smartrecruiters("sample", "Sample Corp")),
    ("remotive.json", None, "jobs", lambda: sources.fetch_remotive()),
    ("remoteok.json", None, "list", lambda: sources.fetch_remoteok()),
    ("arbeitnow.json", None, "data", lambda: sources.fetch_arbeitnow()),
]

DETAIL_URL = re.compile(r"/postings/[^/?]+$")  # the per-posting call, not the list call


def _envelope(record: object, shape: str) -> object:
    """Wrap one captured record in the body shape its board returns."""
    if shape == "raw":
        return record
    if shape == "list":
        return [record]
    return {shape: [record]}


def _replay(main: object, detail: object):
    """A stand-in for sources._get_json that answers from the captured bytes."""
    def _get_json(url: str, retries: int = 2) -> object:
        if detail is not None and DETAIL_URL.search(url):
            return detail
        return main
    return _get_json


def build(out_path: Path, quiet: bool = False) -> int:
    """Write the fixture and return the number of postings inserted."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm"):  # a stale WAL would shadow the new file
        stale = Path(str(out_path) + suffix)
        if stale.exists():
            stale.unlink()

    store = Store(str(out_path))
    real_get_json = sources._get_json
    # Pin the clock: _now() stamps last_seen/first_seen at second resolution, so an unpinned
    # build differs whenever two runs straddle a second boundary. One fixed instant makes two
    # runs byte-identical no matter when they run; staleness is a wall-clock property of the
    # real pipeline and is intentionally frozen here.
    real_now = store_module._now
    store_module._now = lambda: "2026-11-01T00:00:00+00:00"
    inserted = 0
    try:
        for name, detail_name, shape, call in REPLAY:
            record = json.loads((SAMPLES / name).read_text(encoding="utf-8"))
            main = _envelope(record, shape)
            detail = None
            if detail_name:
                detail = json.loads((SAMPLES / detail_name).read_text(encoding="utf-8"))
            sources._get_json = _replay(main, detail)
            postings = call()
            new, updated = store.upsert_postings(postings)
            inserted += new
            if not quiet:
                print(f"{name:28s} {len(postings):4d} parsed {new:4d} new {updated:4d} refreshed")

        corpus = json.loads(CORPUS.read_text(encoding="utf-8"))["postings"]
        new, updated = store.upsert_postings(corpus)
        inserted += new
        if not quiet:
            print(f"{'corpus.json':28s} {len(corpus):4d} parsed {new:4d} new {updated:4d} refreshed")

        # The same pass `rescore` runs, against the synthetic profile the tests use. The
        # preferences are loaded from a path that does not exist, which yields the empty
        # defaults: the demo's numbers must not depend on the machine's own preferences.yaml.
        profile = match.load_profile(str(ROOT / "tests" / "fixture_facts.yaml"))
        prefs = match.load_preferences(str(ROOT / "tests" / "no-preferences-here.yaml"))
        rows = store.list(limit=-1)
        positive = 0
        for row in rows:
            score, variant, reasons = match.score_posting(row, profile, prefs=prefs)
            store.set_score(row["uid"], score, variant, reasons)
            if score > 0:
                positive += 1
        if not quiet:
            print(f"scored {len(rows):4d} postings against tests/fixture_facts.yaml "
                  f"({positive} above 0.0)")
    finally:
        sources._get_json = real_get_json
        store_module._now = real_now
        store.close()
    if not quiet:
        print(f"{str(out_path):28s} {inserted:4d} postings written")
    return inserted


def main(argv: list[str]) -> int:
    out = Path(argv[1]) if len(argv) > 1 else DEFAULT_OUT
    build(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
