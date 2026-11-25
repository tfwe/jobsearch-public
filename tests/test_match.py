import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from jobsearch.match import (VARIANTS, is_eligible, load_profile, score_posting,  # noqa: E402
                             us_sponsorship_unstated)

FACTS = str(Path(__file__).parent / "fixture_facts.yaml")

US_ONLY = (False, "outside Canada (US-only)")


def P(**kw):
    base = {"uid": "t:x:1", "source": "greenhouse", "company": "Acme", "title": "", "location": "",
            "url": "", "description": "", "posted_at": "", "remote": False}
    base.update(kw)
    return base


SEC_DESC = ("Write KQL detections and Splunk Enterprise Security analytics rules. Own SOAR playbooks "
            "and Elastic SIEM content. Python required.")

# Ramp's real benefits boilerplate (stored description). Not a hiring location.
RAMP_BENEFITS = ("Build agents for finance teams. Benefits: Canada - Group Medical, Dental, and Vision "
                 "coverage through Sun Life.")


def test_load_profile_fixture_file_nonempty():
    prof = load_profile(FACTS)
    assert prof["tokens"], "token set must not be empty"
    assert {"python", "kql", "pydantic", "pytorch"} <= prof["skills"]
    assert "security content engineer" in prof["titles"]
    assert all(t == t.lower() for t in prof["tokens"])


def test_load_profile_missing_file_falls_back():
    prof = load_profile("/nonexistent/facts.yaml")
    assert prof["tokens"] and prof["skills"] and prof["titles"]


def test_security_content_engineer_scores_high_sec_content():
    prof = load_profile(FACTS)
    post = P(title="Security Content Engineer", location="Winnipeg, MB, Canada", description=SEC_DESC)
    score, variant, reasons = score_posting(post, prof)
    assert variant == "sec-content"
    assert score >= 40
    assert any("security content" in r for r in reasons)


def test_senior_scores_lower_but_eligible():
    prof = load_profile(FACTS)
    plain = P(title="Detection Engineer", location="Remote - Canada", description=SEC_DESC)
    senior = P(title="Senior Detection Engineer", location="Remote - Canada", description=SEC_DESC)
    assert is_eligible(senior) == (True, "")
    s_plain, _, _ = score_posting(plain, prof)
    s_senior, _, _ = score_posting(senior, prof)
    assert 0 <= s_senior < s_plain


def test_sales_engineer_security_not_eligible():
    ok, reason = is_eligible(P(title="Sales Engineer, Security", location="Toronto", description="x"))
    assert ok is False
    assert reason == "role family out of scope"
    assert score_posting(P(title="Sales Engineer, Security"), load_profile(FACTS))[0] == 0.0


def test_detroit_us_work_authorization_not_eligible():
    post = P(title="Security Engineer", location="Detroit, MI",
             description="You must be authorized to work in the United States.")
    assert is_eligible(post) == (False, "US work authorization required")


def test_same_wording_with_remote_canada_stays_eligible():
    post = P(title="Security Engineer", location="Remote - Canada",
             description="You must be authorized to work in the United States.")
    assert is_eligible(post) == (True, "")


def test_work_auth_rule_not_triggered_by_title_alone():
    post = P(title="Security Engineer (US Citizen Preferred)", location="Toronto, ON",
             description="Build detection content in KQL.")
    assert is_eligible(post)[0] is True


def test_remote_us_only_with_citizenship_is_not_eligible():
    post = P(title="Security Engineer", location="Remote - US",
             description="Must be a U.S. citizen. Security clearance required.")
    assert is_eligible(post) == (False, "US work authorization required")


def test_score_is_deterministic_and_bounded():
    prof = load_profile(FACTS)
    posts = [
        P(title="Security Content Engineer", location="Winnipeg, MB, Canada", description=SEC_DESC),
        P(title="AI Engineer", location="Remote", description="LLM agents, LoRA, PyTorch, Python."),
        P(title="Junior Security Analyst", location="Ottawa", description="SOC analyst, Sentinel."),
        P(title="Recruiter", location="", description=""),
        P(title="", location="", description=""),
    ]
    for post in posts:
        a = score_posting(post, prof)
        b = score_posting(post, prof)
        assert a == b
        score, variant, reasons = a
        assert isinstance(score, float)
        assert 0.0 <= score <= 100.0
        assert variant in VARIANTS
        assert isinstance(reasons, list)


def test_general_fallback_when_no_family_hits():
    score, variant, _ = score_posting(P(title="Helpdesk Technician", location="Winnipeg",
                                        description="Office IT support."), load_profile(FACTS))
    assert variant == "general"
    assert 0.0 <= score <= 100.0


def test_ai_platform_variant():
    post = P(title="Applied AI Engineer", location="Remote - Canada",
             description="Build LLM engineer tooling with MLOps and Python.")
    _, variant, _ = score_posting(post, load_profile(FACTS))
    assert variant == "ai-platform"


def _loc(location, description="", title="Software Engineer"):
    return is_eligible(P(title=title, location=location, description=description))


def test_location_us_onsite_rejected():
    assert _loc("San Francisco, CA") == US_ONLY
    assert _loc("New York, NY (HQ)") == US_ONLY


def test_location_other_country_rejected_with_location_reason():
    assert _loc("Belgrade, Serbia") == (False, "outside Canada (belgrade, serbia)")
    assert _loc("Bangalore - Remote") == (False, "outside Canada (bangalore - remote)")


def test_location_us_remote_rejected_as_us_only():
    assert _loc("US - Remote") == US_ONLY
    assert _loc("Remote - United States") == US_ONLY


def test_location_canada_signals_eligible():
    assert _loc("Canada - Remote") == (True, "")
    assert _loc("Montreal - Remote") == (True, "")
    assert _loc("Toronto, Ontario") == (True, "")
    assert _loc("Waterloo, ON") == (True, "")


def test_location_global_remote_with_country_free_description_eligible():
    assert _loc("Remote", "Build detection content in KQL and Python.") == (True, "")
    assert _loc("Anywhere in the world") == (True, "")


def test_location_remote_with_restricted_geography_in_description_rejected():
    # A global-remote location is vetoed by a restricted geography in the description (rule C).
    assert _loc("Remote", "Our engineers sit in Berlin.") == (False, "outside Canada (remote)")
    assert _loc("Remote", "Must be authorized to work in the United States.") == \
        (False, "outside Canada (remote)")
    # A US location stays US-only whatever the description says.
    assert _loc("Remote - US", "Our engineers sit in Berlin.") == US_ONLY


def test_location_canada_in_description_does_not_rescue_foreign_location():
    assert _loc("Belgrade, Serbia", "Hybrid team, open to candidates in Winnipeg.") == \
        (False, "outside Canada (belgrade, serbia)")
    assert _loc("San Francisco, CA", "Remote across Canada.") == US_ONLY


def test_ramp_benefits_canada_text_does_not_make_ny_role_eligible():
    assert is_eligible(P(title="Applied AI Engineer", location="New York, NY (HQ)",
                         description=RAMP_BENEFITS)) == US_ONLY


def test_location_unknown_or_empty_stays_eligible():
    assert _loc("") == (True, "")
    assert _loc("", "Python and KQL.") == (True, "")
    assert _loc("Hybrid") == (True, "")
    assert _loc("Remote-Friendly") == (True, "")


def test_location_title_never_decides():
    assert _loc("Remote", title="Applied AI Engineer - Belgrade, Serbia") == (True, "")
    assert _loc("Toronto", title="Research Engineer - San Francisco, CA") == (True, "")


def test_location_description_beyond_1500_chars_ignored():
    filler = "x" * 1501
    assert _loc("Remote", filler + " Berlin office.") == (True, "")
    assert _loc("Remote", "x" * 1480 + " Berlin office.") == (False, "outside Canada (remote)")


def test_location_us_token_not_taken_from_description_word_us():
    assert _loc("Remote", "Join us to build detection content.") == (True, "")


# Verdict table: the location strings named in the task, plus stored-board variants.
VERDICTS = [
    ("New York, NY (HQ)", US_ONLY),
    ("Sao Paulo", (False, "outside Canada (sao paulo)")),
    ("São Paulo", (False, "outside Canada (sao paulo)")),
    ("Costa Rica", (False, "outside Canada (costa rica)")),
    ("Bangalore - Remote", (False, "outside Canada (bangalore - remote)")),
    ("US - Remote", US_ONLY),
    ("Remote - United States", US_ONLY),
    ("London, UK", (False, "outside Canada (london, uk)")),
    ("EMEA", (False, "outside Canada (emea)")),
    ("Home based - EMEA", (False, "outside Canada (home based - emea)")),
    ("Remote - US: Select locations", US_ONLY),
    ("Remote-Friendly, United States", US_ONLY),
    ("Canada - Remote", (True, "")),
    ("Toronto, Ontario, Canada", (True, "")),
    ("Montreal - Remote", (True, "")),
    ("Montréal - Remote", (True, "")),
    ("Remote, Canada; Remote, Poland", (True, "")),
    ("Home Based - Americas", (True, "")),
    ("Home based - Worldwide", (True, "")),
    ("Hybrid", (True, "")),
    ("Remote", (True, "")),
    ("", (True, "")),
    ("London, UK; Ontario, CAN", (True, "")),
    ("Remote - Canada: Select locations", (True, "")),
]


def test_location_verdict_table():
    for location, expected in VERDICTS:
        assert _loc(location) == expected, location


def test_rejection_reason_truncates_location_to_40_chars():
    loc = "Lisbon, Portugal and several other cities across Europe"
    assert _loc(loc) == (False, "outside Canada (" + loc.lower()[:40] + ")")


def test_stored_rows_gate_verdicts():
    # Rows from the live store. Descriptions are not stored here; the title and location decide.
    assert is_eligible(P(company="Ramp", title="Applied AI Engineer",
                         location="New York, NY (HQ)", description=RAMP_BENEFITS)) == US_ONLY
    assert is_eligible(P(company="OpenAI", title="Partner Applied AI Engineer - LATAM",
                         location="São Paulo")) == (False, "outside Canada (sao paulo)")
    assert is_eligible(P(company="Databricks", title="AI Automation Engineer",
                         location="Costa Rica")) == (False, "outside Canada (costa rica)")
    assert is_eligible(P(company="Hopper", title="Forward Deployed AI Engineer, Travel Intelligence",
                         location="Canada - Remote")) == (True, "")
    assert is_eligible(P(company="Dropbox", title="Senior Security Engineer, AI Security",
                         location="Remote - Canada: Select locations")) == (True, "")


def test_intern_title_penalized_not_boosted():
    prof = load_profile(FACTS)
    intern = P(title="Security Content Engineer, Intern", location="Toronto", description=SEC_DESC)
    grad = P(title="Security Content Engineer, New Grad", location="Toronto", description=SEC_DESC)
    s_intern, _, r_intern = score_posting(intern, prof)
    s_grad, _, _ = score_posting(grad, prof)
    assert "seniority: intern/co-op title (-20)" in r_intern
    assert not any("seniority" in r and "(+6)" in r for r in r_intern)
    assert round(s_grad - s_intern, 1) == 26.0  # +6 vs -20, all else equal (scores below the 100 clamp)


def test_coop_title_penalized():
    prof = load_profile(FACTS)
    _, _, reasons = score_posting(P(title="Machine Learning Intern/Co-op (Winter 2027)", location="Canada",
                                    description=SEC_DESC), prof)
    assert "seniority: intern/co-op title (-20)" in reasons


def test_internal_title_is_not_intern():
    _, _, reasons = score_posting(P(title="Internal Tools Security Engineer", location="Toronto",
                                    description=SEC_DESC), load_profile(FACTS))
    assert not any("intern" in r for r in reasons)


def test_junior_and_graduate_titles_still_get_plus_six():
    prof = load_profile(FACTS)
    for title in ("Junior Security Analyst", "Graduate Software Engineer", "Entry Level SOC Analyst"):
        _, _, reasons = score_posting(P(title=title, location="Toronto", description=SEC_DESC), prof)
        assert "seniority: junior/new-grad title (+6)" in reasons, title


def test_home_based_americas_eligible_and_home_based_emea_rejected():
    assert _loc("Home Based - Americas") == (True, "")
    assert _loc("Home based - EMEA")[0] is False


# ---- location tiers and include_us ----

def _tier_score(location, **kw):
    prof = load_profile(FACTS)
    post = P(title="Security Engineer", location=location, description=SEC_DESC, **kw)
    score, _, reasons = score_posting(post, prof)
    loc_reasons = [r for r in reasons if r.startswith("location:")]
    assert len(loc_reasons) == 1, reasons
    return score, loc_reasons[0]


def test_tier_remote_canada_is_plus_12():
    score, reason = _tier_score("Remote - Canada")
    assert reason == "location: Canada remote/hybrid (+12)"
    assert score == _tier_score("Hybrid")[0] + 10.0  # tier 12 vs tier 2, all else equal


def test_tier_toronto_canada_is_plus_10():
    score, reason = _tier_score("Toronto, Canada")
    assert reason == "location: Canada city (toronto) (+10)"
    assert score == _tier_score("Hybrid")[0] + 8.0


def test_tier_canada_alone_is_plus_6():
    score, reason = _tier_score("Canada")
    assert reason == "location: Canada (+6)"
    assert score == _tier_score("Hybrid")[0] + 4.0


def test_tier_remote_no_restriction_is_plus_4():
    score, reason = _tier_score("Remote")
    assert reason == "location: global remote (+4)"
    assert score == _tier_score("Hybrid")[0] + 2.0


def test_tier_hybrid_is_plus_2():
    score, reason = _tier_score("Hybrid")
    assert reason == "location: unknown geography (+2)"


def test_tier_remote_friendly_is_ambiguous_not_global_remote():
    assert _tier_score("Remote-Friendly")[1] == "location: unknown geography (+2)"


def test_tier_london_on_is_a_major_city():
    assert _tier_score("London, ON, Canada")[1] == "location: Canada city (london on) (+10)"


def test_tier_order_is_strict():
    tiers = [_tier_score(loc)[0] for loc in ("Remote - Canada", "Toronto, Canada", "Canada", "Remote", "Hybrid")]
    assert tiers == sorted(tiers, reverse=True) and len(set(tiers)) == 5


def test_include_us_admits_san_francisco_at_plus_one():
    assert is_eligible(P(title="Security Engineer", location="San Francisco, CA", description=SEC_DESC),
                       include_us=True) == (True, "location: US role, relocation assumed (+1)")


def test_include_us_default_still_rejects_san_francisco():
    assert is_eligible(P(title="Security Engineer", location="San Francisco, CA", description=SEC_DESC)) == US_ONLY


def test_include_us_still_rejects_required_us_work_authorization():
    post = P(title="Security Engineer", location="San Francisco, CA", description=SEC_DESC
             + " You must be authorized to work in the United States.")
    assert is_eligible(post, include_us=True) == (False, "US work authorization required")


def test_include_us_does_not_admit_non_us_non_canada_location():
    assert is_eligible(P(title="Security Engineer", location="London, UK", description=SEC_DESC),
                       include_us=True)[0] is False


def test_include_us_tier_reason_is_plus_one():
    from jobsearch.match import _location_tier
    assert _location_tier("San Francisco, CA") == (1.0, "location: US role, relocation assumed (+1)")
    assert _location_tier("Remote - US") == (1.0, "location: US role, relocation assumed (+1)")


def test_score_posting_include_us_scores_us_posting_default_zero():
    post = P(title="Software Engineer, Security, Cloud", location="New York, NY (HQ)",
             description="Build security tooling in Python.")
    prof = load_profile(FACTS)
    assert score_posting(post, prof)[0] == 0.0
    score, _, reasons = score_posting(post, prof, include_us=True)
    assert score > 0
    assert "location: US role, relocation assumed (+1)" in reasons


# --- experience requirement (description, penalty only) ---------------------------------------

EXP_BASE_TITLE = "Security Content Engineer"
EXP_BASE_LOC = "Remote - Canada"


def _exp_score(desc, title=EXP_BASE_TITLE, location=EXP_BASE_LOC):
    return score_posting(P(title=title, location=location, description=desc), load_profile(FACTS))


def _exp_delta(phrase_desc, neutral_desc):
    """Score difference between a description with a years phrase and the same text with it neutralised."""
    s_with, _, r_with = _exp_score(phrase_desc)
    s_without, _, r_without = _exp_score(neutral_desc)
    return round(s_with - s_without, 1), r_with, r_without


def test_experience_8_plus_of_security_experience_is_minus_24_with_reason():
    desc = SEC_DESC + " Requirements 8+ years of Security Experience, demonstrating impactful contributions."
    neutral = desc.replace("8+ years", "many")
    delta, r_with, r_without = _exp_delta(desc, neutral)
    assert delta == -24.0
    assert "experience: asks 8+ years (-24)" in r_with
    assert not any(r.startswith("experience:") for r in r_without)


def test_experience_range_takes_lower_bound():
    desc = SEC_DESC + " You bring 5-7 years of professional experience."
    neutral = desc.replace("5-7 years", "many")
    delta, r_with, _ = _exp_delta(desc, neutral)
    assert delta == -14.0
    assert "experience: asks 5-7 years (-14)" in r_with
    assert "experience: asks 8+ years (-24)" not in r_with


def test_experience_at_least_3_years_is_minus_6():
    desc = SEC_DESC + " At least 3 years of relevant experience required."
    neutral = desc.replace("At least 3 years", "Some practical")
    delta, r_with, _ = _exp_delta(desc, neutral)
    assert delta == -6.0
    assert "experience: asks 3-4 years (-6)" in r_with


def test_experience_minimum_of_4_years_and_yrs_forms_are_3_4_band():
    for phrase in ("minimum of 4 years of experience", "3+ yrs of hands-on experience"):
        desc = SEC_DESC + " Need " + phrase + "."
        _, _, reasons = _exp_score(desc)
        assert "experience: asks 3-4 years (-6)" in reasons, phrase


def test_experience_two_plus_years_and_no_mention_give_no_change():
    base = SEC_DESC + " Work hands-on with detections."
    s_none, _, r_none = _exp_score(base)
    s_two, _, r_two = _exp_score(base + " Requires 2+ years of experience.")
    assert s_two == s_none
    assert not any(r.startswith("experience:") for r in r_none + r_two)
    _, _, r_one = _exp_score(base + " 1-2 years of experience preferred.")
    assert not any(r.startswith("experience:") for r in r_one)


def test_experience_unrelated_years_text_triggers_at_most_once():
    desc = SEC_DESC + (" We have 8 years of experience in 2019 and 8+ years of experience in 2020,"
                       " plus 8 years of experience across teams.")
    _, _, reasons = _exp_score(desc)
    assert sum(r.startswith("experience:") for r in reasons) == 1
    assert "experience: asks 8+ years (-24)" in reasons


def test_experience_years_without_experience_word_nearby_do_not_trigger():
    desc = SEC_DESC + " Our 8+ years old product is loved by many."
    s_plain, _, r_plain = _exp_score(SEC_DESC + " Our product is loved by many.")
    s_old, _, r_old = _exp_score(desc)
    assert s_old == s_plain
    assert not any(r.startswith("experience:") for r in r_old)


def test_experience_lowest_qualifying_figure_decides():
    desc = SEC_DESC + " Requires 8+ years of experience; 3 years of experience in SOAR preferred."
    _, _, reasons = _exp_score(desc)
    assert "experience: asks 3-4 years (-6)" in reasons
    assert not any("8+" in r for r in reasons if r.startswith("experience:"))


def test_experience_beyond_6000_chars_is_ignored():
    filler = "filler " * 900  # 6300 characters, no years figures, no experience words
    late = SEC_DESC + " " + filler + " Requires 8+ years of experience."
    _, _, reasons = _exp_score(late)
    assert not any(r.startswith("experience:") for r in reasons)
    early = SEC_DESC + " Requires 8+ years of experience. " + filler
    _, _, reasons = _exp_score(early)
    assert "experience: asks 8+ years (-24)" in reasons


def test_experience_penalty_never_rejects_and_senior_title_still_applies():
    desc = "Senior Security Engineer role. Requirements 8+ years of Security Experience. " + SEC_DESC
    post = P(title="Senior Security Engineer, AI Security", location="Remote - Canada", description=desc)
    prof = load_profile(FACTS)
    assert is_eligible(post) == (True, "")
    score, variant, reasons = score_posting(post, prof)
    assert score >= 0.0
    assert "seniority: senior-level title (-14)" in reasons
    assert "experience: asks 8+ years (-24)" in reasons
    assert variant != "general"
    # Penalty floor: a near-zero posting cannot go negative, and it still is not rejected.
    weak = P(title="Helpdesk Technician", location="Hybrid", description="Requirements 10 years of experience.")
    assert is_eligible(weak) == (True, "")
    assert score_posting(weak, prof)[0] == 0.0


# ---------- dedupe_postings (bilingual reposts) ----------

from jobsearch.match import dedupe_postings  # noqa: E402

HOP_EN = "Forward Deployed AI Engineer, Travel Intelligence"
HOP_BI = ("Ingénieur(e) IA en déploiement avancé, Travel Intelligence// "
          "Forward Deployed AI Engineer, Travel Intelligence")
HOP2_EN = "Forward Deployed Engineer, AI Trust and Reliability"
HOP2_BI = "Ingénieur(e) IA en déploiement avancé, AI Trust and Reliability// " + HOP2_EN


def R(uid, company="Hopper", title=HOP_EN, score=40.5, **kw):
    return {"uid": uid, "company": company, "title": title, "score": score, **kw}


def test_dedupe_real_hopper_pair_collapses_to_one_row():
    out = dedupe_postings([R("h:en"), R("h:bi", title=HOP_BI)])
    assert [r["uid"] for r in out] == ["h:en"]  # tie on score: the English title wins


def test_dedupe_real_hopper_second_pair_collapses_to_one_row():
    out = dedupe_postings([R("h2:en", title=HOP2_EN, score=30.0), R("h2:bi", title=HOP2_BI, score=29.0)])
    assert [r["uid"] for r in out] == ["h2:en"]


def test_dedupe_keeps_higher_score_even_when_it_is_bilingual():
    out = dedupe_postings([R("h:en", score=30.0), R("h:bi", title=HOP_BI, score=31.0)])
    assert [r["uid"] for r in out] == ["h:bi"]


def test_dedupe_keeps_english_on_tie_regardless_of_input_order():
    out = dedupe_postings([R("h:bi", title=HOP_BI), R("h:en")])
    assert [r["uid"] for r in out] == ["h:en"]


def test_dedupe_does_not_collapse_different_roles_at_same_company():
    rows = [R("a", title="Security Engineer", score=20.0), R("b", title="Software Engineer", score=21.0)]
    assert [r["uid"] for r in dedupe_postings(rows)] == ["a", "b"]


def test_dedupe_does_not_collapse_merely_similar_titles():
    rows = [R("a", title="Senior AI Engineer", score=20.0), R("b", title="AI Engineer II", score=21.0)]
    assert [r["uid"] for r in dedupe_postings(rows)] == ["a", "b"]


def test_dedupe_containment_must_be_on_word_boundaries():
    rows = [R("a", title="AI Engineer", score=20.0), R("b", title="Senior AI Engineers Guild", score=21.0)]
    assert [r["uid"] for r in dedupe_postings(rows)] == ["a", "b"]


def test_dedupe_different_companies_with_identical_titles_never_collapse():
    rows = [R("a", company="Hopper"), R("b", company="Hopper Labs")]
    assert [r["uid"] for r in dedupe_postings(rows)] == ["a", "b"]
    rows = [R("a", company="Acme", title="Software Engineer"),
            R("b", company="Beta", title="Software Engineer")]
    assert [r["uid"] for r in dedupe_postings(rows)] == ["a", "b"]


def test_dedupe_company_match_is_case_insensitive():
    rows = [R("a", company="HOPPER"), R("b", company="hopper", title=HOP_BI)]
    assert [r["uid"] for r in dedupe_postings(rows)] == ["a"]


def test_dedupe_preserves_input_order_of_survivors_and_passes_others_through():
    rows = [R("x", company="Zed", title="Data Engineer", score=5.0), R("h:bi", title=HOP_BI, score=40.5),
            R("y", company="Acme", score=9.0), R("h:en", score=40.5)]
    assert [r["uid"] for r in dedupe_postings(rows)] == ["x", "y", "h:en"]


def test_dedupe_is_pure_and_does_not_mutate_or_touch_database_fields():
    rows = [R("h:en"), R("h:bi", title=HOP_BI)]
    snapshot = [dict(r) for r in rows]
    out = dedupe_postings(rows)
    assert rows == snapshot  # both rows still in the caller's list, unchanged
    assert out is not rows
    assert dedupe_postings([]) == []


def test_dedupe_missing_title_or_company_never_collapses():
    rows = [R("a", title=""), R("b", title="")]
    assert len(dedupe_postings(rows)) == 2
    rows = [R("a", company=None), R("b", company=None)]
    assert len(dedupe_postings(rows)) == 2


def test_dedupe_missing_score_counts_as_zero():
    out = dedupe_postings([R("a", score=None), R("b", title=HOP_BI, score=1.0)])
    assert [r["uid"] for r in out] == ["b"]


# --- preferences and their consumption in match.py -----------------
# Every test passes a preferences path under tmp_path (or a prefs dict). None touches
# the operator's real preferences.yaml.

from jobsearch.match import (DEFAULT_PREFS_PATH, _location_tier, load_preferences,  # noqa: E402
                             score_breakdown)

PREFS_FULL = """\
version: 1
relocation:
  countries: [CA]
  cities: [Toronto, Halifax]
  exclude_regions: [QC]
  updated_at: "2026-11-24T22:30:00Z"
  reason: "Open to relocation anywhere in Canada."
work_authorization:
  US:
    status: requires_sponsorship
    updated_at: "2026-11-24T22:30:00Z"
    reason: "Require US visa sponsorship to work in the US."
  GB:
    status: maybe_later
    reason: "not a known status, so GB is treated as absent"
"""

PREFS_US_ONLY = """\
version: 1
relocation:
  countries: []
  cities: []
  exclude_regions: []
work_authorization:
  US:
    status: requires_sponsorship
    reason: "Require US visa sponsorship."
"""

US_LOC = "San Francisco, CA"
US_SILENT = "Build detection content and SOAR playbooks in Python."
US_DECLINES = "Build detection content and SOAR playbooks in Python. We are unable to sponsor visas."
US_FAVOURABLE = "Build detection content in Python. We offer visa sponsorship for strong candidates."


def _write_prefs(tmp_path, text, name="preferences.yaml"):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return str(p)


def _us_prefs(tmp_path):
    return load_preferences(_write_prefs(tmp_path, PREFS_US_ONLY))


def test_prefs_default_path_is_the_resume_sibling():
    assert DEFAULT_PREFS_PATH == str(Path.home() / "resume" / "preferences.yaml")


def test_block_style_countries_list_parses(tmp_path):
    """A hand-edited file may use "- CA" instead of "[CA]"; both shapes must load."""
    p = tmp_path / "preferences.yaml"
    p.write_text("version: 1\nrelocation:\n  countries:\n  - CA\n  - US\n"
                 "work_authorization:\n  US:\n    status: requires_sponsorship\n")
    prefs = load_preferences(p)
    assert prefs["countries"] == ["CA", "US"]
    assert prefs["work_authorization"]["US"]["status"] == "requires_sponsorship"


def test_prefs_defaults_when_file_absent(tmp_path):
    expected = {"countries": [], "cities": [], "exclude_regions": [], "work_authorization": {}}
    assert load_preferences(str(tmp_path / "nope.yaml")) == expected


def test_prefs_unreadable_or_malformed_gives_defaults(tmp_path):
    expected = {"countries": [], "cities": [], "exclude_regions": [], "work_authorization": {}}
    assert load_preferences(str(tmp_path)) == expected  # a directory: OSError on open
    assert load_preferences(_write_prefs(tmp_path, "relocation: [oops\n  countries: x\n")) == expected
    assert load_preferences(_write_prefs(tmp_path, "this is not yaml-ish\n")) == expected
    assert load_preferences(_write_prefs(tmp_path, "version: 1\nsurprise_key: 1\n")) == expected
    assert load_preferences(_write_prefs(tmp_path, "relocation:\n  countries: [CA]\n  bogus: x\n")) == expected


def test_prefs_parses_countries_cities_exclusions_and_authorization(tmp_path):
    prefs = load_preferences(_write_prefs(tmp_path, PREFS_FULL))
    assert prefs["countries"] == ["CA"]
    assert prefs["cities"] == ["toronto", "halifax"]
    assert prefs["exclude_regions"] == ["QC"]
    assert prefs["work_authorization"] == {
        "US": {"status": "requires_sponsorship", "updated_at": "2026-11-24T22:30:00Z",
               "reason": "Require US visa sponsorship to work in the US."},
    }


def test_prefs_unknown_status_drops_only_that_country(tmp_path):
    prefs = load_preferences(_write_prefs(tmp_path, PREFS_FULL))
    assert "GB" not in prefs["work_authorization"]
    assert set(prefs) == {"countries", "cities", "exclude_regions", "work_authorization"}


def test_prefs_lowercase_country_key_is_uppercased(tmp_path):
    text = "relocation:\n  countries: [ca]\nwork_authorization:\n  us:\n    status: authorized\n"
    prefs = load_preferences(_write_prefs(tmp_path, text))
    assert prefs["countries"] == ["CA"]
    assert prefs["work_authorization"]["US"]["status"] == "authorized"


def test_load_profile_on_facts_yaml_unchanged_by_preferences_reader():
    prof = load_profile(FACTS)
    assert set(prof) == {"tokens", "skills", "titles"}
    assert prof["tokens"] and prof["skills"] and prof["titles"]
    assert "python" in prof["skills"]


def test_sponsorship_decline_rejected_even_with_include_us(tmp_path):
    ok, why = is_eligible(P(title="Security Engineer", location=US_LOC, description=US_DECLINES),
                          include_us=True, prefs=_us_prefs(tmp_path))
    assert ok is False
    assert "US sponsorship" in why and "declines" in why


def test_sponsorship_refusal_phrases_all_match(tmp_path):
    prefs = _us_prefs(tmp_path)
    for phrase in ("unable to sponsor", "cannot sponsor", "will not sponsor", "no visa sponsorship",
                   "sponsorship is not available", "without sponsorship"):
        post = P(title="Security Engineer", location=US_LOC, description=f"Python role. We {phrase}.")
        ok, why = is_eligible(post, include_us=True, prefs=prefs)
        assert ok is False and "declines" in why, phrase


def test_sponsorship_decline_phrases_are_recognised(tmp_path):
    """Live gap: "we do not sponsor" was read as silence, so a hard no rendered as a soft row."""
    prefs = _us_prefs(tmp_path)
    for phrase in ("we do not sponsor", "the company does not sponsor", "no sponsorship",
                   "not eligible for sponsorship", "we don't sponsor"):
        post = P(title="Security Engineer", location=US_LOC, description=f"Python role. {phrase}.")
        ok, why = is_eligible(post, include_us=True, prefs=prefs)
        assert ok is False and "declines" in why, phrase
        assert us_sponsorship_unstated(post, prefs) is False, phrase


def test_offered_sponsorship_is_still_a_maybe(tmp_path):
    """The other direction: promising to sponsor is not a decline, but it is not a promise either."""
    prefs = _us_prefs(tmp_path)
    for phrase in ("we offer visa sponsorship", "sponsorship available", "H-1B transfer supported",
                   "we sponsor work visas"):
        post = P(title="Security Engineer", location=US_LOC, description=f"Python role. {phrase}.")
        ok, _why = is_eligible(post, include_us=True, prefs=prefs)
        assert ok is True, phrase


def test_favourable_sponsorship_mention_is_not_a_refusal(tmp_path):
    prefs = _us_prefs(tmp_path)
    post = P(title="Security Engineer", location=US_LOC, description=US_FAVOURABLE)
    ok, why = is_eligible(post, include_us=True, prefs=prefs)
    assert ok is True                       # a favourable mention is not a refusal
    assert "declines" not in why
    # Still not in the inbox: nothing in the posting promises sponsorship, so the score is 0.
    assert score_posting(post, load_profile(FACTS), include_us=True, prefs=prefs)[0] == 0.0
    assert us_sponsorship_unstated(post, prefs) is True


def test_us_posting_silent_on_sponsorship_scores_zero(tmp_path):
    prof = load_profile(FACTS)
    post = P(title="Security Engineer", location=US_LOC, description=US_SILENT)
    prefs = _us_prefs(tmp_path)
    assert score_posting(post, prof, include_us=True, prefs=prefs)[0] == 0.0
    assert score_posting(post, prof, include_us=False, prefs=prefs)[0] == 0.0
    assert score_breakdown(post, prof, include_us=True, prefs=prefs)["score"] == 0.0


def test_sponsorship_pref_flips_us_posting_when_absent_vs_present(tmp_path):
    post = P(title="Security Engineer", location=US_LOC, description=US_SILENT)
    absent = load_preferences(str(tmp_path / "no-such-preferences.yaml"))
    present = _us_prefs(tmp_path)
    # Absent: the existing opt-in path admits the US posting at +1.
    assert is_eligible(post, include_us=True, prefs=absent) == (True, "location: US role, relocation assumed (+1)")
    assert score_posting(post, load_profile(FACTS), include_us=True, prefs=absent)[0] > 0
    # Present with requires_sponsorship: the posting stays eligible but scores 0.0, so it is out of
    # the default inbox while remaining reachable (silence is a maybe, a decline is a no).
    assert is_eligible(post, include_us=True, prefs=present)[0] is True
    assert score_posting(post, load_profile(FACTS), include_us=True, prefs=present)[0] == 0.0
    assert us_sponsorship_unstated(post, present) is True


def test_us_authorized_status_does_not_block_us_posting(tmp_path):
    text = "work_authorization:\n  US:\n    status: authorized\n"
    prefs = load_preferences(_write_prefs(tmp_path, text))
    post = P(title="Security Engineer", location=US_LOC, description=US_DECLINES)
    assert is_eligible(post, include_us=True, prefs=prefs) == (True, "location: US role, relocation assumed (+1)")


def test_relocation_tier_for_non_winnipeg_canadian_city_when_ca_listed(tmp_path):
    prefs = load_preferences(_write_prefs(tmp_path, PREFS_FULL))
    score, reason = _location_tier("Kingston, Ontario", prefs)
    assert score == 10.0
    assert reason == "location: Canada city (kingston), open to relocate (+10)"
    score, reason = _location_tier("Toronto, ON", prefs)
    assert (score, reason) == (10.0, "location: Canada city (toronto), open to relocate (+10)")


def test_canadian_city_without_ca_listed_keeps_existing_tiers():
    assert _location_tier("Toronto, ON", _empty_defaults()) == (10.0, "location: Canada city (toronto) (+10)")
    assert _location_tier("Kingston, Ontario", _empty_defaults()) == (6.0, "location: Canada (+6)")


def test_winnipeg_and_plain_canada_keep_existing_values_when_ca_listed(tmp_path):
    prefs = load_preferences(_write_prefs(tmp_path, PREFS_FULL))
    assert _location_tier("Winnipeg, MB", prefs) == (10.0, "location: Canada city (winnipeg) (+10)")
    assert _location_tier("Canada", prefs) == (6.0, "location: Canada (+6)")
    assert _location_tier("Remote - Canada", prefs) == (12.0, "location: Canada remote/hybrid (+12)")


def test_relocation_scoring_reaches_score_posting(tmp_path):
    prefs = load_preferences(_write_prefs(tmp_path, PREFS_FULL))
    post = P(title="Security Engineer", location="Toronto, ON", description=SEC_DESC)
    score, _, reasons = score_posting(post, load_profile(FACTS), prefs=prefs)
    assert "location: Canada city (toronto), open to relocate (+10)" in reasons
    assert score > 0


def test_excluded_province_rejected(tmp_path):
    prefs = load_preferences(_write_prefs(tmp_path, PREFS_FULL))
    post = P(title="Security Engineer", location="Montreal, QC", description=SEC_DESC)
    assert is_eligible(post, prefs=prefs) == (False, "excluded region (QC)")
    assert score_posting(post, load_profile(FACTS), prefs=prefs)[0] == 0.0
    # Exclusion wins over the Canada rule: a non-excluded Canadian city still passes.
    ok, _ = is_eligible(P(title="Security Engineer", location="Toronto, ON", description=SEC_DESC), prefs=prefs)
    assert ok is True


def test_excluded_state_code_rejected_and_empty_list_changes_nothing():
    post = P(title="Security Engineer", location="San Francisco, CA", description=US_SILENT)
    prefs = {"countries": [], "cities": [], "exclude_regions": ["CA"], "work_authorization": {}}
    assert is_eligible(post, include_us=True, prefs=prefs) == (False, "excluded region (CA)")
    assert is_eligible(post, include_us=True, prefs=_empty_defaults()) == (True, "location: US role, relocation assumed (+1)")


def _empty_defaults():
    return {"countries": [], "cities": [], "exclude_regions": [], "work_authorization": {}}
