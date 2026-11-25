"""Deterministic posting scorer. Stdlib only. Weights and keyword sets are pinned by the tests."""
from __future__ import annotations

import re
import unicodedata
from pathlib import Path

VARIANTS = ["ai-security", "sec-content", "sec-automation", "ai-platform", "ml-engineer", "general"]

ROLE_FAMILIES: dict[str, list[str]] = {
    "ai-security": [
        "ai security", "llm security", "ai red team", "prompt injection", "model security",
        "security engineer ai", "guardrail", "threat detection llm", "ai governance security",
        "ml security",
    ],
    "sec-content": [
        "detection engineer", "detection content", "security content", "threat detection",
        "siem content", "kql", "sigma", "detection rule", "soc engineer", "soc analyst",
        "security analytics",
    ],
    "sec-automation": [
        "security automation", "soar", "security orchestration", "automation engineer",
        "python engineer security", "integration engineer security", "platform security automation",
    ],
    "ai-platform": [
        "ai engineer", "llm engineer", "agent engineer", "machine learning engineer",
        "ai platform", "mlops", "applied ai", "ai infrastructure", "genai engineer",
    ],
    "ml-engineer": [
        "research engineer", "applied scientist", "model training", "fine-tuning", "lora",
        "interpretability", "deep learning engineer",
    ],
    "general": [],
}

# Hard-negative title words: sales|account executive|marketing|recruiter|customer success|
# product manager|designer|finance|legal|hr). Word-bounded so "hr" does not hit "chrome".
_HARD_NEG_TITLE = re.compile(
    r"\b(sales|account executive|marketing|recruiter|customer success|product manager|designer|finance|legal|hr)\b"
)
_JUNIOR = re.compile(r"\b(junior|new grad|entry level|graduate)\b")
_INTERN = re.compile(r"\b(intern|interns|internship|co-op)\b")
_SENIOR = re.compile(r"\b(senior|staff|principal|lead|head of|director|manager|architect)\b")
_WORK_AUTH = re.compile(
    r"must be (a )?u\.?s\.? citizen|security clearance|authorized to work in the (us|united states)"
)
_US_SIGNAL = re.compile(r"\b(us|usa|u\.s\.|united states)\b")
# Posting language that declines sponsorship. Matched on the normalized
# description; a favourable mention ("we offer visa sponsorship") does not match.
_NO_SPONSORSHIP = re.compile(
    r"\b(?:unable to sponsor|cannot sponsor|can't sponsor|will not sponsor|do not sponsor|"
    r"does not sponsor|don't sponsor|no visa sponsorship|no sponsorship|"
    r"not eligible for sponsorship|cannot offer sponsorship|cannot provide sponsorship|"
    r"sponsorship is not available|sponsorship is unavailable|without sponsorship)\b"
)
# A 2-letter uppercase token (province or state code) on the raw location; exclude_regions matches it.
_REGION_CODE = re.compile(r"(?<![A-Za-z])([A-Z]{2})(?![A-Za-z])")

# Reasons the sponsorship preference produces. Declined is a rejection; unstated is a zero score.
SPONSORSHIP_DECLINED = "US sponsorship not offered (posting declines sponsorship)"
SPONSORSHIP_UNSTATED = "US sponsorship required, not stated in posting"

# Experience requirement (description, penalty only). Only the first _EXP_DESC_CHARS characters are
# read. A year figure counts only when an experience-like word lies within _EXP_WINDOW characters of
# it. The lowest qualifying figure decides the penalty.
_EXP_DESC_CHARS = 6000
_EXP_WINDOW = 40
_EXP_YEARS = re.compile(r"(?<![\d.])(\d{1,2})(?:\s*\+|\s*(?:-|\u2013|to)\s*\d{1,2})?\s*(?:years?|yrs?)\b")
_EXP_WORD = re.compile(
    r"\b(?:experience\w*|professional|industry|security|software|engineering|relevant|hands-on)\b"
)

# Location gate (is_eligible): an inverted allow-gate, rules A-D in order (see _location_gate).
# Evaluated on the folded location; the description (first _GATE_DESC_CHARS chars) is read only by
# rule C, to veto a global-remote location that names a restricted geography. The title never
# decides location, and a Canada mention in a description never rescues a foreign location.
_GATE_DESC_CHARS = 1500
# Rule A: any of these anywhere in the location makes the posting eligible.
_GATE_CANADA_TERMS = (
    "canada", "winnipeg", "toronto", "ontario", "manitoba", "vancouver", "british columbia",
    "montreal", "quebec", "ottawa", "waterloo", "saskatchewan", "nova scotia",
    "newfoundland", "halifax", "edmonton", "mississauga",
)
# Rule B: unknown geography is kept. Whole-location match; "" means the board gave no location.
_GATE_AMBIGUOUS = frozenset({
    "", "hybrid", "flexible", "multiple locations", "various", "n/a", "unknown", "not specified",
    "remote-friendly",
})
# Rule C: global-remote phrases, vetoed by any geo-restriction token.
_GLOBAL_REMOTE_TERMS = (
    "remote", "anywhere", "worldwide", "global", "home based", "home-based", "work from anywhere",
    "distributed", "remote-first",
)
# "americas" and "north america" are deliberately not restrictions. "canada" is never one.
_GEO_RESTRICTION_TERMS = (
    "united states", "usa", "u.s.", "us", "u.k.", "uk", "united kingdom", "europe", "emea", "apac",
    "latam", "india", "asia", "africa", "australia", "japan", "singapore", "brazil", "mexico",
    "ireland", "germany", "france", "netherlands", "spain", "poland", "israel", "philippines",
)
_OTHER_COUNTRY_TERMS = (
    "india", "bangalore", "bengaluru", "serbia", "belgrade", "london", "uk", "united kingdom", "england",
    "scotland", "ireland", "dublin", "germany", "berlin", "munich", "france", "paris", "netherlands",
    "amsterdam", "spain", "madrid", "portugal", "lisbon", "italy", "milan", "rome", "poland", "warsaw",
    "sweden", "stockholm", "denmark", "copenhagen", "norway", "finland", "switzerland", "zurich",
    "austria", "vienna", "belgium", "brussels", "czech", "prague", "romania", "bucharest", "ukraine",
    "greece", "turkey", "israel", "tel aviv", "dubai", "uae", "egypt", "nigeria", "kenya", "south africa",
    "singapore", "japan", "tokyo", "china", "hong kong", "korea", "taiwan", "vietnam", "indonesia",
    "malaysia", "thailand", "bangkok", "philippines", "pakistan", "australia", "sydney", "new zealand",
    "brazil", "mexico", "argentina", "colombia", "chile", "peru", "latam", "south america",
    "emea", "apac", "europe", "asia", "africa", "middle east", "oceania",
)
# Restriction names beyond the spec list, so that 'Bangalore - Remote' and 'Remote - Virginia' are
# not global remote: the old deny-list's country and city names, plus US state names.
_GEO_RESTRICTION_EXTRA = _OTHER_COUNTRY_TERMS + (
    "eu", "california", "new york", "washington", "texas", "massachusetts", "illinois", "georgia",
    "virginia", "colorado", "oregon", "district of columbia",
)
_ALL_RESTRICTIONS = _GEO_RESTRICTION_TERMS + _GEO_RESTRICTION_EXTRA
# Description side of rule C: bare "us" and "eu" are common English ("join us"), so location-only.
_DESC_RESTRICTIONS = tuple(t for t in _ALL_RESTRICTIONS if t not in ("us", "eu"))
# Rule D: US signals, on the folded location; postal codes on the raw location (case-sensitive).
_US_LOC_TERMS = ("united states", "usa", "u.s.", "us")
_US_STATE_CODES = ("AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE "
                   "NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC").split()
_US_POSTAL = re.compile(r"(?<![A-Za-z])(?:" + "|".join(_US_STATE_CODES) + r")(?![A-Za-z])")
# Canada terms for the work-authorization rule only (unchanged; the location gate uses _GATE_CANADA_TERMS).
_CANADA_TERMS = ("canada", "canadian", "canadians", "winnipeg", "toronto", "ontario", "manitoba", "vancouver",
                 "british columbia", "montreal", "quebec", "ottawa", "waterloo")
# Location tiers. Named major Canadian cities, scored +10 when a Canada signal is present.
_MAJOR_CITY_TERMS = (
    "toronto", "winnipeg", "vancouver", "montreal", "mississauga", "ottawa", "waterloo", "edmonton",
    "halifax", "victoria", "quebec city", "kitchener", "markham", "burnaby", "surrey",
)
_LONDON_ON = re.compile(r"(?<![a-z0-9])london,? on(?![a-z0-9])")
# US place names for the include_us path. Postal codes are matched via _US_POSTAL on the raw location.
_US_PLACE_TERMS = (
    "san francisco", "new york", "los angeles", "seattle", "boston", "chicago", "austin", "denver",
    "atlanta", "detroit", "palo alto", "mountain view", "sunnyvale", "menlo park", "san jose",
    "san diego", "washington dc", "california", "texas", "massachusetts", "illinois", "colorado",
    "virginia", "florida", "new jersey", "pennsylvania", "oregon", "michigan", "minnesota", "maryland",
    "washington",
)
_US_REASON = "location: US role, relocation assumed (+1)"
# Staffing-agency / spam-board markers, matched against the company name only.
_STAFFING = re.compile(
    r"\b(staffing|recruiting|recruitment|talent solutions|robert half|randstad|adecco|"
    r"kelly services|manpower|teksystems|insight global|apex systems)\b"
)

DEFAULT_PROFILE_PATH = str(Path.home() / "resume" / "facts.yaml")
DEFAULT_PREFS_PATH = str(Path.home() / "resume" / "preferences.yaml")

# Skills merged into every profile on top of the ones parsed from facts.yaml, so scoring does
# not depend on a profile that omits them. Edit to match your own background.
EXTRA_SKILLS = ["kql", "splunk", "elastic siem", "soar", "wazuh"]

_FALLBACK_SKILLS = EXTRA_SKILLS + [
    "python", "pydantic", "docker", "sqlite", "pytest", "bash", "yaml", "systemd",
    "pytorch", "transformers", "peft",
]
_FALLBACK_TITLES = ["security content engineer", "soc analyst", "software engineer"]
_FALLBACK_PROJECTS = ["aurora ledger reconciliation pipeline", "beacon agent operations layer",
                      "containerized log triage agent"]


def _unquote(v: str) -> str:
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        v = v[1:-1]
    return v.strip()


def _split_list(v: str) -> list[str]:
    v = v.strip()
    if v.startswith("[") and v.endswith("]"):
        v = v[1:-1]
    return [_unquote(x) for x in v.split(",") if _unquote(x)]


_TOP = re.compile(r"^([A-Za-z_][\w-]*):\s*(.*)$")
_ITEM = re.compile(r"^  - ([A-Za-z_][\w-]*):\s*(.*)$")
_FIELD = re.compile(r"^    ([A-Za-z_][\w-]*):\s*(.*)$")


def _parse_facts(text: str) -> dict[str, list[dict]]:
    """Line-based reader for the subset of YAML facts.yaml uses (stdlib only)."""
    sections: dict[str, list[dict]] = {}
    section: str | None = None
    item: dict | None = None
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        if not raw[0].isspace():
            m = _TOP.match(raw)
            if m:
                section = m.group(1)
                item = None
                sections.setdefault(section, [])
            continue
        if section is None:
            continue
        m = _ITEM.match(raw)
        if m:
            item = {m.group(1): _unquote(m.group(2))}
            sections[section].append(item)
            continue
        m = _FIELD.match(raw)
        if m and item is not None:  # indent-4 keys only; deeper nesting (metrics, verify) is ignored
            item[m.group(1)] = _split_list(m.group(2)) if m.group(2).strip().startswith("[") \
                else _unquote(m.group(2))
    return sections


def _profile_from_sections(sec: dict[str, list[dict]]) -> dict:
    skills: set[str] = set(EXTRA_SKILLS)
    titles: set[str] = set()
    projects: set[str] = set()
    fields: set[str] = set()
    for c in sec.get("certifications", []):
        for t in c.get("tokens", []) or []:
            skills.add(t.lower())
    for p in sec.get("projects", []):
        for t in str(p.get("tech", "")).split(","):
            if t.strip():
                skills.add(t.strip().lower())
        if p.get("name"):
            projects.add(p["name"].lower())
    for e in sec.get("employment", []):
        raw = str(e.get("title", "")).lower()
        if raw:
            main = re.sub(r"\s*\(.*?\)", "", raw).strip()
            if main:
                titles.add(main)
            m = re.search(r"converted from ([^)]+)", raw)
            if m:
                titles.add(m.group(1).strip())
    for e in sec.get("education", []):
        f = str(e.get("field", "")).lower()
        f = re.sub(r"^specialization in\s+", "", f).strip()
        if f:
            fields.add(f)
    tokens = skills | titles | projects | fields
    return {"tokens": tokens, "skills": skills, "titles": titles}


def _fallback_profile() -> dict:
    skills = set(_FALLBACK_SKILLS)
    titles = set(_FALLBACK_TITLES)
    tokens = skills | titles | set(_FALLBACK_PROJECTS)
    return {"tokens": tokens, "skills": skills, "titles": titles}


def load_profile(path: str = DEFAULT_PROFILE_PATH) -> dict:
    """Harvest {"tokens","skills","titles"} (lowercased sets) from facts.yaml.

    Missing or unreadable file -> built-in fallback profile, so scoring still runs.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return _fallback_profile()
    profile = _profile_from_sections(_parse_facts(text))
    if not profile["tokens"]:
        return _fallback_profile()
    return profile


# ---------- preferences ----------

PREF_STATUSES = frozenset({"authorized", "requires_sponsorship", "not_authorized"})
_PREF_TOP_BLOCKS = ("relocation", "work_authorization")
_PREF_RELOC_KEYS = ("countries", "cities", "exclude_regions", "updated_at", "reason")
_PREF_AUTH_KEYS = ("status", "updated_at", "reason")
_MAP = re.compile(r"^  ([A-Za-z_][\w-]*):\s*(.*)$")          # 2-space child of a block
_BARE_ITEM = re.compile(r"^  - (.*)$")                       # block-style list item: "  - CA"
_CODE2 = re.compile(r"^[A-Z]{2}$")
_COUNTRY_KEY = re.compile(r"^[A-Za-z]{2}$")


def _empty_prefs() -> dict:
    return {"countries": [], "cities": [], "exclude_regions": [], "work_authorization": {}}


def _parse_preferences(text: str) -> dict:
    """Line-based reader for the closed preferences.yaml schema. Raises ValueError on any line
    outside that schema; load_preferences turns the error into the empty defaults."""
    prefs = _empty_prefs()
    auth: dict[str, dict] = {}
    block: str | None = None
    country: str | None = None
    list_key: str | None = None  # a relocation list opened with no inline value ("countries:")
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        if not raw[0].isspace():
            m = _TOP.match(raw)
            if not m:
                raise ValueError(f"malformed line: {raw!r}")
            key, val = m.group(1), m.group(2).strip()
            block, country = None, None
            list_key = None
            if key == "version":
                continue
            if key in _PREF_TOP_BLOCKS and val == "":
                block = key
                continue
            raise ValueError(f"unknown key: {key}")
        if raw.startswith("    "):                       # 4-space field of a country entry
            m = _FIELD.match(raw)
            if (not m or block != "work_authorization" or country is None
                    or m.group(1) not in _PREF_AUTH_KEYS):
                raise ValueError(f"unexpected line: {raw!r}")
            auth[country][m.group(1)] = _unquote(m.group(2))
            continue
        bare = _BARE_ITEM.match(raw)                     # "  - CA" under a list key
        if bare and block == "relocation" and list_key:
            item = _unquote(bare.group(1).strip())
            if list_key in ("countries", "exclude_regions"):
                if _CODE2.match(item.upper()):
                    prefs[list_key].append(item.upper())
            elif item:
                prefs[list_key].append(item.lower())
            continue
        m = _MAP.match(raw)
        if not m:
            raise ValueError(f"malformed line: {raw!r}")
        key, val = m.group(1), m.group(2)
        if block == "relocation":
            if key not in _PREF_RELOC_KEYS:
                raise ValueError(f"unknown key: {key}")
            if key in ("countries", "exclude_regions"):
                codes = [c.upper() for c in _split_list(val)]
                prefs[key] = [c for c in codes if _CODE2.match(c)]
            elif key == "cities":
                prefs[key] = [c.lower() for c in _split_list(val)]
            list_key = key if key in ("countries", "cities", "exclude_regions") and not val.strip() else None
            # relocation-level updated_at / reason: provenance only, not used for behaviour.
        elif block == "work_authorization":
            if not _COUNTRY_KEY.match(key) or val.strip():
                raise ValueError(f"unexpected line: {raw!r}")
            country = key.upper()
            auth.setdefault(country, {})
        else:
            raise ValueError(f"unexpected line: {raw!r}")
    prefs["work_authorization"] = {
        cc: {"status": e.get("status", ""), "updated_at": e.get("updated_at", ""),
             "reason": e.get("reason", "")}
        for cc, e in auth.items() if e.get("status") in PREF_STATUSES
    }
    return prefs


def load_preferences(path: str | None = None) -> dict:
    """Return {"countries","cities","exclude_regions","work_authorization"} from preferences.yaml.

    Missing, unreadable, malformed or unknown-key files give the empty defaults and never raise.
    An unknown work_authorization status drops that country only. PyYAML is not used.
    """
    target = DEFAULT_PREFS_PATH if path is None else path
    try:
        with open(target, encoding="utf-8") as fh:
            text = fh.read()
        return _parse_preferences(text)
    except (OSError, ValueError):
        return _empty_prefs()


def _us_sponsorship_status(prefs: dict | None) -> str | None:
    """The recorded US work-authorization status, or None when the US is not listed."""
    auth = (prefs or {}).get("work_authorization") or {}
    return (auth.get("US") or {}).get("status")


def us_sponsorship_unstated(posting: dict, prefs: dict | None = None) -> bool:
    """True when the US is recorded as requiring sponsorship and this US posting never says.

    A posting that declines sponsorship is rejected by is_eligible() instead; this is the other
    half, the one that stays eligible so it can be opened and read, with a score of 0.
    """
    if _us_sponsorship_status(prefs) != "requires_sponsorship":
        return False
    raw_location = str(posting.get("location") or "").strip()
    if not _is_us_location(_fold(raw_location), raw_location):
        return False
    return _NO_SPONSORSHIP.search(_norm(posting.get("description"))) is None


def _excluded_region(raw_location: str, prefs: dict | None) -> str | None:
    """First province/state code in the raw location that is listed in exclude_regions."""
    excluded = (prefs or {}).get("exclude_regions") or []
    if not excluded:
        return None
    for code in _REGION_CODE.findall(raw_location):
        if code in excluded:
            return code
    return None


def _norm(s: object) -> str:
    return " ".join(str(s or "").lower().split())


def _has(text: str, phrase: str) -> bool:
    return re.search(r"(?<![a-z0-9])" + re.escape(phrase) + r"(?![a-z0-9])", text) is not None


def _first_hit(text: str, terms: tuple[str, ...]) -> str | None:
    """Earliest-occurring term in text (word-bounded), or None."""
    best: tuple[int, str] | None = None
    for term in terms:
        m = re.search(r"(?<![a-z0-9])" + re.escape(term) + r"(?![a-z0-9])", text)
        if m and (best is None or m.start() < best[0]):
            best = (m.start(), term)
    return best[1] if best else None


def _fold(s: str) -> str:
    """Gate normalization: NFKD accent-fold, then _norm ('Montréal' and 'São Paulo' appear in boards)."""
    return _norm(unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii"))


def _is_us_location(location: str, raw_location: str) -> bool:
    """US city or state named in the location (folded), or a US state postal code (raw)."""
    return bool(_first_hit(location, _US_LOC_TERMS) or _first_hit(location, _US_PLACE_TERMS)
                or _US_POSTAL.search(raw_location))


def _location_gate(raw_location: str, location: str, description: str,
                   include_us: bool = False, prefs: dict | None = None) -> tuple[bool, str]:
    """Inverted allow-gate: the first matching rule decides. raw_location is used only for the
    case-sensitive postal codes; location is folded; description is folded and cut to
    _GATE_DESC_CHARS. include_us admits a US location (rule E, before the rule D rejection).
    A province/state code listed in prefs exclude_regions is rejected before any other rule."""
    excluded = _excluded_region(raw_location, prefs)
    if excluded:
        return False, f"excluded region ({excluded})"
    # A: Canada anywhere in the location, whatever else it says.
    if _first_hit(location, _GATE_CANADA_TERMS):
        return True, ""
    # B: unknown geography is kept.
    if location in _GATE_AMBIGUOUS:
        return True, ""
    # C: global remote, with no geo-restriction in the location or the description.
    if (_first_hit(location, _GLOBAL_REMOTE_TERMS)
            and _first_hit(location, _ALL_RESTRICTIONS) is None
            and _first_hit(description, _DESC_RESTRICTIONS) is None):
        return True, ""
    # E: opt-in. A US location is eligible at +1 (relocation assumed).
    if include_us and _is_us_location(location, raw_location):
        return True, _US_REASON
    # D: everything else is out. The reason names the location only.
    if _first_hit(location, _US_LOC_TERMS) or _US_POSTAL.search(raw_location):
        return False, "outside Canada (US-only)"
    return False, f"outside Canada ({location[:40]})"


def is_eligible(posting: dict, include_us: bool = False, prefs: dict | None = None) -> tuple[bool, str]:
    """Return (eligible, reason). reason is "" for a plain Canada/global pass, the US +1 reason
    when include_us admits a US location, and the rejection reason otherwise. prefs=None loads
    preferences.yaml (missing file -> defaults)."""
    if prefs is None:
        prefs = load_preferences()
    title = _norm(posting.get("title"))
    company = _norm(posting.get("company"))
    raw_location = str(posting.get("location") or "").strip()
    location = _fold(raw_location)
    full_description = _norm(posting.get("description"))
    description = _fold(full_description)[:_GATE_DESC_CHARS]

    if _HARD_NEG_TITLE.search(title):
        return False, "role family out of scope"
    if _STAFFING.search(company):
        return False, "staffing agency / spam board"

    if _WORK_AUTH.search(full_description):
        canadian = _first_hit(location, _CANADA_TERMS) is not None
        remote_anywhere = ((_first_hit(location, ("remote", "anywhere")) is not None)
                           and not _US_SIGNAL.search(location))
        if not (canadian or remote_anywhere):
            return False, "US work authorization required"

    # Recorded US sponsorship requirement (preferences.yaml). A posting that explicitly declines
    # sponsorship is a no. One that is merely silent stays eligible, so it is stored and reachable,
    # and score_parts() zeroes its score to keep it out of the default inbox: silence is a maybe.
    if (_is_us_location(location, raw_location)
            and _us_sponsorship_status(prefs) == "requires_sponsorship"
            and _NO_SPONSORSHIP.search(full_description)):
        return False, SPONSORSHIP_DECLINED

    return _location_gate(raw_location, location, description, include_us, prefs)


_CA_GENERIC_TERMS = ("canada", "canadian", "canadians")


def _canadian_city(raw_location: str, location: str) -> str | None:
    """Named Canadian city for the relocation tier: a major-city term, London ON, or else the
    first comma-separated segment when it is neither a Canada/remote term nor a country word."""
    major = _first_hit(location, _MAJOR_CITY_TERMS) or ("london on" if _LONDON_ON.search(location) else None)
    if major:
        return major
    first = _fold(raw_location.split(",")[0]).strip()
    if (not first or _first_hit(first, _CA_GENERIC_TERMS + _GATE_CANADA_TERMS) is not None
            or _first_hit(first, _GLOBAL_REMOTE_TERMS) is not None or first in _GATE_AMBIGUOUS):
        return None
    return first


def _location_tier(raw_location: str, prefs: dict | None = None) -> tuple[float, str]:
    """Location tier table. First match wins: 12 Canada+remote/hybrid, 10 named major
    Canadian city, 6 other Canada signal, 4 global remote, 1 US (include_us only), else 2.
    When "CA" is in prefs countries, a named non-Winnipeg Canadian city scores the relocation
    tier: 10, reason "open to relocate". Other Canada signal stays at 6."""
    location = _fold(raw_location)
    if location in _GATE_AMBIGUOUS:
        return 2.0, "location: unknown geography (+2)"
    canada = _first_hit(location, _GATE_CANADA_TERMS) is not None
    if canada and _first_hit(location, ("remote", "hybrid")) is not None:
        return 12.0, "location: Canada remote/hybrid (+12)"
    if canada and "CA" in ((prefs or {}).get("countries") or []):
        city = _canadian_city(raw_location, location)
        if city and city != "winnipeg":
            return 10.0, f"location: Canada city ({city}), open to relocate (+10)"
    if canada:
        city = _first_hit(location, _MAJOR_CITY_TERMS) or ("london on" if _LONDON_ON.search(location) else None)
        if city:
            return 10.0, f"location: Canada city ({city}) (+10)"
        return 6.0, "location: Canada (+6)"
    if (_first_hit(location, _GLOBAL_REMOTE_TERMS) is not None
            and _first_hit(location, _ALL_RESTRICTIONS) is None):
        return 4.0, "location: global remote (+4)"
    if _is_us_location(location, raw_location):
        return 1.0, _US_REASON
    return 2.0, "location: unknown geography (+2)"


def _experience_years(desc: str) -> int | None:
    """Lowest years-of-experience figure in the normalized description that sits near an experience
    word, or None. Range values count at their lower bound."""
    text = desc[:_EXP_DESC_CHARS]
    found = []
    for m in _EXP_YEARS.finditer(text):
        window = text[max(0, m.start() - _EXP_WINDOW):m.end() + _EXP_WINDOW]
        if _EXP_WORD.search(window):
            found.append(int(m.group(1)))
    return min(found) if found else None


def required_years(desc: str) -> int | None:
    """Public wrapper: lowest required-years figure in a description, or None. Normalises case and
    whitespace first, so '5+ Years of Experience' reads the same as '5+ years of experience'."""
    return _experience_years(_norm(desc or ""))


def _experience_penalty(years: int | None) -> tuple[float, str]:
    """0-2 or unstated years: no change. 3-4: -6. 5-7: -14. 8 or more: -24. Never a rejection."""
    if years is None or years <= 2:
        return 0.0, ""
    if years <= 4:
        return -6.0, "experience: asks 3-4 years (-6)"
    if years <= 7:
        return -14.0, "experience: asks 5-7 years (-14)"
    return -24.0, "experience: asks 8+ years (-24)"


def _family_hits(text: str) -> dict[str, list[str]]:
    return {fam: [kw for kw in kws if _has(text, kw)] for fam, kws in ROLE_FAMILIES.items()}


# The scale the workspace draws its contribution bars against. Not a cap on the score.
SCORE_SCALE = 40.0


def score_parts(posting: dict, profile: dict, include_us: bool = False,
                prefs: dict | None = None) -> dict:
    """The score and the components that build it, in one pass.

    score_posting() and score_breakdown() both read this, so the number the UI shows
    can never drift from the number the store holds. Pure given prefs: prefs=None loads
    preferences.yaml once for this call.
    """
    if prefs is None:
        prefs = load_preferences()
    ok, why = is_eligible(posting, include_us=include_us, prefs=prefs)
    if ok and us_sponsorship_unstated(posting, prefs):
        # Eligible, but silent on sponsorship with the US recorded as requiring it: a maybe, kept
        # in the store at 0 so it sits out of the default inbox and can be read when looked for.
        return {"eligible": True, "why": SPONSORSHIP_UNSTATED, "score": 0.0, "variant": "general",
                "reasons": [SPONSORSHIP_UNSTATED], "skills": 0.0, "role": 0.0, "location": 0.0,
                "seniority": 0.0, "total_raw": 0.0,
                "caps": {"title_pts": 0.0, "desc_pts": 0.0, "tok_title_pts": 0.0,
                         "tok_desc_pts": 0.0}}
    if not ok:
        return {"eligible": False, "why": why, "score": 0.0, "variant": "general",
                "reasons": [why], "skills": 0.0, "role": 0.0, "location": 0.0,
                "seniority": 0.0, "total_raw": 0.0,
                "caps": {"title_pts": 0.0, "desc_pts": 0.0, "tok_title_pts": 0.0,
                         "tok_desc_pts": 0.0}}

    title = _norm(posting.get("title"))
    desc = _norm(posting.get("description"))
    tokens = sorted(profile.get("tokens", set()))
    reasons: list[str] = []

    # Family keywords. Points count each distinct keyword once per field; variant counts per family.
    t_fam = _family_hits(title)
    d_fam = _family_hits(desc)
    t_kw = sorted({kw for hits in t_fam.values() for kw in hits})
    d_kw = sorted({kw for hits in d_fam.values() for kw in hits} - set(t_kw))
    title_pts = min(44.0, 16.0 * len(t_kw))
    desc_pts = min(18.0, 2.0 * len(d_kw))
    for kw in t_kw:
        reasons.append(f"title family keyword '{kw}' (+16)")
    if d_kw:
        reasons.append(f"description family keywords {len(d_kw)} x +2 (cap 18)")

    # Profile tokens.
    t_tok = [t for t in tokens if _has(title, t)]
    d_tok = [t for t in tokens if _has(desc, t) and t not in t_tok]
    tok_title_pts = 6.0 * len(t_tok)
    tok_desc_pts = min(12.0, 1.5 * len(d_tok))
    if t_tok:
        reasons.append(f"title matches profile: {', '.join(t_tok)} (+6 each)")
    if d_tok:
        reasons.append(f"description matches profile: {len(d_tok)} x +1.5 (cap 12)")

    # Location: tier table, on the location field only.
    loc_pts, loc_reason = _location_tier(str(posting.get("location") or ""), prefs)
    reasons.append(loc_reason)

    # Seniority (title only). Intern and co-op titles are penalized: the operator is past them.
    sen_pts = 0.0
    if _JUNIOR.search(title):
        sen_pts += 6.0
        reasons.append("seniority: junior/new-grad title (+6)")
    if _INTERN.search(title):
        sen_pts -= 20.0
        reasons.append("seniority: intern/co-op title (-20)")
    if _SENIOR.search(title):
        sen_pts -= 14.0
        reasons.append("seniority: senior-level title (-14)")

    # Experience requirement (description). Stacks with seniority; a penalty only.
    exp_pts, exp_reason = _experience_penalty(_experience_years(desc))
    if exp_reason:
        reasons.append(exp_reason)

    total = title_pts + desc_pts + tok_title_pts + tok_desc_pts + loc_pts + sen_pts + exp_pts
    score = round(max(0.0, min(100.0, total)), 1)

    # Variant: family with most title+description hits; ties break in VARIANTS order.
    best_variant, best_n = "general", 0
    for fam in VARIANTS:
        if fam == "general" or fam not in ROLE_FAMILIES:
            continue
        n = len(t_fam[fam]) + len(d_fam[fam])
        if n > best_n:
            best_variant, best_n = fam, n
    if best_n == 0:
        best_variant = "general"
    reasons.append(f"variant: {best_variant}")
    return {
        "eligible": True, "why": "",
        "score": score, "variant": best_variant, "reasons": reasons,
        # The three groups the workspace draws. role+skills+location+seniority == total_raw.
        "skills": tok_title_pts + tok_desc_pts,
        "role": title_pts + desc_pts,
        "location": loc_pts,
        "seniority": sen_pts + exp_pts,
        "total_raw": total,
        "caps": {"title_pts": title_pts, "desc_pts": desc_pts,
                 "tok_title_pts": tok_title_pts, "tok_desc_pts": tok_desc_pts},
        "title_tokens": t_tok, "desc_tokens": d_tok,
    }


def score_posting(posting: dict, profile: dict, include_us: bool = False,
                  prefs: dict | None = None) -> tuple[float, str, list[str]]:
    """Return (score 0..100, variant, reasons). Pure: same input, same output.
    include_us is forwarded to is_eligible; with it set, a US location scores at the +1 tier.
    prefs is forwarded too; None loads preferences.yaml."""
    parts = score_parts(posting, profile, include_us=include_us, prefs=prefs)
    return parts["score"], parts["variant"], parts["reasons"]


def score_breakdown(posting: dict, profile: dict, include_us: bool = False,
                    prefs: dict | None = None) -> dict:
    """Read-only breakdown for the UI: the same numbers score_posting returns, plus parts."""
    return score_parts(posting, profile, include_us=include_us, prefs=prefs)



# ---------- duplicate collapse (bilingual reposts) ----------

_PUNCT = re.compile(r"[^\w\s]|_")


def _dedupe_title(title: str, strip_bilingual: bool) -> str:
    """Comparison-only title form: lowercase, whitespace collapsed, optionally cut after the last
    '//' (the English half of a 'French // English' title), punctuation removed."""
    t = _norm(title)
    if strip_bilingual and "//" in t:
        t = t[: t.rfind("//")]
    t = _PUNCT.sub(" ", t)
    return " ".join(t.split())


def _contains_words(outer: str, inner: str) -> bool:
    """True when inner occurs in outer on word boundaries. Both are comparison forms."""
    return bool(inner) and f" {inner} " in f" {outer} "


def _same_role(a: dict, b: dict) -> bool:
    """Collapse test for two postings: same company (case-insensitive) and one normalized title
    contained in the other. Only the shorter title may have its post-'//' text cut for comparison."""
    if _norm(a.get("company")) != _norm(b.get("company")) or not _norm(a.get("company")):
        return False
    ta, tb = _norm(a.get("title")), _norm(b.get("title"))
    if not ta or not tb:
        return False
    # Cut only the strictly shorter side; equal lengths cut neither.
    cut_a = len(ta) < len(tb)
    cut_b = len(tb) < len(ta)
    na = _dedupe_title(ta, cut_a)
    nb = _dedupe_title(tb, cut_b)
    if not na or not nb:
        return False
    return _contains_words(na, nb) or _contains_words(nb, na)


def dedupe_postings(rows: list[dict]) -> list[dict]:
    """Collapse bilingual reposts of one role. Pure: returns a new list in input order.

    Of each colliding pair, keep the higher score; on a tie keep the title already in English
    (no '//'); if still tied, keep the earlier row. Rows are never altered, and non-colliding
    rows pass through untouched."""
    def rank(item: tuple[int, dict]) -> tuple:
        idx, r = item
        score = r.get("score")
        score = float(score) if score is not None else 0.0
        return (-score, "//" in str(r.get("title") or ""), idx)

    order = sorted(enumerate(rows), key=rank)
    kept: list[dict] = []
    dropped: set[int] = set()
    for idx, r in order:
        if any(_same_role(r, k) for k in kept):
            dropped.add(idx)
        else:
            kept.append(r)
    return [r for i, r in enumerate(rows) if i not in dropped]
