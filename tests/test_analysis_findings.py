"""Tests for analysis.md parsing: sections, paragraph findings, titles, estimates.

Pure-function tests over literal text. No browser, no filesystem, no network.
"""
from __future__ import annotations

from jobsearch.analysis_findings import parse_analysis, split_sections

SAMPLE = """VERDICT
no chance: the role requires "5+ years across security engineering" plus partner security sign-off, whereas the applicant has one short security role and no LLM red-teaming record.

FIT
In projects (Aurora Ledger Reconciliation Pipeline, Beacon Agent Operations Layer), the applicant builds Python tooling and automated test harnesses, including 900 tests in pytest. This aligns with strong Python.

In employment (Example Corp, Security Content Engineer), the applicant brings foundational security operations and AI training.

GAPS
First, production tenure: the role requires 5+ years securing production systems, while facts.yaml proves one short security role and nothing longer.

Second, LLM red-teaming and guardrails: facts.yaml does not prove red-teaming LLM agents for jailbreaks.

Third, enterprise security reviews: facts.yaml shows no partner security questionnaires brought to sign-off.

TO_BE_PERFECT
Over three to four months, publish a Python repository for a mock travel-booking LLM agent. Meeting the seniority bar honestly requires three to five years in production roles.
"""


def test_real_shape_sample_parses_into_verdict_strengths_and_gaps():
    result = parse_analysis(SAMPLE)
    assert result["ok"] is True
    kinds = [f["type"] for f in result["findings"]]
    assert kinds == ["decide", "strength", "strength", "gap", "gap", "gap"]
    assert result["findings"][0]["title"] == "Verdict"
    assert result["findings"][0]["posting"] is None
    assert result["findings"][0]["facts"] is None


def test_gap_with_marker_splits_into_posting_and_facts():
    gap = parse_analysis(SAMPLE)["findings"][3]
    assert gap["title"] == "Production tenure"
    assert gap["body"] == ""
    assert gap["posting"].startswith("the role requires 5+ years securing production systems")
    assert not gap["posting"].endswith(",")
    assert gap["facts"].startswith("one short security role")
    assert gap["facts"].endswith("and nothing longer.")


def test_gap_without_marker_keeps_body_and_posting_is_none():
    gap = parse_analysis(SAMPLE)["findings"][4]
    assert gap["type"] == "gap"
    assert gap["title"] == "LLM red-teaming and guardrails"
    assert gap["posting"] is None
    assert gap["facts"] is None
    assert gap["body"].startswith("facts.yaml does not prove red-teaming")


def test_fit_paragraphs_are_strengths_and_unnamed_titles_are_numbered():
    strengths = [f for f in parse_analysis(SAMPLE)["findings"] if f["type"] == "strength"]
    assert len(strengths) == 2
    assert strengths[0]["title"] == "Strength 1"
    assert strengths[1]["title"] == "Strength 2"
    assert strengths[1]["body"].startswith("In employment")


def test_heading_only_text_is_not_ok():
    assert parse_analysis("VERDICT\n\nFIT\n\nGAPS\n") == {"ok": False, "findings": [], "perfect": None}


def test_none_and_empty_are_not_ok():
    assert parse_analysis(None) == {"ok": False, "findings": [], "perfect": None}
    assert parse_analysis("") == {"ok": False, "findings": [], "perfect": None}


def test_prose_without_headings_is_not_ok():
    assert parse_analysis("Just a paragraph about the job.\n\nAnother one.")["ok"] is False


def test_to_be_perfect_estimate_is_the_matched_phrase():
    perfect = parse_analysis(SAMPLE)["perfect"]
    assert perfect["estimate"] == "three to four months"
    assert perfect["text"].startswith("Over three to four months")


def test_to_be_perfect_without_a_duration_has_empty_estimate():
    text = SAMPLE.replace("Over three to four months,", "Start soon,")
    text = text.replace("three to five years", "a long time")
    assert parse_analysis(text)["perfect"]["estimate"] == ""


def test_absent_to_be_perfect_section_gives_none():
    text = SAMPLE.split("TO_BE_PERFECT")[0]
    assert parse_analysis(text)["perfect"] is None


def test_ordinal_stripping_and_colon_title_rule():
    text = ("GAPS\n"
            "Fifth, bonus skills and public proof: OWASP Top 10 is unproven.\n\n"
            "2) rollback plans: nothing proves a launch.\n\n"
            "The team wants speed and a long list of unrelated requirements here.")
    findings = parse_analysis(text)["findings"]
    assert findings[0]["title"] == "Bonus skills and public proof"
    assert findings[0]["body"] == "OWASP Top 10 is unproven."
    assert findings[1]["title"] == "Rollback plans"
    assert findings[1]["body"] == "nothing proves a launch."
    assert findings[2]["title"] == "Gap 3"
    assert findings[2]["body"].startswith("The team wants speed")


def test_colon_beyond_80_characters_is_not_a_title():
    long_lead = "Lead " + "word " * 20 + "here: and the rest of the sentence goes on."
    finding = parse_analysis("GAPS\n" + long_lead)["findings"][0]
    assert finding["title"] == "Gap 1"
    assert finding["body"] == long_lead


def test_split_sections_ignores_lowercase_and_unknown_headings():
    text = "fit\nlowercase is body\n\nFIT\nreal body\n\nNOTES\nnot a heading\n\nGAPS\ngap body"
    sections = split_sections(text)
    assert list(sections) == ["FIT", "GAPS"]
    assert sections["FIT"] == "real body\n\nNOTES\nnot a heading"
    assert sections["GAPS"] == "gap body"


def test_split_sections_none_is_empty():
    assert split_sections(None) == {}


def test_blank_line_runs_collapse_and_trailing_spaces_strip():
    text = "FIT   \nfirst paragraph.   \n\n\n\nsecond paragraph.\n\n\nGAPS\nx"
    assert split_sections(text)["FIT"] == "first paragraph.\n\nsecond paragraph."


def test_output_is_unescaped_plain_text():
    text = "GAPS\nFirst, tooling: R&D & CI <blue> pipelines lack proof, while facts.yaml shows R&D & CI <blue> is absent."
    gap = parse_analysis(text)["findings"][0]
    assert "&" in gap["posting"] and "<blue>" in gap["posting"]
    assert "&amp;" not in gap["facts"]
    assert gap["title"] == "Tooling"


def test_extract_verdict_level_parses_strong_stretch_no_chance_and_malformed():
    from jobsearch.analysis_findings import extract_verdict_level

    def doc(first_line):
        return f"VERDICT\n\n{first_line}\nMore detail follows.\n\nFIT\n\n- a point\n"

    assert extract_verdict_level(doc("Strong fit: apply.")) == (
        "strong", "Strong fit: apply.")
    assert extract_verdict_level(doc("**Stretch** — senior above your level.")) == (
        "stretch", "**Stretch** — senior above your level.")
    assert extract_verdict_level(doc("No chance at this level.")) == (
        "no_chance", "No chance at this level.")
    assert extract_verdict_level(doc("NO CHANCE")) == ("no_chance", "NO CHANCE")
    # Malformed: the line is kept for display but no level is claimed.
    assert extract_verdict_level(doc("Maybe, depends on the team.")) == (
        None, "Maybe, depends on the team.")
    assert extract_verdict_level(doc("Strongly considering it")) == (
        None, "Strongly considering it")
    # Missing, empty or headingless input never raises.
    assert extract_verdict_level(None) == (None, "")
    assert extract_verdict_level("") == (None, "")
    assert extract_verdict_level("VERDICT\n\n\nFIT\n- x\n") == (None, "")
    assert extract_verdict_level("just prose, no headings") == (None, "")
