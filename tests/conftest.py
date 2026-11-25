"""Suite-wide isolation from the operator's real files.

preferences.yaml is user data under the resume directory. load_preferences() reads it by default,
so the moment an operator has one, every test that does not pass prefs= starts answering from
live preferences and the suite goes red for the right reason but in the wrong place. Point the
module default at a path that never exists for the duration of a test.
"""
import pytest

from jobsearch import copilot, letter, match


@pytest.fixture(autouse=True)
def no_real_preferences(monkeypatch, tmp_path):
    """Run every test as if the operator had no preferences.yaml."""
    monkeypatch.setattr(match, "DEFAULT_PREFS_PATH", str(tmp_path / "absent-preferences.yaml"))


@pytest.fixture(autouse=True)
def no_real_facts_location(monkeypatch, tmp_path):
    """Run every test as if no facts.yaml were on the machine.

    The location-blocker tests assert the DEFAULT_CITY fallback, so leaving the
    operator's own facts.yaml visible makes them answer from live data.
    """
    monkeypatch.setattr(letter, "DEFAULT_FACTS", str(tmp_path / "absent-facts.yaml"))
    monkeypatch.setattr(copilot, "DEFAULT_FACTS_PATH", str(tmp_path / "absent-facts.yaml"))
