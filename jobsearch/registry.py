"""companies.yaml registry: load, save, and live-verify ATS board entries."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import yaml

DEFAULT_REGISTRY = str(Path(__file__).resolve().parents[1] / "companies.yaml")
KNOWN_ATS = ("greenhouse", "lever", "ashby", "smartrecruiters", "workable")
_REQUIRED = ("name", "ats", "token")


def _normalise(e: dict) -> dict:
    return {
        "name": str(e["name"]),
        "ats": str(e["ats"]),
        "token": str(e["token"]),
        "tags": [str(t) for t in (e.get("tags") or [])],
        "verified": bool(e.get("verified", False)),
    }


def load_registry(path: str = DEFAULT_REGISTRY) -> list[dict]:
    """Parse companies.yaml into a list of entry dicts. Raises ValueError on bad shape."""
    with open(path, encoding="utf-8") as fh:
        doc = yaml.safe_load(fh) or {}
    raw = doc.get("entries") if isinstance(doc, dict) else None
    if not isinstance(raw, list):
        raise ValueError(f"{path}: expected a top-level 'entries' list")
    out: list[dict] = []
    for i, e in enumerate(raw, 1):
        if not isinstance(e, dict):
            raise ValueError(f"{path}: entry {i} is not a mapping")
        missing = [k for k in _REQUIRED if not e.get(k)]
        if missing:
            raise ValueError(f"{path}: entry {i} ({e.get('name')!r}) missing {', '.join(missing)}")
        if str(e["ats"]) not in KNOWN_ATS:
            raise ValueError(f"{path}: entry {i} ({e['name']!r}) has unknown ats {e['ats']!r}")
        out.append(_normalise(e))
    return out


def save_registry(entries: list[dict], path: str | None = None) -> None:
    """Rewrite the registry as plain YAML (comments are not preserved). Atomic replace."""
    target = Path(path or DEFAULT_REGISTRY)
    doc = {"entries": [_normalise(e) for e in entries]}
    text = yaml.safe_dump(doc, sort_keys=False, default_flow_style=False, allow_unicode=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=".companies-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, target)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def verify_registry(entries: list[dict]) -> list[dict]:
    """Live-check every entry against its ATS board. Never raises for a bad board."""
    from jobsearch.sources import fetch_board  # lazy: load/save work without network modules

    results: list[dict] = []
    for e in entries:
        base = {"name": e.get("name", ""), "ats": e.get("ats", ""), "token": e.get("token", "")}
        try:
            count = len(fetch_board(e))
        except Exception as exc:  # one dead board must not stop the check
            results.append({**base, "ok": False, "count": 0, "detail": str(exc)})
            continue
        ok = count > 0
        detail = "" if ok else "0 postings (dead board?)"
        results.append({**base, "ok": ok, "count": count, "detail": detail})
    return results
