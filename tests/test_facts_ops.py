"""Tests for jobsearch.facts_ops: answer parsing, op validation, apply, and diff.

All fixtures are small YAML strings defined here. Nothing reads a real resume directory.
"""
import copy

import pytest
import yaml

from jobsearch import facts_ops, match
from jobsearch.facts_ops import (FactsOpsError, apply, diff_text, parse_answer,
                                 upgrade_absent_sets, validate)

PREFS = """\
version: 1
relocation:
  countries: [CA]
  cities: [Toronto]
  exclude_regions: []
  reason: Open to relocating within Canada.
  updated_at: "2026-01-01T00:00:00Z"
work_authorization:
  US:
    status: requires_sponsorship
    reason: Needs visa sponsorship.
"""

FACTS = """\
contact:
  location: Winnipeg, MB
  phone: "+1 403 555 0100"
  email: alex@example.com
employment:
  - employer: Acme
    title: Analyst
    metric: reduced run time by 37%
shared_claims:
  - Built a scanner used by 12 teams
"""

INSTRUCTION = "I can relocate to Toronto and will need sponsorship in the US."


def _answer(ops, status="propose", target="preferences.yaml", summary="Update relocation preferences."):
    return {
        "status": status,
        "target": target,
        "summary": summary,
        "question": "",
        "reason": "",
        "ops": ops,
    }


def _check(payload, *, confirm_protected=False, facts=FACTS, prefs=PREFS, instruction=INSTRUCTION):
    return validate(
        payload,
        facts_text=facts,
        prefs_text=prefs,
        confirm_protected=confirm_protected,
        instruction_text=instruction,
    )


# ---------- parse_answer ----------

def test_parse_answer_accepts_raw_object():
    payload, err = parse_answer('  {"status": "no_change", "target": "facts.yaml"}\n')
    assert err is None
    assert payload == {"status": "no_change", "target": "facts.yaml"}


def test_parse_answer_empty_output_is_fault():
    assert parse_answer("  \n\t ") == (None, "empty_output")


def test_parse_answer_non_ascii_is_fault_not_transliterated():
    payload, err = parse_answer('{"summary": "Montr\u00e9al"}')
    assert payload is None
    assert err == "non_ascii"


def test_parse_answer_fenced_json_is_rejected_not_unwrapped():
    fenced = '```json\n{"status": "no_change", "target": "facts.yaml"}\n```'
    assert parse_answer(fenced) == (None, "invalid_json")


def test_parse_answer_prose_wrapped_json_is_rejected():
    wrapped = 'Here you go: {"status": "no_change", "target": "facts.yaml"}'
    assert parse_answer(wrapped) == (None, "invalid_json")


def test_parse_answer_non_object_json_is_rejected():
    assert parse_answer('["propose"]') == (None, "invalid_json")


# ---------- validate: envelope ----------

def test_validate_valid_preferences_proposal_passes():
    ops = [
        {"op": "set", "path": "relocation.countries", "value": ["CA"]},
        {"op": "set", "path": "relocation.reason", "value": "Open to relocating within Canada."},
        {"op": "set", "path": "work_authorization.US.status", "value": "requires_sponsorship"},
    ]
    got, err = _check(_answer(ops))
    assert err is None
    assert got == ops


@pytest.mark.parametrize("status", ["no_change", "clarify", "refuse"])
def test_validate_non_propose_statuses_return_empty_ops_no_error(status):
    ops = [{"op": "set", "path": "relocation.reason", "value": "ignored"}]
    got, err = _check(_answer(ops, status=status))
    assert err is None
    assert got == []


def test_validate_invalid_status_refused():
    got, err = _check(_answer([], status="maybe"))
    assert got is None
    assert err is not None and err.startswith("invalid_status")


def test_validate_invalid_target_refused():
    got, err = _check(_answer([], status="no_change", target="resume.yaml"))
    assert got is None
    assert err is not None and err.startswith("invalid_target")


def test_validate_more_than_ten_ops_refused():
    ops = [{"op": "set", "path": "relocation.reason", "value": f"r{i}"} for i in range(11)]
    got, err = _check(_answer(ops))
    assert got is None
    assert err is not None and "ops_count" in err


def test_validate_zero_ops_refused_for_propose():
    got, err = _check(_answer([]))
    assert got is None
    assert err is not None and "ops_count" in err


def test_validate_ops_over_4096_bytes_refused():
    ops = [{"op": "set", "path": "relocation.reason", "value": "x" * 420} for _ in range(10)]
    got, err = _check(_answer(ops))
    assert got is None
    assert err is not None and err.startswith("ops_too_large")


@pytest.mark.parametrize("summary", ["Hi", "x" * 241])
def test_validate_summary_length_bounds(summary):
    ops = [{"op": "set", "path": "relocation.reason", "value": "Open to relocating."}]
    got, err = _check(_answer(ops, summary=summary))
    assert got is None
    assert err is not None and err.startswith("summary_length")


def test_validate_non_ascii_summary_refused():
    ops = [{"op": "set", "path": "relocation.reason", "value": "Open to relocating."}]
    got, err = _check(_answer(ops, summary="Montr\u00e9al is fine."))
    assert got is None
    assert err is not None and err.startswith("non_ascii")


# ---------- validate: paths ----------

def test_validate_unallowlisted_path_refused_and_named():
    ops = [{"op": "set", "path": "relocation.salary", "value": "high"}]
    got, err = _check(_answer(ops))
    assert got is None
    assert err == "path_not_allowed: relocation.salary"


def test_validate_facts_path_refused_for_preferences_target():
    ops = [{"op": "set", "path": "contact.email", "value": "x@example.com"}]
    got, err = _check(_answer(ops))
    assert got is None
    assert err == "path_not_allowed: contact.email"


def test_validate_protected_facts_path_refused_without_confirmation():
    ops = [{"op": "set", "path": "employment.0.metric", "value": "reduced run time by 37%"}]
    got, err = _check(_answer(ops, target="facts.yaml"), confirm_protected=False)
    assert got is None
    assert err == "path_not_allowed: employment.0.metric"


def test_validate_protected_path_with_confirmation_and_quoted_value_passes():
    ops = [{"op": "set", "path": "employment.0.metric", "value": "reduced run time by 37%"}]
    got, err = _check(_answer(ops, target="facts.yaml"), confirm_protected=True)
    assert err is None
    assert got == ops


def test_validate_protected_path_confirmed_but_value_not_quoted_refused():
    ops = [{"op": "set", "path": "employment.0.metric", "value": "reduced run time by 50%"}]
    got, err = _check(_answer(ops, target="facts.yaml"), confirm_protected=True)
    assert got is None
    assert err == "path_not_allowed: employment.0.metric"


def test_validate_shared_claims_requires_upsert_append():
    ops = [{"op": "set", "path": "shared_claims", "value": "Built a scanner"}]
    got, err = _check(_answer(ops, target="facts.yaml"))
    assert got is None
    assert err is not None and err.startswith("invalid_op_for_path: shared_claims")


def test_validate_shared_claims_upsert_passes():
    ops = [{"op": "upsert", "path": "shared_claims", "value": "Built a scanner used by 12 teams"}]
    got, err = _check(_answer(ops, target="facts.yaml"))
    assert err is None


def test_validate_facts_contact_location_passes():
    ops = [{"op": "set", "path": "contact.location", "value": "Toronto, ON"}]
    got, err = _check(_answer(ops, target="facts.yaml", summary="Update location to Toronto."))
    assert err is None


# ---------- validate: values and schema ----------

def test_validate_non_ascii_value_refused():
    ops = [{"op": "set", "path": "relocation.reason", "value": "Montr\u00e9al"}]
    got, err = _check(_answer(ops))
    assert got is None
    assert err == "non_ascii_value: relocation.reason"


def test_validate_bad_op_refused():
    ops = [{"op": "merge", "path": "relocation.reason", "value": "x"}]
    got, err = _check(_answer(ops))
    assert got is None
    assert err is not None and err.startswith("invalid_op")


def test_validate_bad_country_code_refused():
    ops = [{"op": "set", "path": "relocation.countries", "value": ["Canada"]}]
    got, err = _check(_answer(ops))
    assert got is None
    assert err is not None and err.startswith("invalid_country_code")


def test_validate_bad_work_authorization_status_refused():
    ops = [{"op": "set", "path": "work_authorization.US.status", "value": "green_card"}]
    got, err = _check(_answer(ops))
    assert got is None
    assert err is not None and err.startswith("invalid_work_authorization")


def test_validate_bad_work_authorization_country_key_refused():
    ops = [{"op": "set", "path": "work_authorization.usa.status", "value": "authorized"}]
    got, err = _check(_answer(ops))
    assert got is None
    assert err is not None and err.startswith("invalid_country_code")


# ---------- validate: truthfulness numbers ----------

def test_validate_invented_number_refused():
    ops = [{"op": "set", "path": "relocation.reason", "value": "Cut costs by 90%."}]
    got, err = _check(_answer(ops))
    assert got is None
    assert err == "unsupported_number: 90% in relocation.reason"


def test_validate_number_present_in_facts_passes():
    ops = [{"op": "upsert", "path": "shared_claims", "value": "Cut run time by 37%"}]
    got, err = _check(_answer(ops, target="facts.yaml"))
    assert err is None


def test_validate_number_present_in_instruction_passes():
    ops = [{"op": "set", "path": "relocation.reason", "value": "Willing to move for 2 years."}]
    got, err = _check(_answer(ops), instruction="I would move for 2 years.")
    assert err is None


def test_validate_number_matches_token_not_substring():
    # "2" must not pass just because "2024" appears in the facts text.
    facts = FACTS + "\nstart_year: 2024\n"
    ops = [{"op": "set", "path": "relocation.reason", "value": "Notice of 2 weeks."}]
    got, err = _check(_answer(ops), facts=facts, instruction="")
    assert got is None
    assert err == "unsupported_number: 2 in relocation.reason"


# ---------- apply ----------

def test_apply_stamps_updated_at_on_relocation_and_work_auth():
    ops = [
        {"op": "set", "path": "relocation.countries", "value": ["CA", "US"]},
        {"op": "set", "path": "work_authorization.US.status", "value": "not_authorized"},
    ]
    out = apply(PREFS, ops)
    data = yaml.safe_load(out)
    assert data["relocation"]["countries"] == ["CA", "US"]
    assert data["work_authorization"]["US"]["status"] == "not_authorized"
    stamp = data["relocation"]["updated_at"]
    assert stamp != "2026-01-01T00:00:00Z"
    assert stamp.endswith("Z") and "T" in stamp
    assert data["work_authorization"]["US"]["updated_at"] == stamp


def test_apply_empty_prefs_bootstraps_version_1():
    out = apply("", [{"op": "upsert", "path": "relocation.reason", "value": "Open to Toronto."}])
    data = yaml.safe_load(out)
    assert data["version"] == 1
    assert data["relocation"]["reason"] == "Open to Toronto."
    assert out.startswith("version: 1\n")


def test_apply_missing_prefs_none_yaml_bootstraps_version_1():
    out = apply("# only a comment\n", [{"op": "upsert", "path": "relocation.cities", "value": ["Toronto"]}])
    assert yaml.safe_load(out)["version"] == 1


def test_apply_set_absent_path_fails_and_names_it():
    with pytest.raises(FactsOpsError, match="work_authorization.DE.status"):
        apply(PREFS, [{"op": "set", "path": "work_authorization.DE.status", "value": "authorized"}])


def test_apply_upsert_creates_path():
    out = apply(PREFS, [{"op": "upsert", "path": "work_authorization.DE.status", "value": "authorized"}])
    assert yaml.safe_load(out)["work_authorization"]["DE"]["status"] == "authorized"


def test_apply_delete_removes_key():
    out = apply(PREFS, [{"op": "delete", "path": "relocation.cities"}])
    data = yaml.safe_load(out)
    assert "cities" not in data["relocation"]
    assert data["relocation"]["countries"] == ["CA"]


def test_apply_delete_absent_key_fails():
    with pytest.raises(FactsOpsError, match="relocation.nope"):
        apply(PREFS, [{"op": "delete", "path": "relocation.nope"}])


def test_apply_shared_claims_appends_to_list():
    out = apply(FACTS, [{"op": "upsert", "path": "shared_claims", "value": "Ran a second scanner"}])
    data = yaml.safe_load(out)
    assert data["shared_claims"] == ["Built a scanner used by 12 teams", "Ran a second scanner"]


def test_apply_round_trip_keeps_other_keys_intact():
    before = yaml.safe_load(FACTS)
    out = apply(FACTS, [{"op": "set", "path": "contact.phone", "value": "+1 403 555 0199"}])
    after = yaml.safe_load(out)
    expected = copy.deepcopy(before)
    expected["contact"]["phone"] = "+1 403 555 0199"
    assert after == expected
    assert out.endswith("\n")
    assert "sort_keys" not in out
    assert out.index("contact:") < out.index("employment:") < out.index("shared_claims:")


def test_apply_bad_yaml_raises_factsops_error():
    with pytest.raises(FactsOpsError):
        apply("relocation: [unclosed", [{"op": "upsert", "path": "relocation.reason", "value": "x"}])


def test_apply_non_mapping_root_raises():
    with pytest.raises(FactsOpsError, match="yaml_root_not_mapping"):
        apply("- a\n- b\n", [{"op": "upsert", "path": "relocation.reason", "value": "x"}])


# ---------- diff ----------

def test_diff_text_names_the_file():
    diff = diff_text("a: 1\n", "a: 2\n", "preferences.yaml")
    assert "--- a/preferences.yaml" in diff
    assert "+++ b/preferences.yaml" in diff
    assert "-a: 1" in diff and "+a: 2" in diff


def test_diff_text_identical_is_empty():
    assert diff_text("a: 1\n", "a: 1\n", "facts.yaml") == ""


# ---------- the two readers must agree ----------

def test_applied_preferences_parse_back_through_match(tmp_path):
    """The writer must emit what match.py's line reader reads, or every preference silently resets."""
    ops = [{"op": "upsert", "path": "relocation.countries", "value": ["CA"]},
           {"op": "upsert", "path": "relocation.cities", "value": ["Toronto"]},
           {"op": "upsert", "path": "relocation.exclude_regions", "value": ["ON"]},
           {"op": "upsert", "path": "work_authorization.US.status",
            "value": "requires_sponsorship"}]
    p = tmp_path / "preferences.yaml"
    p.write_text(apply("", ops))
    prefs = match.load_preferences(p)
    assert prefs["countries"] == ["CA"]
    assert prefs["cities"] == ["toronto"]
    assert prefs["exclude_regions"] == ["ON"]
    assert prefs["work_authorization"]["US"]["status"] == "requires_sponsorship"


def test_upgrade_absent_sets_creates_missing_keys_and_leaves_present_ones():
    """The model says set for a key it thinks exists; on an empty file that must not kill it."""
    ops = [{"op": "set", "path": "relocation.countries", "value": ["CA"]},
           {"op": "set", "path": "work_authorization.US.status", "value": "requires_sponsorship"}]
    fixed, changed = upgrade_absent_sets("", ops)
    assert [o["op"] for o in fixed] == ["upsert", "upsert"]
    assert changed == ["relocation.countries", "work_authorization.US.status"]
    # A key that is already there keeps its set: rewriting is not the same as creating.
    text = "version: 1\nrelocation:\n  countries: [CA]\n"
    fixed, changed = upgrade_absent_sets(text, [{"op": "set", "path": "relocation.countries",
                                                 "value": ["US"]}])
    assert [o["op"] for o in fixed] == ["set"] and changed == []


def test_upgraded_sets_apply_cleanly_on_an_empty_file():
    ops = [{"op": "set", "path": "relocation.countries", "value": ["CA"]}]
    fixed, _ = upgrade_absent_sets("", ops)
    out = apply("", fixed)
    assert "countries: [CA]" in out


def test_relocation_lists_are_dumped_inline():
    """Block-style "- CA" is what match.py cannot read; assert the inline shape instead."""
    out = apply("", [{"op": "upsert", "path": "relocation.countries", "value": ["CA", "US"]}])
    assert "countries: [CA, US]" in out
    assert "- CA" not in out
