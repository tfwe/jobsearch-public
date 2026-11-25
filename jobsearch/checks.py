"""Read-only posting checks for the UI: language blockers, seniority flags, skill marks.

Nothing here writes, scores or changes a status. The French test is defined once, as
term lists, and rendered two ways: as a Python predicate for one posting and as an SQL
expression for the list filter. The filter is a superset of the row flag: it also hides a
posting that mentions a French phrase the row flag does not label.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timezone

# The French test is a curated phrase list, not a word-pair. A word pair matched
# "English required, German or French is a bonus" as a hard blocker on nearly every
# Cloudflare posting; these phrases only fire where the posting asks for French.
# One list, matched as a lowercase substring in Python and as a LIKE in SQL. The list
# filter is a superset of the row flag (see french_mentions).
FRENCH_PHRASES = (
    "french is required", "french required", "french fluency", "fluent in french",
    "french speaking", "french-speaking", "spoken french", "written french",
    "french and english", "english and french", "french language", "french/english",
    "english/french", "french & english", "proficiency in french", "fluency in french",
    "in french and english", "french and english is required",
    "bilingual french", "french bilingual", "bilingual in french",
    "bilingual in english and french", "maîtrise du français", "maitrise du francais",
    "maîtrise du français et de l'anglais", "français requis", "francais requis",
    "français obligatoire", "francais obligatoire", "français et anglais",
    "anglais et français", "bilingue français", "bilingue francais",
)


# A hit whose sentence also carries one of these is a preference, not a blocker.
FRENCH_SOFTENERS = ("a plus", "an asset", "is a bonus", "nice to have", "nice-to-have",
                    "an advantage", "not required", "desirable", "optional", "preferred")


def french_sql(column: str = "description") -> str:
    """An SQL predicate true when the posting looks French-required (phrase list above).

    Every quote in a phrase is doubled so the pattern cannot end the string literal.
    """
    likes = " OR ".join(
        "lower(%s) LIKE '%%%s%%'" % (column, p.replace("'", "''")) for p in FRENCH_PHRASES)
    return f"({likes})"


def french_mentions(text: str | None) -> bool:
    """The phrase match the SQL filter uses: any French-requirement phrase anywhere.

    It is a superset of french_required(): a sentence with a softener still matches here,
    which is why the list filter can hide a "French is preferred" posting that the row
    flag does not label. Filtering such a row out is deliberate: a French requirement is
    still not a clean fit when the profile records no French.
    """
    low = (text or "").lower()
    return any(phrase in low for phrase in FRENCH_PHRASES)


def french_required(text: str | None) -> bool:
    """True when the text carries a French-requirement phrase. Never guesses."""
    return french_quote(text) != ""


_SENTENCE_ENDS = (".", "!", "?", "\n")


def french_quote(text: str | None, limit: int = 190) -> str:
    """The first sentence that asks for French, trimmed; '' when there is none.

    A sentence that also carries a softener ('French is a bonus') is skipped: the
    posting mentions French without requiring it, which is not a blocker.
    """
    low = (text or "").lower()
    if not low:
        return ""
    for m in re.finditer("|".join(re.escape(p) for p in FRENCH_PHRASES), low):
        start = max(low.rfind("\n", 0, m.start()), 0)
        stops = [low.find(ch, m.end()) for ch in _SENTENCE_ENDS]
        stops = [s for s in stops if s != -1]
        end = min(stops) + 1 if stops else len(low)
        start = max(start, low.rfind(". ", 0, m.start()) + 2)
        sentence = " ".join((text or "")[start:end].split())
        if any(s in sentence.lower() for s in FRENCH_SOFTENERS):
            continue
        if len(sentence) > limit:
            sentence = sentence[: limit - 1].rstrip() + "…"
        return sentence
    return ""



# ---------- language of the posting body ----------
# A posting can be locked to one language without ever asking for it: it can simply be
# written in French. Function words are the cheapest honest signal, and a strict
# threshold keeps an English posting with a French availability line out of it.
FR_STOP = frozenset("le la les des du de nous vous et pour dans avec sur au aux que qui "
                    "est sont une un une ce cette ces vos votre notre nos être etre plus "
                    "par pas ou où tout tous toute".split())
EN_STOP = frozenset("the and of to in for with you we is are a an on our your that this "
                    "be as at will from by or not have has it its their they".split())


def looks_french(text: str | None) -> bool:
    """True when the posting text reads as French: many French function words, few English."""
    words = re.findall(r"[a-zà-ÿ']+", (text or "")[:6000].lower())
    if len(words) < 80:
        return False
    fr = sum(1 for w in words if w in FR_STOP)
    en = sum(1 for w in words if w in EN_STOP)
    return fr >= 40 and fr > 1.6 * en


# ---------- seniority ----------

_SENIORITY = (
    ("Principal", re.compile(r"\bprincipal\b", re.I)),
    ("Staff", re.compile(r"\bstaff\b", re.I)),
    ("Director", re.compile(r"\bdirector\b", re.I)),
    ("Head of", re.compile(r"\bhead of\b", re.I)),
    ("Architect", re.compile(r"\barchitect\b", re.I)),
    ("Senior", re.compile(r"\bsenior\b", re.I)),
    ("Lead", re.compile(r"\blead\b", re.I)),
    ("Manager", re.compile(r"\bmanager\b", re.I)),
    ("Intern/co-op", re.compile(r"\b(intern|internship|co-op)\b", re.I)),
)


def seniority_flag(title: str | None) -> str | None:
    """The seniority word in a title that the scorer penalizes, else None.

    Junior and new-grad are excluded: the scorer rewards those titles.
    """
    for label, rx in _SENIORITY:
        if rx.search(title or ""):
            return label
    return None


# ---------- skills ----------

def matched_skills(text: str | None, skills) -> list[str]:
    """Profile skill tokens that appear in the text, longest first, deduped.

    Matching is word-bounded and case-insensitive, the same rule the scorer uses, so a
    highlight means the scorer could see it too.
    """
    low = (text or "").lower()
    hits = []
    for token in sorted({str(t).strip().lower() for t in skills if str(t).strip()},
                        key=len, reverse=True):
        if re.search(r"(?<![a-z0-9])" + re.escape(token) + r"(?![a-z0-9])", low):
            hits.append(token)
    return hits


def resume_coverage(posting_text: str | None, resume_text: str | None, tokens) -> dict:
    """How many of the profile tokens the posting mentions also appear on the resume.

    Both sides go through matched_skills, so a hit means what the scorer would see.
    With no resume text the result is unavailable and nothing is claimed about it;
    with no posting token at all the ratio is 1.0, so nothing reads as missing.
    """
    target = matched_skills(posting_text, list(tokens))
    if not (resume_text or "").strip():
        return {"covered": [], "missing": target, "ratio": 0.0 if target else 1.0,
                "available": False}
    covered = matched_skills(resume_text, target)
    missing = [t for t in target if t not in covered]
    ratio = len(covered) / len(target) if target else 1.0
    return {"covered": covered, "missing": missing, "ratio": ratio, "available": True}


def highlight(text: str, skills, esc) -> str:
    """Escape the posting text, then wrap matched skills in <mark>.

    Works on the escaped string so the escaping happens exactly once and never on
    markup this function adds.
    """
    escaped = esc(text or "")
    tokens = sorted({str(t).strip().lower() for t in skills if str(t).strip()},
                    key=len, reverse=True)
    taken: list[tuple[int, int]] = []
    spans: list[tuple[int, int]] = []
    for token in tokens:
        pattern = re.compile(r"(?<![A-Za-z0-9])" + re.escape(esc(token)) + r"(?![A-Za-z0-9])",
                             re.I)
        for m in pattern.finditer(escaped):
            if any(m.start() < e and s < m.end() for s, e in taken):
                continue
            spans.append((m.start(), m.end()))
            taken.append((m.start(), m.end()))
    out, cursor = [], 0
    for start, end in sorted(spans):
        out.append(escaped[cursor:start])
        out.append(f"<mark>{escaped[start:end]}</mark>")
        cursor = end
    out.append(escaped[cursor:])
    return "".join(out)


def apply_gate(blockers: list[dict]) -> dict:
    """One decision for the submit step: blocked, caution or clear.

    Rows with no severity count as hard, so an untagged row can never let a submit
    through silently. The external posting link is never removed by any decision.
    """
    hard = sum(1 for b in blockers if b.get("severity", "hard") == "hard")
    soft = sum(1 for b in blockers if b.get("severity") == "soft")
    if hard:
        decision = "blocked"
        summary = f"Do not apply: {hard} hard blocker{'s' if hard != 1 else ''}."
        if soft:
            summary += f" {soft} soft flag{'s' if soft != 1 else ''} as well."
    elif soft:
        decision = "caution"
        summary = f"Proceed with caution: {soft} soft flag{'s' if soft != 1 else ''}."
    else:
        decision = "clear"
        summary = "Nothing blocking."
    return {"decision": decision, "hard_count": hard, "soft_count": soft, "summary": summary}


# ---------- dates ----------

def posted_days(posted_at: str | None, now: datetime | None = None) -> int | None:
    """Whole days since the posting date, or None when the date is missing or unparseable."""
    if not posted_at:
        return None
    try:
        d = date.fromisoformat(str(posted_at)[:10])
    except ValueError:
        return None
    today = (now or datetime.now(timezone.utc)).date()
    return max(0, (today - d).days)


def posted_phrase(posted_at: str | None, now: datetime | None = None) -> str:
    """'posted today', 'posted 3 days ago', or 'posting date unknown' when the date is unknown."""
    days = posted_days(posted_at, now)
    if days is None:
        return "posting date unknown"
    if days == 0:
        return "posted today"
    if days == 1:
        return "posted yesterday"
    return f"posted {days} days ago"
