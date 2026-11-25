"""Tests for jobsearch.checks: the French phrase test, seniority flags, highlights, ages.

The French predicate is the one thing on the screen that reads a posting's prose, so it
is pinned here: the SQL form and the Python form must select exactly the same postings.
"""
from __future__ import annotations

import sqlite3

import pytest

from jobsearch.checks import (french_mentions, french_quote, french_required, french_sql,
                              highlight, looks_french, matched_skills, posted_days,
                              posted_phrase, seniority_flag)


def test_sql_and_python_agree_on_a_corpus(tmp_path):
    texts = [
        "Professional working proficiency in French & English is required.",
        "English required, German or French is a bonus.",
        "Bilingual in English and Cantonese.",
        "Nice-to-have: high degree of French language proficiency.",
        "Excellente maitrise du francais et de l'anglais.",
        "Nous cherchons une personne bilingue francais anglais.",
        "Constructs pipelines in Python and SQL.",
        "",
    ]
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE postings (uid INTEGER, description TEXT)")
    db.executemany("INSERT INTO postings VALUES (?, ?)", list(enumerate(texts)))
    sql_hits = {row[0] for row in db.execute(f"SELECT uid FROM postings WHERE {french_sql()}")}
    py_hits = {i for i, text in enumerate(texts) if french_mentions(text)}
    assert sql_hits == py_hits
    assert 0 in sql_hits          # the explicit requirement
    assert 1 not in sql_hits      # "is a bonus"
    assert 2 not in sql_hits      # bilingual, but not French


def test_french_sql_survives_an_apostrophe():
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE t (description TEXT)")
    # The phrase list carries "l'anglais"; the builder must not end the string literal.
    assert db.execute(f"SELECT COUNT(*) FROM t WHERE {french_sql()}").fetchone()[0] == 0


def test_french_quote_returns_the_asking_sentence():
    text = ("About us. We build things. Professional working proficiency in French and "
            "English is required. Benefits follow.")
    assert french_quote(text) == ("Professional working proficiency in French and English "
                                 "is required.")


def test_french_quote_skips_a_softened_sentence():
    text = "Requirements: Python. Fluency in French is a plus. Benefits follow."
    assert french_quote(text) == ""


def test_french_quote_trims_a_long_sentence():
    text = "French is required " + ("very " * 80) + "."
    quote = french_quote(text)
    assert len(quote) <= 190 and quote.endswith("…")


def test_flag_is_stricter_than_the_filter():
    softened = "Fluency in French is a plus."
    assert french_mentions(softened) is True
    assert french_required(softened) is False


@pytest.mark.parametrize("title,flag", [
    ("Principal Security Engineer", "Principal"),
    ("Staff Platform Engineer", "Staff"),
    ("Senior Detection Engineer", "Senior"),
    ("Security Engineering Intern", "Intern/co-op"),
    ("Junior SOC Analyst", None),      # junior is a bonus in the scorer, not a blocker
    ("Backend Engineer", None),
    ("Technical Lead", "Lead"),
])
def test_seniority_flag(title, flag):
    assert seniority_flag(title) == flag


def test_matched_skills_is_word_bounded_and_ordered():
    text = "Kubernetes and Python, plus kubernetes operators."
    assert matched_skills(text, ["python", "kubernetes", "go"]) == ["kubernetes", "python"]
    # 'go' must not match inside 'google' or 'governance'.
    assert matched_skills("google governance", ["go"]) == []


def test_highlight_escapes_and_marks():
    text = "<script>alert(1)</script> Kubernetes rules"
    out = highlight(text, ["kubernetes"], lambda s: s.replace("<", "&lt;").replace(">", "&gt;"))
    assert "<script>" not in out
    assert "&lt;script&gt;" in out
    assert "<mark>Kubernetes</mark>" in out


def test_highlight_never_nests_marks():
    out = highlight("data engineering", ["data", "data engineering"],
                    lambda s: s)
    assert out.count("<mark>") == 1


def test_posted_days_and_phrase():
    from datetime import datetime, timezone
    now = datetime(2026, 11, 23, tzinfo=timezone.utc)
    assert posted_days("2026-11-23", now) == 0
    assert posted_days("2026-11-01", now) == 22
    assert posted_days(None, now) is None
    assert posted_days("not a date", now) is None
    assert posted_phrase("2026-11-23", now) == "posted today"
    assert posted_phrase("2026-11-22", now) == "posted yesterday"
    assert posted_phrase("2026-11-01", now) == "posted 22 days ago"
    assert posted_phrase(None, now) == "posting date unknown"


def test_looks_french_on_a_french_body_and_not_on_an_english_one():
    french = ("Nous recherchons une personne pour le deploiement de nos modeles. "
              "Vous travaillez avec les equipes et vous assurez que les clients sont "
              "satisfaits. Le poste est base a Montreal, dans nos bureaux. ") * 4
    english = ("We are looking for an engineer to deploy our models with customers. "
               "You will work with the team and make sure the results are useful. "
               "The role is based in Montreal, in our offices. ") * 4
    assert looks_french(french) is True
    assert looks_french(english) is False
    assert looks_french("short text") is False
    assert looks_french(None) is False


def test_resume_coverage_splits_covered_and_missing():
    from jobsearch.checks import resume_coverage
    posting = "Build Docker and Python pipelines. Python a must."
    got = resume_coverage(posting, "Shipped Python services on Kubernetes.",
                          ["python", "docker", "go"])
    assert got == {"covered": ["python"], "missing": ["docker"], "ratio": 0.5,
                   "available": True}
    # Word-bounded like matched_skills: 'go' inside 'google' is not a posting token.
    assert resume_coverage("Google hiring", "", ["go"])["available"] is False
    empty = resume_coverage("Nothing relevant here.", "Python", ["python", "docker"])
    assert empty == {"covered": [], "missing": [], "ratio": 1.0, "available": True}


def test_resume_coverage_is_unavailable_without_a_resume():
    from jobsearch.checks import resume_coverage
    for resume in (None, "", "   "):
        got = resume_coverage("Build Docker and Python.", resume, ["python", "docker"])
        assert got["available"] is False
        assert got["covered"] == []
        assert sorted(got["missing"]) == ["docker", "python"]
    assert resume_coverage(None, None, ["python"]) == {
        "covered": [], "missing": [], "ratio": 1.0, "available": False}
