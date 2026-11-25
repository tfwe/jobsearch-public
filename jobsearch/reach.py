"""Seniority reach: is a posting within reach of an early-career candidate? Stdlib only.

classify() reads the title (level) and the description (years asked, via match.required_years).
The verdict is a deterministic label; the store persists level and level_years so the browse
filter and the AI-call gate never need to re-read descriptions.
"""
from __future__ import annotations

import re

from jobsearch.match import _fold, required_years

LEVELS = ("intern", "entry", "mid", "senior", "staff", "principal", "lead", "manager", "director",
          "architect")
OUT_OF_REACH_LEVELS = frozenset({"staff", "principal", "lead", "manager", "director", "architect"})
# Years of experience the example profile shows. serve.py quotes it when it explains the
# reach blocker; the gate itself judges a posting by the years the posting asks for.
PROFILE_YEARS = 1

_INTERN = re.compile(r"\b(intern|internship|co-op)\b")
_ENTRY = re.compile(r"\b(junior|new grad|graduate|entry[ -]level)\b")
# Senior group, each word mapped to the level it implies. Any hit here wins over intern/entry.
# Executive titles (chief, C-suite, VP) map to "director", the most senior executive level.
_GROUP = {  # the most senior level present wins: LEVELS order (architect > director > manager > ...)
    "architect": re.compile(r"\barchitect\b"),
    "director": re.compile(r"\b(director|chief|ciso|cto|cio|cso|cfo|coo|vp|svp|evp)\b"),
    "manager": re.compile(r"\bmanager\b"),
    "lead": re.compile(r"\b(lead|head of)\b"),
    "principal": re.compile(r"\bprincipal\b"),
    "staff": re.compile(r"\bstaff\b"),
    "senior": re.compile(r"\b(senior|sr)\b\.?"),
}


def title_level(title: str) -> str:
    """Seniority level from a job title. The senior group (see _GROUP) decides first, most senior
    word wins; intern/entry/junior decide only when no senior-group word is present; else mid."""
    t = _fold(title or "")
    hits = [lvl for lvl, rx in _GROUP.items() if rx.search(t)]
    if hits:
        return max(hits, key=LEVELS.index)
    if _INTERN.search(t):
        return "intern"
    if _ENTRY.search(t):
        return "entry"
    return "mid"


def classify(title: str, description: str) -> dict:
    """Verdict for one posting: out_of_reach, stretch or in_reach, with the causes in `why`."""
    level = title_level(title)
    years = required_years(description or "")
    out_level = level in OUT_OF_REACH_LEVELS
    out_years = years is not None and years >= 5
    if out_level or out_years:
        verdict = "out_of_reach"
    elif level == "senior" or years in (3, 4):
        verdict = "stretch"
    else:
        verdict = "in_reach"

    causes = []
    if out_level:
        causes.append(f"{level}-level title")
    elif level == "senior":
        causes.append("senior title")
    if out_years:
        causes.append(f"asks {years}+ years")
    elif years in (3, 4):
        causes.append(f"asks {years} years")
    why = "" if verdict == "in_reach" else ", ".join(causes)

    return {"level": level, "years": years, "verdict": verdict,
            "in_reach": verdict == "in_reach", "why": why}
