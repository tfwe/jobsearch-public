"""Offline tests for jobsearch.sources. _get_json is monkeypatched; the network is never touched."""

import json
import urllib.error
from pathlib import Path

import pytest

from jobsearch import sources as S

SAMPLES = Path(__file__).resolve().parent.parent / "samples"


def _sample(name: str) -> dict:
    return json.loads((SAMPLES / f"{name}.json").read_text(encoding="utf-8"))


def _payload_for(name: str, record: dict):
    """Wrap a single captured record in the envelope its board really returns."""
    return {
        "greenhouse": {"jobs": [record]},
        "lever": [record],
        "ashby": {"jobs": [record]},
        "remotive": {"jobs": [record]},
        "remoteok": [{"legal": "notice dict, no id/position"}, record],
        "arbeitnow": {"data": [record]},
    }[name]


@pytest.fixture
def board(monkeypatch):
    """Serve the fixture for `name` from the patched _get_json."""

    def install(name: str, record: dict | None = None):
        payload = _payload_for(name, record if record is not None else _sample(name))
        monkeypatch.setattr(S, "_get_json", lambda url, retries=2: payload)

    return install


def _assert_posting_shape(p: dict):
    assert set(p) == {"uid", "source", "company", "title", "location", "url",
                      "description", "posted_at", "remote"}
    assert isinstance(p["remote"], bool)
    assert len(p["description"]) <= S.DESC_LIMIT
    assert "<" not in p["description"] and "&lt;" not in p["description"]


# ---------------------------------------------------------------- per-adapter

def test_greenhouse_normalises_fixture(board):
    board("greenhouse")
    [p] = S.fetch_greenhouse("cloudflare", "Cloudflare")
    _assert_posting_shape(p)
    assert p["uid"] == "greenhouse:cloudflare:7695702"
    assert p["source"] == "greenhouse"
    assert p["company"] == "Cloudflare"
    assert p["title"] == "Account Executive, FedCiv"
    assert p["url"] == "https://boards.greenhouse.io/cloudflare/jobs/7695702?gh_jid=7695702"
    assert p["location"] == "Hybrid"
    assert p["posted_at"] == "2026-11-09"
    assert p["remote"] is False
    # content is entity-escaped markup: must come out as plain text
    assert p["description"].startswith("About Us At Cloudflare")
    assert "content-intro" not in p["description"]


def test_lever_normalises_fixture(board):
    board("lever")
    [p] = S.fetch_lever("wattpad", "Wattpad")
    _assert_posting_shape(p)
    assert p["uid"] == "lever:wattpad:4b103011-eec6-4944-bdfd-1461a51a6932"
    assert p["source"] == "lever"
    assert p["company"] == "Wattpad"
    assert p["title"] == "[Wattpad] Delivery Manager"
    assert p["url"] == "https://jobs.lever.co/wattpad/4b103011-eec6-4944-bdfd-1461a51a6932"
    assert p["location"] == "Toronto, Ontario"
    assert p["posted_at"] == "2026-11-11"  # createdAt is epoch milliseconds
    assert p["remote"] is False           # workplaceType is not "remote"
    assert p["description"].startswith("Wattpad’s vision")


def test_lever_remote_when_workplace_type_remote(board):
    rec = dict(_sample("lever"), workplaceType="remote")
    board("lever", rec)
    [p] = S.fetch_lever("wattpad", "Wattpad")
    assert p["remote"] is True


def test_lever_description_falls_back_to_stripped_html(board):
    rec = dict(_sample("lever"), descriptionPlain="")
    rec["description"] = "<p>Build <b>things</b> &amp; ship</p>"
    board("lever", rec)
    [p] = S.fetch_lever("wattpad", "Wattpad")
    assert p["description"] == "Build things & ship"


def test_ashby_normalises_fixture(board):
    board("ashby")
    [p] = S.fetch_ashby("1password", "1Password")
    _assert_posting_shape(p)
    assert p["uid"] == "ashby:1password:febe5d7f-25c9-4d77-a184-0f99e748fe7f"
    assert p["source"] == "ashby"
    assert p["company"] == "1Password"
    assert p["title"] == "Senior Commercial Account Executive (San Francisco/Bay Area)"
    assert p["url"] == "https://jobs.ashbyhq.com/1password/febe5d7f-25c9-4d77-a184-0f99e748fe7f"
    assert p["location"] == "United States"
    assert p["posted_at"] == "2026-10-29"
    assert p["remote"] is True            # isRemote is true
    assert p["description"].startswith("1Password is growing.")


def test_ashby_remote_from_workplace_type_alone(board):
    rec = dict(_sample("ashby"), isRemote=False, workplaceType="Remote", location="Anywhere")
    board("ashby", rec)
    [p] = S.fetch_ashby("1password", "1Password")
    assert p["remote"] is True


def test_remotive_normalises_fixture(board):
    board("remotive")
    [p] = S.fetch_remotive()
    _assert_posting_shape(p)
    assert p["uid"] == "remotive:remotive:2091177"
    assert p["source"] == "remotive"
    assert p["company"] == "TELUS Digital"
    assert p["title"] == "Content Reviewer - United States"
    assert p["url"] == "https://remotive.com/remote-jobs/all-others/content-reviewer-united-states-2091177"
    assert p["location"] == "USA"          # candidate_required_location
    assert p["posted_at"] == "2026-11-19"
    assert p["remote"] is True
    # inline style attributes live inside tags; none of that may leak into the text
    assert "tw-" not in p["description"] and "style=" not in p["description"]


def test_remoteok_normalises_fixture_and_skips_legal_notice(board):
    board("remoteok")
    postings = S.fetch_remoteok()   # payload = [legal dict, job]
    assert len(postings) == 1
    p = postings[0]
    _assert_posting_shape(p)
    assert p["uid"] == "remoteok:remoteok:1137977"
    assert p["source"] == "remoteok"
    assert p["company"] == "Reddit"
    assert p["title"] == "Product Manager, Developer Ecosystem"
    assert p["url"] == "https://remoteOK.com/remote-jobs/remote-product-manager-developer-ecosystem-reddit-1137977"
    assert p["location"] == "US Only"
    assert p["posted_at"] == "2026-11-22"
    assert p["remote"] is True
    # fixture description is UTF-8 mis-decoded as latin-1; must be repaired
    assert "Itâ" not in p["description"]
    assert p["description"].startswith("Reddit is a community of communities. It’s built")


def test_arbeitnow_normalises_fixture(board):
    board("arbeitnow")
    [p] = S.fetch_arbeitnow()
    _assert_posting_shape(p)
    slug = "werkstudent-marketplace-manager-amazon-tiktok-shop-temu-berlin-179896"
    assert p["uid"] == f"arbeitnow:arbeitnow:{slug}"
    assert p["source"] == "arbeitnow"
    assert p["company"] == "AMALOCK"
    assert p["title"] == "Werkstudent Marketplace Manager (m/w/d) – Amazon, TikTok Shop & Temu"
    assert p["url"].startswith("https://www.arbeitnow.com/jobs/companies/amalock/")
    assert p["location"] == "Berlin"
    assert p["posted_at"] == "2026-11-23"   # created_at is epoch seconds
    assert p["remote"] is True
    assert p["description"].startswith("Du willst verstehen")


def test_every_adapter_has_fixture_coverage():
    # guard: the six adapters and six fixtures stay in lockstep
    names = {p.stem for p in SAMPLES.glob("*.json")}
    assert names == {"greenhouse", "lever", "ashby", "remotive", "remoteok", "arbeitnow",
                     "smartrecruiters", "smartrecruiters_detail", "workable"}


# ---------------------------------------------------------------- SmartRecruiters / Workable

def _sr_fake(monkeypatch, fail_ids=()):
    """Serve the SmartRecruiters list fixture, and the detail fixture for any detail id.

    Returns the list of URLs requested so tests can count the calls."""
    listing = _sample("smartrecruiters")
    detail = _sample("smartrecruiters_detail")
    urls: list[str] = []

    def fake(url, retries=2):
        urls.append(url)
        if "/postings?" in url:
            return listing
        pid = url.rsplit("/", 1)[1]
        if pid in fail_ids:
            raise S.FetchError("HTTP 500", url)
        return detail

    monkeypatch.setattr(S, "_get_json", fake)
    return urls


def test_smartrecruiters_normalises_fixture(monkeypatch):
    _sr_fake(monkeypatch)
    rows = S.fetch_smartrecruiters("SGS", "SGS")
    assert [p["uid"] for p in rows] == ["smartrecruiters:SGS:744000163545151",
                                        "smartrecruiters:SGS:744000163478879"]
    p = rows[0]
    _assert_posting_shape(p)
    assert p["source"] == "smartrecruiters"
    assert p["company"] == "SGS"
    assert p["title"] == "Environmental Planner (Contract)"
    assert p["location"] == "Kamloops, BC, Canada"
    assert p["url"] == ("https://jobs.smartrecruiters.com/SGS/"
                        "744000163545151-environmental-planner-contract-")
    assert p["posted_at"] == "2026-11-24"
    assert p["remote"] is False
    assert p["description"].startswith("Location: Kamloops Regional Office")
    assert "Our client is seeking an experienced Environmental Planner" in p["description"]
    assert "SGS is the world" not in p["description"]      # companyDescription is skipped


def test_smartrecruiters_only_fetches_details_for_canada(monkeypatch):
    urls = _sr_fake(monkeypatch)
    rows = S.fetch_smartrecruiters("SGS", "SGS")
    assert "smartrecruiters:SGS:744000163547720" not in {p["uid"] for p in rows}
    list_calls = [u for u in urls if "/postings?" in u]
    detail_calls = [u for u in urls if "/postings/" in u]
    assert len(list_calls) == 1
    assert detail_calls == [S.SMARTRECRUITERS_DETAIL_URL.format(token="SGS", pid=pid)
                            for pid in ("744000163545151", "744000163478879")]
    assert not any("744000163547720" in u for u in urls)


def test_smartrecruiters_survives_a_failed_detail(monkeypatch):
    urls = _sr_fake(monkeypatch, fail_ids={"744000163545151"})
    rows = S.fetch_smartrecruiters("SGS", "SGS")
    assert [p["uid"] for p in rows] == ["smartrecruiters:SGS:744000163478879"]
    assert sum("/postings/" in u for u in urls) == 2   # the failed one was attempted, then skipped


def test_workable_normalises_fixture(monkeypatch):
    payload = _sample("workable")
    monkeypatch.setattr(S, "_get_json", lambda url, retries=2: payload)
    rows = S.fetch_workable("worthai", "Worth AI")
    # One posting, three locations: one row with the places merged, never three rows on one uid.
    [p] = rows
    _assert_posting_shape(p)
    assert p["uid"] == "workable:worthai:908332A8B3"
    assert p["source"] == "workable"
    assert p["company"] == "Worth AI"
    assert p["title"] == "Customer Success Manager"
    assert p["url"] == "https://apply.workable.com/j/908332A8B3"
    assert p["posted_at"] == "2026-11-10"
    assert p["remote"] is True                          # telecommuting: True
    assert p["location"] == ("Orlando, Florida, United States / "
                             "Atlanta, Georgia, United States / "
                             "Tampa, Florida, United States")


def test_workable_distinct_shortcodes_stay_distinct_rows(monkeypatch):
    payload = _sample("workable")
    second = dict(payload["jobs"][0], shortcode="AAAA1111BB", title="Staff Engineer",
                  city="Toronto", state="Ontario", country="Canada")
    monkeypatch.setattr(S, "_get_json",
                        lambda url, retries=2: {**payload, "jobs": [payload["jobs"][0], second]})
    rows = S.fetch_workable("worthai", "Worth AI")
    assert [p["uid"] for p in rows] == ["workable:worthai:908332A8B3",
                                        "workable:worthai:AAAA1111BB"]
    assert rows[1]["location"] == "Toronto, Ontario, Canada"


def test_fetch_board_dispatches_the_new_ats(monkeypatch):
    payload = _sample("workable")
    monkeypatch.setattr(S, "_get_json", lambda url, retries=2: payload)
    rows = S.fetch_board({"ats": "workable", "token": "worthai", "name": "Worth"})
    assert len(rows) == 1 and rows[0]["company"] == "Worth"

    seen = []
    monkeypatch.setattr(S, "fetch_smartrecruiters", lambda t, c: seen.append((t, c)) or [])
    S.fetch_board({"ats": "smartrecruiters", "token": "SGS", "name": "SGS"})
    assert seen == [("SGS", "SGS")]

    with pytest.raises(S.FetchError):
        S.fetch_board({"ats": "myspace", "token": "x", "name": "X"})


def test_canonical_key_is_empty_when_any_part_is_missing():
    full = {"company": "Acme", "title": "Engineer", "location": "Remote",
            "url": "https://jobs.acme.example/1"}
    assert S.canonical_key(full) == "acme|engineer|remote|https://jobs.acme.example/1"
    for field in ("company", "title", "location", "url"):
        assert S.canonical_key(dict(full, **{field: ""})) == "", field
    assert S.canonical_key(dict(full, url="not a url")) == ""


# ---------------------------------------------------------------- helpers

def test_strip_html_decodes_double_escaped_and_collapses_ws():
    assert S._strip_html("&lt;p&gt;Hi &amp;amp; bye&lt;/p&gt;\n\n") == "Hi & bye"
    assert S._strip_html("<p>a</p>\n<p>  b &amp; c </p>") == "a b & c"
    assert S._strip_html(None) == ""


def test_fix_mojibake_only_touches_mangled_sequences():
    assert S._fix_mojibake("It\u00e2\u0080\u0099s") == "It’s"
    assert S._fix_mojibake("café") == "café"


def test_posted_at_helpers():
    assert S._date_from_iso("2026-10-29T16:56:07.691+00:00") == "2026-10-29"
    assert S._date_from_iso("") == ""
    assert S._date_from_epoch(1794428333946, millis=True) == "2026-11-11"
    assert S._date_from_epoch("garbage") == ""


def test_description_truncated_to_limit(board):
    rec = dict(_sample("remotive"), description="<p>" + "x" * (S.DESC_LIMIT + 500) + "</p>")
    board("remotive", rec)
    [p] = S.fetch_remotive()
    assert len(p["description"]) == S.DESC_LIMIT


# ---------------------------------------------------------------- dispatch / fan-out

def test_fetch_board_dispatches_on_ats(monkeypatch):
    seen = []
    monkeypatch.setattr(S, "fetch_greenhouse", lambda t, c: seen.append(("gh", t, c)) or [])
    monkeypatch.setattr(S, "fetch_lever", lambda t, c: seen.append(("lv", t, c)) or [])
    monkeypatch.setattr(S, "fetch_ashby", lambda t, c: seen.append(("ab", t, c)) or [])
    monkeypatch.setattr(S, "fetch_remotive", lambda: seen.append(("rt",)) or [])
    monkeypatch.setattr(S, "fetch_remoteok", lambda: seen.append(("ro",)) or [])
    monkeypatch.setattr(S, "fetch_arbeitnow", lambda: seen.append(("an",)) or [])
    for ats in ["greenhouse", "lever", "ashby", "remotive", "remoteok", "arbeitnow"]:
        S.fetch_board({"name": "Acme", "ats": ats, "token": "acme"})
    assert seen == [("gh", "acme", "Acme"), ("lv", "acme", "Acme"), ("ab", "acme", "Acme"),
                    ("rt",), ("ro",), ("an",)]


def test_fetch_board_unknown_ats_raises():
    with pytest.raises(S.FetchError):
        S.fetch_board({"name": "X", "ats": "myspace", "token": "x"})


def test_fetch_all_never_raises_and_reports_errors(monkeypatch):
    lever_rec = _sample("lever")
    ashby_rec = _sample("ashby")

    def fake_get(url, retries=2):
        if "greenhouse" in url:
            raise S.FetchError("HTTP 404", url)
        if "lever" in url:
            return []                                   # 0 postings
        if "ashby" in url:
            return _payload_for("ashby", ashby_rec)
        if "remotive" in url:
            raise S.FetchError("HTTP 503", url)
        if "remoteok" in url:
            raise RuntimeError("boom")                  # non-FetchError must not escape
        return _payload_for("arbeitnow", _sample("arbeitnow"))

    monkeypatch.setattr(S, "_get_json", fake_get)
    registry = [
        {"name": "Cloudflare", "ats": "greenhouse", "token": "cloudflare"},
        {"name": "Acme", "ats": "lever", "token": "acme"},
        {"name": "1Password", "ats": "ashby", "token": "1password"},
        {"name": "Weird", "ats": "myspace", "token": "weird"},
    ]
    postings, errors = S.fetch_all(registry, include_aggregators=True)

    assert {p["uid"] for p in postings} == {
        "ashby:1password:febe5d7f-25c9-4d77-a184-0f99e748fe7f",
        "arbeitnow:arbeitnow:werkstudent-marketplace-manager-amazon-tiktok-shop-temu-berlin-179896",
    }
    assert "greenhouse:cloudflare: HTTP 404" in errors
    assert "lever:acme: 0 postings (dead board?)" in errors
    assert "myspace:weird: unknown ats 'myspace'" in errors
    assert "remotive:remotive: HTTP 503" in errors
    assert any(e.startswith("remoteok:remoteok: RuntimeError") for e in errors)
    assert len(errors) == 5


def test_fetch_all_without_aggregators_skips_them(monkeypatch):
    calls = []

    def fake_get(url, retries=2):
        calls.append(url)
        return {"jobs": []}

    monkeypatch.setattr(S, "_get_json", fake_get)
    postings, errors = S.fetch_all([{"name": "A", "ats": "ashby", "token": "a"}],
                                   include_aggregators=False)
    assert postings == []
    assert errors == ["ashby:a: 0 postings (dead board?)"]
    assert calls == [S.ASHBY_URL.format(token="a")]


# ---------------------------------------------------------------- _get_json transport

def test_get_json_does_not_retry_4xx(monkeypatch):
    attempts = []

    def fake_urlopen(req, timeout):
        attempts.append(1)
        raise urllib.error.HTTPError(req.full_url, 404, "nf", {}, None)

    monkeypatch.setattr(S.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(S.time, "sleep", lambda s: None)
    with pytest.raises(S.FetchError) as ei:
        S._get_json("https://example.invalid/x")
    assert ei.value.reason == "HTTP 404"
    assert len(attempts) == 1


def test_get_json_retries_transient_then_raises(monkeypatch):
    attempts, sleeps = [], []

    def fake_urlopen(req, timeout):
        attempts.append(1)
        raise urllib.error.URLError("unreachable")

    monkeypatch.setattr(S.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(S.time, "sleep", lambda s: sleeps.append(s))
    with pytest.raises(S.FetchError):
        S._get_json("https://example.invalid/x", retries=2)
    assert len(attempts) == 3 and sleeps == [1, 1]


# ---------------------------------------------------------------- liveness

def _stub_snippet(monkeypatch, code: int, body: bytes = b""):
    """Replace the one network call; check_liveness never reaches urllib under test."""
    calls = []

    def fake(url, timeout=10):
        calls.append(url)
        return code, body

    monkeypatch.setattr(S, "_fetch_snippet", fake)
    return calls


def test_check_liveness_returns_gone_on_404_and_410(monkeypatch):
    for code in (404, 410):
        _stub_snippet(monkeypatch, code)
        res = S.check_liveness("https://example.invalid/job/1")
        assert res["status"] == "gone" and res["http_code"] == code
        assert res["reason"] == f"HTTP {code}"
        assert set(res) == {"status", "http_code", "reason", "checked_at"}


def test_check_liveness_detects_closed_phrase_in_200_body(monkeypatch):
    _stub_snippet(monkeypatch, 200, b"<h1>Senior Engineer</h1><p>Sorry, this job is "
                                    b"no longer available.</p>")
    res = S.check_liveness("https://example.invalid/job/2")
    assert res["status"] == "gone" and res["http_code"] == 200
    assert "job is no longer available" in res["reason"]

    _stub_snippet(monkeypatch, 200, b"<p>The POSITION HAS BEEN FILLED</p>")
    assert S.check_liveness("https://example.invalid/job/2")["status"] == "gone"

    _stub_snippet(monkeypatch, 200, b"<h1>Senior Engineer</h1><p>Apply now</p>")
    res = S.check_liveness("https://example.invalid/job/2")
    assert res["status"] == "live" and res["reason"] == "HTTP 200" and res["http_code"] == 200


def test_check_liveness_returns_unknown_on_403_429_timeout_and_bad_scheme(monkeypatch):
    for code in (403, 429, 500, 503):
        _stub_snippet(monkeypatch, code)
        res = S.check_liveness("https://example.invalid/job/3")
        assert res["status"] == "unknown" and res["http_code"] == code, code

    def timed_out(url, timeout=10):
        raise TimeoutError("timed out")

    monkeypatch.setattr(S, "_fetch_snippet", timed_out)
    res = S.check_liveness("https://example.invalid/job/3")
    assert res["status"] == "unknown" and res["http_code"] is None
    assert res["reason"].startswith("network error")

    calls = _stub_snippet(monkeypatch, 200)
    for url in ("ftp://example.invalid/job/3", "file:///etc/passwd", "",
                "mailto:jobs@example.invalid"):
        res = S.check_liveness(url)
        assert res["status"] == "unknown" and res["http_code"] is None
    assert calls == []  # rejected before any fetch
    assert S.check_liveness("mailto:jobs@example.invalid")["reason"] == "no usable url"
    assert S.check_liveness("ftp://example.invalid/x")["reason"] == "unsupported scheme"
