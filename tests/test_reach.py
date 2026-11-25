import pytest

from jobsearch.reach import LEVELS, OUT_OF_REACH_LEVELS, classify, title_level
from jobsearch.match import required_years

YEARS_8 = "Requires 8+ years of experience in security engineering."
YEARS_4 = "Minimum 4 years of professional experience building detection content."
YEARS_2 = "2 years of experience with Python."


@pytest.mark.parametrize("title, desc, level, verdict, years", [
    ("Security Co-op", "", "intern", "in_reach", None),
    ("Junior SOC Analyst", "", "entry", "in_reach", None),
    ("Security Analyst", "", "mid", "in_reach", None),
    ("Senior Detection Engineer", "", "senior", "stretch", None),
    ("Senior Detection Engineer", YEARS_4, "senior", "stretch", 4),
    ("Security Engineer", YEARS_8, "mid", "out_of_reach", 8),
    ("Staff Security Engineer", "", "staff", "out_of_reach", None),
    ("Principal Engineer", YEARS_2, "principal", "out_of_reach", 2),
    ("Head of Engineering", "", "lead", "out_of_reach", None),
    ("Software Engineer, Leading Detection", "", "mid", "in_reach", None),
    ("Security Engineer", "", "mid", "in_reach", None),
])
def test_classify_table(title, desc, level, verdict, years):
    v = classify(title, desc)
    assert v["level"] == level
    assert v["years"] == years
    assert v["verdict"] == verdict
    assert v["in_reach"] == (verdict == "in_reach")


def test_empty_description_has_no_years():
    assert classify("Security Engineer", "")["years"] is None
    assert classify("Security Engineer", None)["years"] is None
    assert required_years("") is None


def test_leading_does_not_match_lead():
    assert title_level("Leading Threat Hunting") == "mid"
    assert title_level("Tech Lead, Detection") == "lead"


def test_precedence_senior_group_over_intern_and_senior_over_mid():
    assert title_level("Staff Intern") == "staff"
    assert title_level("Engineering Manager, Intern Program") == "manager"
    assert title_level("Junior Analyst") == "entry"
    assert title_level("Security Intern") == "intern"
    assert title_level("Senior Staff Engineer") == "staff"
    assert title_level("Senior Analyst") == "senior"


def test_levels_constants():
    assert LEVELS[0] == "intern" and LEVELS[-1] == "architect"
    assert OUT_OF_REACH_LEVELS == {"staff", "principal", "lead", "manager", "director", "architect"}


@pytest.mark.parametrize("title, desc", [
    ("Intern", ""), ("Junior Analyst", ""), ("Security Engineer", ""),
    ("Senior Engineer", ""), ("Senior Engineer", YEARS_4), ("Engineer", YEARS_8),
    ("Staff Engineer", ""), ("Principal Engineer", YEARS_2), ("Head of Engineering", ""),
    ("Engineering Manager", YEARS_2), ("Security Engineer", YEARS_2),
])
def test_why_is_empty_exactly_when_in_reach(title, desc):
    v = classify(title, desc)
    assert (v["why"] == "") == (v["verdict"] == "in_reach")
    assert v["in_reach"] == (v["verdict"] == "in_reach")


def test_why_names_both_causes():
    v = classify("Staff Engineer", YEARS_8)
    assert v["why"] == "staff-level title, asks 8+ years"
    assert classify("Senior Engineer", YEARS_4)["why"] == "senior title, asks 4 years"


@pytest.mark.parametrize("desc, years", [
    ("5+ Years of Experience required", 5),
    ("5+ years of experience required", 5),
    ("7+ Years of Security engineering", 7),
    ("5+ YEARS of security experience", 5),
    ("Requires 8+ YEARS in security engineering.", 8),
])
def test_capitalised_year_figures_are_read(desc, years):
    assert required_years(desc) == years


def test_capitalised_years_classify_out_of_reach():
    v = classify("Security Engineer", "7+ Years of Security engineering")
    assert v["years"] == 7 and v["verdict"] == "out_of_reach" and not v["in_reach"]
    assert classify("Security Engineer", "5+ YEARS of security experience")["verdict"] == "out_of_reach"


@pytest.mark.parametrize("title, level", [
    ("Sr. Security Engineer", "senior"),
    ("Sr Security Engineer", "senior"),
    ("VP of Security", "director"),
    ("SVP, Engineering", "director"),
    ("EVP Technology", "director"),
    ("CISO", "director"),
    ("CTO", "director"),
    ("Chief Technology Officer", "director"),
    ("Chief Information Security Officer", "director"),
    ("Engineering Manager", "manager"),
    ("Engineering Manager, Intern Program", "manager"),
    ("Intern Program Coordinator", "intern"),
    ("Principal Architect", "architect"),
])
def test_new_senior_vocabulary(title, level):
    assert title_level(title) == level


@pytest.mark.parametrize("title", [
    "Sr. Security Engineer", "VP of Security", "SVP Engineering", "CISO", "CTO",
    "Chief Technology Officer", "Engineering Manager, Intern Program",
])
def test_senior_group_titles_never_in_reach(title):
    v = classify(title, "")
    assert v["verdict"] != "in_reach" and not v["in_reach"]


def test_sr_does_not_match_inside_other_words():
    assert title_level("Service Engineer") == "mid"
    assert title_level("Ssr Analyst") == "mid"
    assert title_level("Ctorial Analyst") == "mid"
    assert title_level("Vpn Engineer") == "mid"
    assert title_level("CISOffice Analyst") == "mid"
