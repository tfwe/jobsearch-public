"""Parse a packet's analysis.md into findings the job board can render.

The analysis is written by hand-shaped templates: four uppercase headings
(VERDICT, FIT, GAPS, TO_BE_PERFECT), each followed by blank-line separated
paragraphs. This module turns that into plain, unescaped data. It deliberately
does not escape HTML: the caller owns escaping, and doing it here would
double-escape once the template layer escapes again.

Splitting is done by regex and string search, not by a markdown parser, because
the files are not general markdown and a parser would reflow the very line
structure (blank-line paragraphs) the rules depend on.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

HEADINGS: tuple[str, ...] = ("VERDICT", "FIT", "GAPS", "TO_BE_PERFECT")

# Only these names are headings. Other all-caps lines are body text, because
# an analysis can legitimately shout a word (e.g. NIST) on its own line.
_ORDINAL_RE = re.compile(
    r"^(?:(?:first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth),\s+"
    r"|\d+[.)]\s+)",
    re.IGNORECASE,
)
_ESTIMATE_RE = re.compile(
    r"(?:over |about |roughly |around |within )?"
    r"((?:one|two|three|four|five|six|seven|eight|nine|ten|\d+)"
    r"(?: (?:to|-|or) (?:one|two|three|four|five|six|\d+))? (?:weeks?|months?|years?))"
)
# Order matters: the first marker found in this list wins, not the earliest position.
_SPLIT_MARKERS: tuple[str, ...] = (
    " while facts.yaml ",
    " whereas facts.yaml ",
    " while the applicant ",
    " whereas the applicant ",
    " but facts.yaml ",
    " and facts.yaml ",
)
_MIN_SPLIT_LEN = 40
_TITLE_COLON_LIMIT = 80


@dataclass
class Finding:
    """One rendered row. ``type`` is decide | strength | gap."""

    type: str
    title: str
    body: str
    posting: str | None = None
    facts: str | None = None

    def as_dict(self) -> dict:
        return {"type": self.type, "title": self.title, "posting": self.posting,
                "facts": self.facts, "body": self.body}


def _normalise(text: str) -> str:
    """Normalise line endings, trailing spaces and runs of blank lines.

    Runs of blank lines are collapsed to a single blank line because paragraph
    boundaries are defined by blank lines; leaving 3+ newlines would create
    phantom empty paragraphs.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in text.split("\n")]
    text = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", text)


def split_sections(text: str | None) -> dict[str, str]:
    """Return {heading: body} for the recognised headings, in file order.

    Body text is stripped; internal blank lines are kept so paragraphs survive.
    A repeated heading keeps its first position and collects every body under it, in
    file order: each occurrence is appended to the same list.
    """
    if not text:
        return {}
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in _normalise(text).split("\n"):
        stripped = line.strip()
        if stripped in HEADINGS:
            current = stripped
            sections.setdefault(current, [])
            continue
        if current is not None:
            sections[current].append(line)
    return {name: "\n".join(lines).strip() for name, lines in sections.items()}


def _paragraphs(body: str) -> list[str]:
    return [p.strip() for p in re.split(r"\n{2,}", body) if p.strip()]


def _title_and_body(paragraph: str, fallback: str, index: int) -> tuple[str, str]:
    """Split ``paragraph`` into (title, body) using the colon rule.

    The ordinal is stripped first so "First, production tenure: ..." titles as
    "Production tenure", not "First, production tenure". Without a colon near the
    start there is no reliable label, so the title falls back to a numbered
    generic name and the body keeps the whole paragraph.
    """
    stripped = _ORDINAL_RE.sub("", paragraph, count=1)
    colon = stripped.find(":")
    if colon == -1 or colon >= _TITLE_COLON_LIMIT:
        return f"{fallback} {index}", paragraph
    title = stripped[:colon].strip().rstrip(",").strip()
    if title:
        title = title[0].upper() + title[1:]
    remainder = stripped[colon + 1:].strip()
    # Lowercase only a capitalised ordinary word ("The role"), not an acronym
    # ("NIST") or a single capital, since the colon split started mid-sentence.
    if len(remainder) > 1 and remainder[0].isupper() and remainder[1].islower():
        remainder = remainder[0].lower() + remainder[1:]
    return title or f"{fallback} {index}", remainder


def _split_posting_facts(body: str) -> tuple[str | None, str | None, str]:
    """Return (posting, facts, body) by splitting on the first known marker.

    Splitting only happens on a real contrast marker and a body long enough to
    hold two clauses; a short paragraph is better left whole than cut badly.
    """
    if len(body) < _MIN_SPLIT_LEN:
        return None, None, body
    lowered = body.lower()
    for marker in _SPLIT_MARKERS:
        idx = lowered.find(marker)
        if idx == -1:
            continue
        posting = body[:idx].strip().rstrip(",").strip()
        facts = body[idx + len(marker):].strip()
        if not posting:
            return None, None, body
        # The marker is the contrast word, so a leftover "and " / ", " would
        # only be a join artefact; drop it rather than reword the facts.
        if facts[:4].lower() == "and ":
            facts = facts[4:].strip()
        elif facts.startswith(", "):
            facts = facts[2:].strip()
        # The card's label already says "Your facts say", so the joining verb ("proves",
        # "shows") reads as a contradiction there. Drop it, keep every word it governs.
        for verb in ("proves ", "shows "):
            if facts[:len(verb)].lower() == verb:
                facts = facts[len(verb):].strip()
                break
        return posting, facts, ""
    return None, None, body


def parse_analysis(text: str | None) -> dict:
    """Return {"ok", "findings", "perfect"} for an analysis.md body.

    ``ok`` is True only when FIT or GAPS yields at least one finding: a file
    with only a verdict or only headings is not something the board can show as
    an analysis, so it is reported as not ok with empty results.
    """
    sections = split_sections(text)
    findings: list[Finding] = []
    counts = {"strength": 0, "gap": 0}

    for name, body in sections.items():
        if name == "VERDICT" and body:
            findings.append(Finding(type="decide", title="Verdict", body=body))
        elif name in ("FIT", "GAPS"):
            kind = "strength" if name == "FIT" else "gap"
            label = "Strength" if kind == "strength" else "Gap"
            for paragraph in _paragraphs(body):
                counts[kind] += 1
                title, rest = _title_and_body(paragraph, label, counts[kind])
                posting, facts, rest = _split_posting_facts(rest)
                findings.append(Finding(type=kind, title=title, body=rest,
                                        posting=posting, facts=facts))

    feature_findings = [f for f in findings if f.type in ("strength", "gap")]
    if not feature_findings:
        return {"ok": False, "findings": [], "perfect": None}

    perfect = None
    if "TO_BE_PERFECT" in sections:
        section_text = sections["TO_BE_PERFECT"]
        match = _ESTIMATE_RE.search(section_text)
        perfect = {"text": section_text, "estimate": match.group(1) if match else ""}

    return {"ok": True, "findings": [f.as_dict() for f in findings], "perfect": perfect}


# Checked in order; the first phrase the first VERDICT line starts with wins.
_VERDICT_PHRASES: tuple[tuple[str, str], ...] = (
    ("no chance", "no_chance"),
    ("stretch", "stretch"),
    ("strong", "strong"),
)


def extract_verdict_level(text: str | None) -> tuple[str | None, str]:
    """Return (level, first_line) for the VERDICT section of an analysis.md body.

    ``level`` is "no_chance", "stretch" or "strong" when the first non-empty VERDICT
    line starts with that phrase (case-insensitive, leading markdown punctuation
    ignored), else None. ``first_line`` is that line as written, or "" when the
    section is missing or empty.
    """
    body = split_sections(text).get("VERDICT", "")
    first = next((line.strip() for line in body.split("\n") if line.strip()), "")
    if not first:
        return None, ""
    low = re.sub(r"^[^a-z]+", "", first.lower())
    for phrase, level in _VERDICT_PHRASES:
        if re.match(re.escape(phrase) + r"\b", low):
            return level, first
    return None, first
