"""Job-board source adapters. Stdlib only.

Every adapter normalises its board payload into the frozen `posting` dict (see the tests).

`canonical_key` builds the cross-source dedupe key. The URL is the bridge: an aggregator row
(Remotive, RemoteOK) points at the employer's own ATS URL, so the same job arriving from two
sources produces the same key.
"""

from __future__ import annotations

import concurrent.futures
import html
import json
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from urllib.parse import urlsplit

USER_AGENT = "Mozilla/5.0 (compatible; jobsearch/1.0)"
TIMEOUT = 30
DESC_LIMIT = 20000
MAX_WORKERS = 6

GREENHOUSE_URL = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"
LEVER_URL = "https://api.lever.co/v0/postings/{token}?mode=json"
ASHBY_URL = "https://api.ashbyhq.com/posting-api/job-board/{token}"
SMARTRECRUITERS_URL = "https://api.smartrecruiters.com/v1/companies/{token}/postings?limit=100"
SMARTRECRUITERS_DETAIL_URL = "https://api.smartrecruiters.com/v1/companies/{token}/postings/{pid}"
WORKABLE_URL = "https://apply.workable.com/api/v1/widget/accounts/{token}?details=true"
REMOTIVE_URL = "https://remotive.com/api/remote-jobs?limit={limit}"
REMOTEOK_URL = "https://remoteok.com/api"
ARBEITNOW_URL = "https://arbeitnow.com/api/job-board-api"


class FetchError(Exception):
    """A source could not be fetched. `str(e)` is the bare reason (e.g. "HTTP 404")."""

    def __init__(self, reason: str, url: str = ""):
        super().__init__(reason)
        self.reason = reason
        self.url = url


# ---------------------------------------------------------------- HTTP

def _get_json(url: str, retries: int = 2) -> object:
    """GET a URL and decode JSON. Retries transient failures (5xx, 429, network) with a 1s sleep."""
    req = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
    )
    last = "unknown error"
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            if e.code < 500 and e.code != 429:
                raise FetchError(f"HTTP {e.code}", url) from None
            last = f"HTTP {e.code}"
        except (urllib.error.URLError, OSError) as e:
            last = f"network error: {getattr(e, 'reason', e)}"
        else:
            try:
                return json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise FetchError("invalid JSON", url) from None
        if attempt < retries:
            time.sleep(1)
    raise FetchError(last, url)


# ---------------------------------------------------------------- liveness

# Phrases that mean a posting is closed even when the server answers 200. Matched
# against the lowercased first 16 KB of the body, so they must be whole sentences.
CLOSED_PHRASES: tuple[str, ...] = (
    "no longer accepting applications",
    "job is no longer available",
    "position has been filled",
    "this job has expired",
    "posting has closed",
    "page not found",
)
SNIPPET_BYTES = 16 * 1024


def _fetch_snippet(url: str, timeout: int = 10) -> tuple[int, bytes]:
    """GET a posting page and return (status, at most SNIPPET_BYTES of body).

    The only network call in the liveness check. Tests replace it, so no test ever
    opens a socket. HTTPError is folded into the status code because a 4xx/5xx is an
    answer, not a failure; anything else (timeouts, refused connections) raises OSError.
    """
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Range": f"bytes=0-{SNIPPET_BYTES - 1}",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read(SNIPPET_BYTES)
    except urllib.error.HTTPError as e:
        return e.code, b""


def check_liveness(url: str, timeout: int = 10) -> dict:
    """Say whether a posting URL still answers as open.

    Returns {status: live|gone|unknown, http_code, reason, checked_at}. Only 404/410
    and a closed phrase in a 2xx/3xx body count as gone. 401/403/429/5xx and network
    errors are unknown: a bot wall or rate limit says nothing about the job itself.
    """
    checked_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    def result(status: str, code: int | None, reason: str) -> dict:
        return {"status": status, "http_code": code, "reason": reason,
                "checked_at": checked_at}

    parts = urlsplit((url or "").strip())
    if not parts.netloc:
        return result("unknown", None, "no usable url")
    if parts.scheme.lower() not in ("http", "https"):
        return result("unknown", None, "unsupported scheme")
    try:
        code, body = _fetch_snippet(url.strip(), timeout)
    except OSError as e:
        return result("unknown", None, f"network error: {getattr(e, 'reason', e)}")
    if code in (404, 410):
        return result("gone", code, f"HTTP {code}")
    if 200 <= code < 400:
        text = body[:SNIPPET_BYTES].decode("utf-8", errors="replace").lower()
        for phrase in CLOSED_PHRASES:
            if phrase in text:
                return result("gone", code, f"closed phrase: {phrase}")
        return result("live", code, f"HTTP {code}")
    return result("unknown", code, f"HTTP {code}")


# ---------------------------------------------------------------- text helpers

_MOJIBAKE_RE = re.compile(r"[\u00c2-\u00f4][\u0080-\u00bf]+")
_TAG_RE = re.compile(r"</?[A-Za-z][^>]*>")
_WS_RE = re.compile(r"\s+")
_REMOTE_RE = re.compile(r"\b(remote|anywhere)\b", re.IGNORECASE)
_ISO_DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})")


def _fix_mojibake(s: str) -> str:
    """Repair UTF-8 bytes that were decoded as latin-1/cp1252 (e.g. 'Itâ\\x80\\x99s' -> 'It’s')."""

    def repl(m: re.Match) -> str:
        try:
            return m.group(0).encode("latin-1").decode("utf-8")
        except UnicodeError:
            return m.group(0)

    return _MOJIBAKE_RE.sub(repl, s)


def _strip_html(s: str | None) -> str:
    """Strip tags, decode entities (twice, for double-escaped boards), collapse whitespace."""
    if not s:
        return ""
    s = _fix_mojibake(str(s))
    s = html.unescape(s)  # greenhouse ships escaped markup: &lt;div&gt;
    s = _TAG_RE.sub(" ", s)
    s = html.unescape(s)
    return _WS_RE.sub(" ", s).strip()


def _clean_plain(s: str | None) -> str:
    """Plain-text field: fix mojibake, decode entities, collapse whitespace. No tag stripping."""
    if not s:
        return ""
    s = html.unescape(_fix_mojibake(str(s)))
    return _WS_RE.sub(" ", s).strip()


def _date_from_iso(s: str | None) -> str:
    m = _ISO_DATE_RE.match(str(s or ""))
    return m.group(1) if m else ""


def _date_from_epoch(value, millis: bool = False) -> str:
    try:
        secs = float(value) / (1000.0 if millis else 1.0)
        return datetime.fromtimestamp(secs, tz=timezone.utc).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


def _location_is_remote(location: str) -> bool:
    return bool(_REMOTE_RE.search(location or ""))


def _posting(
    source: str,
    slug: str,
    native_id,
    company: str,
    title: str,
    location: str,
    url: str,
    description: str,
    posted_at: str,
    remote: bool,
) -> dict:
    return {
        "uid": f"{source}:{slug}:{native_id}",
        "source": source,
        "company": company,
        "title": title,
        "location": location,
        "url": url,
        "description": (description or "")[:DESC_LIMIT],
        "posted_at": posted_at,
        "remote": bool(remote),
    }


def _items(payload, key: str | None) -> list[dict]:
    """Pull the job list out of a payload, skipping non-dict entries."""
    if key is not None:
        if not isinstance(payload, dict) or not isinstance(payload.get(key), list):
            raise FetchError(f"unexpected payload shape (no '{key}' list)")
        seq = payload[key]
    else:
        if not isinstance(payload, list):
            raise FetchError("unexpected payload shape (expected a list)")
        seq = payload
    return [x for x in seq if isinstance(x, dict)]


# ---------------------------------------------------------------- dedupe key

_LEGAL_SUFFIXES = frozenset({"inc", "llc", "ltd", "limited", "corp", "corporation",
                             "gmbh", "bv", "sa", "sas", "plc", "co"})
_NON_WORD_RE = re.compile(r"[\W_]+")
_PAREN_RE = re.compile(r"\([^)]*\)")


def _words(s: str | None) -> list[str]:
    """Lowercase, punctuation to spaces, collapse whitespace; returns the word tokens."""
    return _NON_WORD_RE.sub(" ", str(s or "").lower()).split()


def _canonical_url(url: str | None) -> str:
    parts = urlsplit(str(url or "").strip())
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if not host:
        return ""
    path = parts.path.rstrip("/")
    return f"{(parts.scheme or 'https').lower()}://{host}{path}"


def canonical_key(posting: dict) -> str:
    """Cross-source dedupe key `company|title|location|url`, or "" when it cannot be trusted.

    Pure: no network, no I/O. All four parts must be non-empty; an empty key means "do not
    dedupe this row", because over-merging two real postings is worse than one duplicate.
    """
    company = _words(posting.get("company"))
    if len(company) > 1 and company[0] == "the":
        company = company[1:]
    while len(company) > 1 and company[-1] in _LEGAL_SUFFIXES:
        company = company[:-1]
    title = _words(_PAREN_RE.sub(" ", str(posting.get("title") or "")))
    location = _words(posting.get("location"))
    url = _canonical_url(posting.get("url"))
    parts = [" ".join(company), " ".join(title), " ".join(location), url]
    if not all(parts):
        return ""
    return "|".join(parts)


# ---------------------------------------------------------------- ATS adapters

def fetch_greenhouse(token: str, company: str) -> list[dict]:
    payload = _get_json(GREENHOUSE_URL.format(token=token))
    out = []
    for job in _items(payload, "jobs"):
        if job.get("id") is None:
            continue
        location = _clean_plain((job.get("location") or {}).get("name", ""))
        out.append(_posting(
            "greenhouse", token, job["id"],
            company or _clean_plain(job.get("company_name", "")),
            _clean_plain(job.get("title", "")),
            location,
            job.get("absolute_url", ""),
            _strip_html(job.get("content", "")),
            (job.get("updated_at") or "")[:10],
            _location_is_remote(location),
        ))
    return out


def fetch_lever(token: str, company: str) -> list[dict]:
    payload = _get_json(LEVER_URL.format(token=token))
    out = []
    for job in _items(payload, None):
        if job.get("id") is None:
            continue
        location = _clean_plain((job.get("categories") or {}).get("location", ""))
        workplace = str(job.get("workplaceType") or "").lower()
        description = job.get("descriptionPlain") or _strip_html(job.get("description", ""))
        out.append(_posting(
            "lever", token, job["id"],
            company,
            _clean_plain(job.get("text", "")),
            location,
            job.get("hostedUrl", ""),
            _clean_plain(description),
            _date_from_epoch(job.get("createdAt"), millis=True),
            workplace == "remote" or _location_is_remote(location),
        ))
    return out


def fetch_ashby(token: str, company: str) -> list[dict]:
    payload = _get_json(ASHBY_URL.format(token=token))
    out = []
    for job in _items(payload, "jobs"):
        if job.get("id") is None:
            continue
        location = _clean_plain(job.get("location", ""))
        description = job.get("descriptionPlain") or _strip_html(job.get("descriptionHtml", ""))
        out.append(_posting(
            "ashby", token, job["id"],
            company,
            _clean_plain(job.get("title", "")),
            location,
            job.get("jobUrl", ""),
            _clean_plain(description),
            _date_from_iso(job.get("publishedAt")),
            bool(job.get("isRemote"))
            or str(job.get("workplaceType") or "").lower() == "remote"
            or _location_is_remote(location),
        ))
    return out


# ---------------------------------------------------------------- SmartRecruiters / Workable

_COUNTRY_NAMES = {"ca": "Canada", "us": "United States", "gb": "United Kingdom",
                  "de": "Germany", "fr": "France", "nl": "Netherlands", "ie": "Ireland"}


def _sr_location_text(location: dict) -> str:
    full = _clean_plain(location.get("fullLocation", ""))
    if full:
        return full
    country = str(location.get("country") or "")
    parts = [location.get("city"), location.get("region"),
             _COUNTRY_NAMES.get(country.lower(), country)]
    return ", ".join(p for p in (_clean_plain(x) for x in parts) if p)


def fetch_smartrecruiters(token: str, company: str, max_details: int = 40) -> list[dict]:
    """List once, then fetch detail only for Canadian items (the list carries no description)."""
    payload = _get_json(SMARTRECRUITERS_URL.format(token=token))
    # Canada-only by design: this adapter drops every posting whose country is not ca/canada,
    # unlike the other adapters, which do not filter by country here. Pointed at a
    # non-Canadian board, it returns nothing.
    kept = []
    for item in _items(payload, "content"):
        location = item.get("location") or {}
        country = str(location.get("country") or "").lower()
        if item.get("id") is None or country not in ("ca", "canada"):
            continue
        kept.append(item)
    out = []
    for item in kept[:max_details]:
        pid = item["id"]
        try:
            detail = _get_json(SMARTRECRUITERS_DETAIL_URL.format(token=token, pid=pid))
        except FetchError:
            continue  # one unreachable detail must not sink the board
        if not isinstance(detail, dict):
            continue
        sections = ((detail.get("jobAd") or {}).get("sections")) or {}
        description = "\n".join(
            text for text in (
                _strip_html((sections.get(key) or {}).get("text", ""))
                for key in ("jobDescription", "qualifications"))
            if text)
        location = item.get("location") or {}
        location_text = _sr_location_text(location)
        out.append(_posting(
            "smartrecruiters", token, pid,
            company or _clean_plain((item.get("company") or {}).get("name", "")),
            _clean_plain(item.get("name", "")),
            location_text,
            detail.get("postingUrl") or f"https://jobs.smartrecruiters.com/{token}/{pid}",
            description,
            _date_from_iso(item.get("releasedDate")),
            bool(location.get("remote")) or _location_is_remote(location_text),
        ))
    return out


def fetch_workable(token: str, company: str) -> list[dict]:
    """One row per posting. Workable lists a multi-city job once per location under one
    shortcode, so repeated codes merge their locations into one row instead of emitting
    several rows that would all collide on the same uid."""
    payload = _get_json(WORKABLE_URL.format(token=token))
    out: list[dict] = []
    by_code: dict[str, dict] = {}
    for job in _items(payload, "jobs"):
        shortcode = job.get("shortcode")
        if not shortcode:
            continue
        location = ", ".join(p for p in (_clean_plain(job.get(k))
                                         for k in ("city", "state", "country")) if p)
        remote = bool(job.get("telecommuting")) or _location_is_remote(location)
        row = by_code.get(shortcode)
        if row is not None:
            places = [p for p in row["location"].split(" / ") if p]
            if location and location not in places:
                row["location"] = " / ".join(places + [location])
            row["remote"] = row["remote"] or remote
            continue
        row = _posting(
            "workable", token, shortcode,
            company or _clean_plain(payload.get("name", "")),
            _clean_plain(job.get("title", "")),
            location,
            job.get("url") or job.get("shortlink") or "",
            _strip_html(job.get("description", "")),
            _date_from_iso(job.get("published_on") or job.get("created_at")),
            remote,
        )
        by_code[shortcode] = row
        out.append(row)
    return out


# ---------------------------------------------------------------- aggregators

def fetch_remotive(limit: int = 300) -> list[dict]:
    payload = _get_json(REMOTIVE_URL.format(limit=limit))
    out = []
    for job in _items(payload, "jobs"):
        if job.get("id") is None:
            continue
        out.append(_posting(
            "remotive", "remotive", job["id"],
            _clean_plain(job.get("company_name", "")),
            _clean_plain(job.get("title", "")),
            _clean_plain(job.get("candidate_required_location", "")),
            job.get("url", ""),
            _strip_html(job.get("description", "")),
            _date_from_iso(job.get("publication_date")),
            True,
        ))
    return out


def fetch_remoteok() -> list[dict]:
    payload = _get_json(REMOTEOK_URL)
    out = []
    # item[0] is a legal-notice dict with no id/position: skipped by the filter.
    for job in _items(payload, None):
        if job.get("id") is None or not job.get("position"):
            continue
        out.append(_posting(
            "remoteok", "remoteok", job["id"],
            _clean_plain(job.get("company", "")),
            _clean_plain(job.get("position", "")),
            _clean_plain(job.get("location", "")),
            job.get("url", ""),
            _strip_html(job.get("description", "")),
            _date_from_iso(job.get("date")),
            True,
        ))
    return out


def fetch_arbeitnow() -> list[dict]:
    payload = _get_json(ARBEITNOW_URL)
    out = []
    for job in _items(payload, "data"):
        slug = job.get("slug")
        if not slug:
            continue
        location = _clean_plain(job.get("location", ""))
        out.append(_posting(
            "arbeitnow", "arbeitnow", slug,
            _clean_plain(job.get("company_name", "")),
            _clean_plain(job.get("title", "")),
            location,
            job.get("url", ""),
            _strip_html(job.get("description", "")),
            _date_from_epoch(job.get("created_at")),
            bool(job.get("remote")) or _location_is_remote(location),
        ))
    return out


# ---------------------------------------------------------------- fan-out

def fetch_board(entry: dict) -> list[dict]:
    """Dispatch one registry entry (or aggregator pseudo-entry) to its adapter."""
    ats = str(entry.get("ats") or "").lower()
    token = str(entry.get("token") or "")
    name = str(entry.get("name") or "")
    if ats == "greenhouse":
        return fetch_greenhouse(token, name)
    if ats == "lever":
        return fetch_lever(token, name)
    if ats == "ashby":
        return fetch_ashby(token, name)
    if ats == "remotive":
        return fetch_remotive()
    if ats == "remoteok":
        return fetch_remoteok()
    if ats == "arbeitnow":
        return fetch_arbeitnow()
    if ats == "smartrecruiters":
        return fetch_smartrecruiters(token, name)
    if ats == "workable":
        return fetch_workable(token, name)
    raise FetchError(f"unknown ats {ats!r}")


_AGGREGATORS = [
    {"name": "Remotive", "ats": "remotive", "token": "remotive"},
    {"name": "RemoteOK", "ats": "remoteok", "token": "remoteok"},
    {"name": "Arbeitnow", "ats": "arbeitnow", "token": "arbeitnow"},
]


def _label(entry) -> str:
    if not isinstance(entry, dict):
        return repr(entry)[:60]
    return f"{entry.get('ats')}:{entry.get('token') or entry.get('name')}"


def fetch_all(registry: list[dict], include_aggregators: bool = True
              ) -> tuple[list[dict], list[str]]:
    """Fetch every registry entry (+ aggregators). Never raises.

    Returns (postings, errors). A failed or empty source yields one error string
    like "greenhouse:cloudflare: HTTP 404" or "ashby:acme: 0 postings (dead board?)".
    """
    entries = [e for e in (registry or [])]
    if include_aggregators:
        entries += _AGGREGATORS

    postings: list[dict] = []
    errors: list[str] = []
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futures = [(e, pool.submit(fetch_board, e)) for e in entries]
            for entry, fut in futures:
                label = _label(entry)
                try:
                    got = fut.result()
                except FetchError as e:
                    errors.append(f"{label}: {e.reason}")
                    continue
                except Exception as e:  # one bad source must not kill the run
                    errors.append(f"{label}: {type(e).__name__}: {e}")
                    continue
                if not got:
                    errors.append(f"{label}: 0 postings (dead board?)")
                    continue
                postings.extend(got)
    except Exception as e:  # last-resort guard: fetch_all must never raise
        errors.append(f"fetch_all: {type(e).__name__}: {e}")
    return postings, errors
