"""Unit tests for jobsearch.staleness: mtime comparison, no network, no real files."""
from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from jobsearch import staleness

# Local-time epochs so the expected day in the sentence holds in any timezone.
OLD = datetime(2026, 11, 20, 12, 0).timestamp()
BUILT = datetime(2026, 11, 22, 12, 0).timestamp()
NEW = datetime(2026, 11, 24, 9, 30).timestamp()


def _file(tmp_path: Path, name: str, when: float) -> Path:
    path = tmp_path / name
    path.write_bytes(b"x")
    os.utime(path, (when, when))
    return path


def test_mtime_is_none_for_missing_path(tmp_path):
    assert staleness.mtime(tmp_path / "nope") is None
    assert staleness.mtime(None) is None


def test_mtime_returns_epoch_seconds(tmp_path):
    path = _file(tmp_path, "a.pdf", BUILT)
    assert staleness.mtime(path) == BUILT


def test_stale_sources_newer_facts_file_gives_one_reason_naming_facts(tmp_path):
    built = [_file(tmp_path, "resume.pdf", BUILT)]
    facts = _file(tmp_path, "facts.yaml", NEW)
    reasons = staleness.stale_reasons(built, [("facts.yaml", facts)])
    assert reasons == ["facts.yaml changed on 2026-11-24, after this packet was built"]


def test_stale_sources_newer_pdfs_give_no_reasons(tmp_path):
    built = [_file(tmp_path, "resume.pdf", NEW)]
    facts = _file(tmp_path, "facts.yaml", BUILT)
    variant = _file(tmp_path, "variant.pdf", OLD)
    assert staleness.stale_reasons(built, [("facts.yaml", facts),
                                           ("the general resume", variant)]) == []


def test_stale_sources_missing_source_is_ignored(tmp_path):
    built = [_file(tmp_path, "resume.pdf", BUILT)]
    gone = tmp_path / "facts.yaml"           # never created
    assert staleness.stale_reasons(built, [("facts.yaml", gone)]) == []


def test_stale_sources_missing_built_file_is_ignored(tmp_path):
    facts = _file(tmp_path, "facts.yaml", NEW)
    built = [tmp_path / "resume.pdf"]        # never created
    assert staleness.stale_reasons(built, [("facts.yaml", facts)]) == []


def test_stale_sources_empty_built_means_nothing_compiled_yet(tmp_path):
    facts = _file(tmp_path, "facts.yaml", NEW)
    assert staleness.stale_reasons([], [("facts.yaml", facts)]) == []


def test_stale_sources_within_tolerance_are_not_stale(tmp_path):
    built = [_file(tmp_path, "resume.pdf", BUILT)]
    facts = _file(tmp_path, "facts.yaml", BUILT + staleness.TOLERANCE_S)
    assert staleness.stale_reasons(built, [("facts.yaml", facts)]) == []
    later = _file(tmp_path, "later.yaml", BUILT + staleness.TOLERANCE_S + 1)
    assert len(staleness.stale_reasons(built, [("later", later)])) == 1


def test_stale_sources_keep_the_order_given_and_name_each_label(tmp_path):
    built = [_file(tmp_path, "resume.pdf", BUILT)]
    facts = _file(tmp_path, "facts.yaml", NEW)
    variant = _file(tmp_path, "variant.pdf", NEW)
    reasons = staleness.stale_reasons(built, [("the general resume", variant),
                                              ("facts.yaml", facts)])
    assert [r.split(" changed")[0] for r in reasons] == ["the general resume", "facts.yaml"]


def test_newest_built_file_is_the_yardstick(tmp_path):
    old_pdf = _file(tmp_path, "letter.pdf", OLD)
    new_pdf = _file(tmp_path, "resume.pdf", BUILT)
    facts = _file(tmp_path, "facts.yaml", BUILT + 60)
    assert len(staleness.stale_reasons([old_pdf, new_pdf], [("facts.yaml", facts)])) == 1
    assert staleness.stale_reasons([old_pdf], [("facts.yaml", facts)]) == [
        "facts.yaml changed on 2026-11-22, after this packet was built"]
