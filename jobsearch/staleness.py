"""Stale-packet check: a source file newer than the PDFs built from it.

Pure comparison by mtime. The only I/O is Path.stat(); nothing here raises on a
missing or unreadable file.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

# A source must be this many seconds newer than the newest built file to count;
# filesystems round mtimes, and a copy made moments after a build is not a change.
TOLERANCE_S = 2.0


def mtime(path) -> float | None:
    """Epoch seconds, or None for a missing/unreadable path. Never raises."""
    try:
        return Path(path).stat().st_mtime
    except (OSError, TypeError, ValueError):
        return None


def stale_reasons(built: list[Path], sources: list[tuple[str, Path]]) -> list[str]:
    """One sentence per source newer than the newest file in `built`, in the order given.

    `built` empty (nothing compiled yet) -> []. A missing source or built file is skipped,
    never an error. 'Newer' means > TOLERANCE_S seconds after the newest built mtime.
    Each sentence names the source label and the date it changed, e.g.
    "facts.yaml changed on 2026-11-24, after this packet was built".
    """
    built_times = [t for t in (mtime(p) for p in built) if t is not None]
    if not built_times:
        return []
    newest = max(built_times)
    reasons = []
    for label, path in sources:
        when = mtime(path)
        if when is None or when <= newest + TOLERANCE_S:
            continue
        day = datetime.fromtimestamp(when).date().isoformat()
        reasons.append(f"{label} changed on {day}, after this packet was built")
    return reasons
