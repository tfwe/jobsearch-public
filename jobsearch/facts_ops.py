"""Parse, validate and apply the facts-UI model's JSON ops.

Pure module: no I/O beyond the strings it is given. The worker wiring that calls
it lives elsewhere (this module must not import copilot or serve).

Model answer contract: one raw JSON object, no fences, no prose:
    {"status": "propose|no_change|clarify|refuse",
     "target": "preferences.yaml|facts.yaml",
     "summary": "...", "question": "...", "reason": "...",
     "ops": [{"op": "set|upsert|delete", "path": "...", "value": <any>}]}
"""
from __future__ import annotations

import difflib
import json
import re
from datetime import datetime, timezone

import yaml

STATUSES = frozenset({"propose", "no_change", "clarify", "refuse"})
TARGETS = frozenset({"preferences.yaml", "facts.yaml"})
OPS = frozenset({"set", "upsert", "delete"})
WORK_AUTH_STATUSES = frozenset({"authorized", "requires_sponsorship", "not_authorized"})

MAX_OPS = 10
MAX_OPS_BYTES = 4096
SUMMARY_MIN, SUMMARY_MAX = 5, 240

COUNTRY_CODE = re.compile(r"^[A-Z]{2}$")
NUMERIC_TOKEN = re.compile(r"\$?\d[\d,]*(?:\.\d+)?%?")  # same shape as copilot.NUMERIC_TOKEN

PREFS_EXACT = frozenset({
    "relocation.countries",
    "relocation.cities",
    "relocation.exclude_regions",
    "relocation.reason",
})
PREFS_WORK_AUTH = re.compile(r"^work_authorization\.([^.]+)\.(status|reason)$")
FACTS_EXACT = frozenset({"contact.location", "contact.phone", "contact.email"})
FACTS_APPEND = frozenset({"shared_claims"})
PROTECTED_PREFIXES = ("employment", "education", "certifications", "projects")

_STAMP_ROOTS = ("relocation", "work_authorization")


class FactsOpsError(Exception):
    """A failure the caller must handle: bad answer, bad op, or unapplicable path."""


class _FlowList(list):
    """A list emitted inline as [CA], the shape match.py's line reader accepts."""


def _flow_representer(dumper, data):
    return dumper.represent_sequence("tag:yaml.org,2002:seq", list(data), flow_style=True)


yaml.SafeDumper.add_representer(_FlowList, _flow_representer)

# Lists match.py reads on one line. Written block-style they would parse as a malformed file and
# load_preferences() would quietly return its empty defaults, losing every preference at once.
FLOW_PATHS = (("relocation", "countries"), ("relocation", "cities"),
              ("relocation", "exclude_regions"))


def _flow_lists(data: dict) -> None:
    """Mark the relocation lists so the dump writes them inline."""
    for root, key in dict.fromkeys(FLOW_PATHS):
        block = data.get(root)
        if isinstance(block, dict) and isinstance(block.get(key), list):
            block[key] = _FlowList(block[key])


def parse_answer(text: str) -> tuple[dict | None, str | None]:
    """Parse the raw model answer. Whitespace is stripped; nothing else is repaired.

    Fenced or prose-wrapped JSON fails with invalid_json on purpose.
    """
    stripped = text.strip()
    if not stripped:
        return None, "empty_output"
    if any(ord(ch) > 127 for ch in stripped):
        return None, "non_ascii"
    try:
        payload = json.loads(stripped)
    except ValueError:
        return None, "invalid_json"
    if not isinstance(payload, dict):
        return None, "invalid_json"
    return payload, None


def _ascii_ok(value) -> bool:
    if isinstance(value, str):
        return all(ord(ch) <= 127 for ch in value)
    if isinstance(value, dict):
        return all(_ascii_ok(k) and _ascii_ok(v) for k, v in value.items())
    if isinstance(value, list):
        return all(_ascii_ok(v) for v in value)
    return True


def _leaves(value):
    """Yield every scalar leaf of a value (strings, numbers, bools, None)."""
    if isinstance(value, dict):
        for k, v in value.items():
            yield from _leaves(v)
    elif isinstance(value, list):
        for v in value:
            yield from _leaves(v)
    else:
        yield value


def _numeric_tokens(text: str) -> set[str]:
    return {m.group(0).rstrip(",") for m in NUMERIC_TOKEN.finditer(text)}


def _value_numbers(value) -> list[str]:
    found: list[str] = []
    for leaf in _leaves(value):
        if isinstance(leaf, bool) or leaf is None:
            continue
        if isinstance(leaf, (int, float)):
            found.extend(_numeric_tokens(str(leaf)))
        elif isinstance(leaf, str):
            found.extend(_numeric_tokens(leaf))
    return found


def _path_allowed(target: str, path: str) -> bool:
    if target == "preferences.yaml":
        return path in PREFS_EXACT or PREFS_WORK_AUTH.match(path) is not None
    return path in FACTS_EXACT or path in FACTS_APPEND


def _is_protected(path: str) -> bool:
    head = path.split(".", 1)[0]
    return head in PROTECTED_PREFIXES


def validate(
    payload: dict,
    *,
    facts_text: str,
    prefs_text: str,
    confirm_protected: bool = False,
    instruction_text: str = "",
) -> tuple[list[dict] | None, str | None]:
    """Check a parsed answer. Returns (ops, None) or (None, error_string)."""
    if not isinstance(payload, dict):
        return None, "invalid_payload"

    status = payload.get("status")
    if status not in STATUSES:
        return None, f"invalid_status: {status!r}"
    target = payload.get("target")
    if target not in TARGETS:
        return None, f"invalid_target: {target!r}"

    if status != "propose":
        return [], None

    ops = payload.get("ops")
    if not isinstance(ops, list) or not (1 <= len(ops) <= MAX_OPS):
        count = len(ops) if isinstance(ops, list) else type(ops).__name__
        return None, f"ops_count: {count} (need 1 to {MAX_OPS})"
    encoded = json.dumps(ops, ensure_ascii=True, separators=(",", ":")).encode("ascii")
    if len(encoded) > MAX_OPS_BYTES:
        return None, f"ops_too_large: {len(encoded)} bytes (max {MAX_OPS_BYTES})"

    summary = payload.get("summary")
    if not isinstance(summary, str) or not (SUMMARY_MIN <= len(summary) <= SUMMARY_MAX):
        return None, f"summary_length (need {SUMMARY_MIN} to {SUMMARY_MAX} chars)"
    if not all(ord(ch) <= 127 for ch in summary):
        return None, "non_ascii: summary"

    allowed_numbers = (
        _numeric_tokens(facts_text)
        | _numeric_tokens(prefs_text)
        | _numeric_tokens(instruction_text)
    )

    for raw in ops:
        if not isinstance(raw, dict) or set(raw) - {"op", "path", "value"}:
            return None, f"invalid_op: {raw!r}"
        op = raw.get("op")
        path = raw.get("path")
        if op not in OPS:
            return None, f"invalid_op: {op!r}"
        if not isinstance(path, str) or not path:
            return None, f"invalid_path: {path!r}"
        if op != "delete" and "value" not in raw:
            return None, f"missing_value: {path}"
        value = raw.get("value")

        if not _path_allowed(target, path):
            if not (
                target == "facts.yaml"
                and _is_protected(path)
                and confirm_protected
                and all(
                    str(leaf) in facts_text
                    for leaf in _leaves(value)
                    if leaf is not None
                )
            ):
                return None, f"path_not_allowed: {path}"
        if path in FACTS_APPEND and op != "upsert":
            return None, f"invalid_op_for_path: {path} takes upsert (append)"

        if not _ascii_ok(value):
            return None, f"non_ascii_value: {path}"

        m = PREFS_WORK_AUTH.match(path)
        if m and not COUNTRY_CODE.match(m.group(1)):
            return None, f"invalid_country_code: {m.group(1)!r} in {path}"
        if m and m.group(2) == "status" and op != "delete":
            if value not in WORK_AUTH_STATUSES:
                return None, f"invalid_work_authorization: {value!r} in {path}"
        if path == "relocation.countries" and op != "delete":
            codes = value if isinstance(value, list) else [value]
            for code in codes:
                if not isinstance(code, str) or not COUNTRY_CODE.match(code):
                    return None, f"invalid_country_code: {code!r} in {path}"

        for token in _value_numbers(value):
            if token not in allowed_numbers:
                return None, f"unsupported_number: {token} in {path}"

    return ops, None


def _load(text: str) -> dict:
    if not text or not text.strip():
        return {"version": 1}
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise FactsOpsError(f"yaml_parse_failed: {exc}") from exc
    if data is None:
        return {"version": 1}
    if not isinstance(data, dict):
        raise FactsOpsError("yaml_root_not_mapping")
    return data


def _walk_parent(data: dict, parts: list[str], path: str, create: bool) -> dict:
    node = data
    for part in parts[:-1]:
        nxt = node.get(part)
        if nxt is None:
            if not create:
                raise FactsOpsError(f"path_absent: {path}")
            nxt = {}
            node[part] = nxt
        if not isinstance(nxt, dict):
            raise FactsOpsError(f"path_not_mapping: {path}")
        node = nxt
    return node


def _ensure_mapping(data: dict, parts: list[str], name: str) -> dict:
    node = data
    for part in parts:
        nxt = node.get(part)
        if nxt is None:
            nxt = {}
            node[part] = nxt
        if not isinstance(nxt, dict):
            raise FactsOpsError(f"path_not_mapping: {name}")
        node = nxt
    return node


def _stamp_targets(ops: list[dict]) -> set[str]:
    roots: set[str] = set()
    for raw in ops:
        path = raw.get("path", "")
        head = path.split(".", 1)[0]
        if head == "relocation":
            roots.add("relocation")
        elif head == "work_authorization":
            parts = path.split(".")
            if len(parts) >= 2:
                roots.add(f"work_authorization.{parts[1]}")
    return roots


def apply(text: str, ops: list[dict]) -> str:
    """Apply ops in order to the YAML text and return re-serialized YAML.

    Empty or missing text starts from {"version": 1}. set requires the key to exist,
    upsert creates it, delete removes it. upsert on shared_claims appends to the list.
    updated_at is stamped in UTC on relocation and on each touched work_authorization.<CC>.
    """
    data = _load(text)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    for raw in ops:
        if not isinstance(raw, dict):
            raise FactsOpsError(f"invalid_op: {raw!r}")
        op = raw.get("op")
        path = raw.get("path")
        if op not in OPS or not isinstance(path, str) or not path:
            raise FactsOpsError(f"invalid_op: {op!r} {path!r}")
        parts = path.split(".")
        leaf = parts[-1]

        if op == "delete":
            parent = _walk_parent(data, parts, path, create=False)
            if leaf not in parent:
                raise FactsOpsError(f"path_absent: {path}")
            del parent[leaf]
            continue

        if "value" not in raw:
            raise FactsOpsError(f"missing_value: {path}")
        value = raw["value"]

        if op == "set":
            parent = _walk_parent(data, parts, path, create=False)
            if leaf not in parent:
                raise FactsOpsError(f"path_absent: {path}")
            parent[leaf] = value
        else:  # upsert
            parent = _walk_parent(data, parts, path, create=True)
            existing = parent.get(leaf)
            if path in FACTS_APPEND:
                if existing is None:
                    parent[leaf] = [value]
                elif isinstance(existing, list):
                    existing.append(value)
                else:
                    raise FactsOpsError(f"not_a_list: {path}")
            else:
                parent[leaf] = value

    for root in sorted(_stamp_targets(ops)):
        block = _ensure_mapping(data, root.split("."), root)
        block["updated_at"] = now

    _flow_lists(data)
    out = yaml.safe_dump(
        data,
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=False,
        indent=2,
    )
    if not out.endswith("\n"):
        out += "\n"
    return out


def upgrade_absent_sets(text: str, ops: list[dict]) -> tuple[list[dict], list[str]]:
    """Turn a set on an absent path into an upsert; return the ops and the paths changed.

    The model reaches for set on a key it believes exists. On an empty or partial file that is a
    path_absent failure and the instruction dies with it, which is exactly the first-run case:
    preferences.yaml does not exist yet. upsert creates the same key, and the preview diff shows
    the addition, so nothing is hidden by the rewrite.
    """
    data = _load(text)
    out: list[dict] = []
    changed: list[str] = []
    for op in ops:
        if isinstance(op, dict) and op.get("op") == "set":
            path = str(op.get("path") or "")
            cursor: object = data
            absent = False
            for part in path.split("."):
                if not isinstance(cursor, dict) or part not in cursor:
                    absent = True
                    break
                cursor = cursor[part]
            if absent:
                op = dict(op, op="upsert")
                changed.append(path)
        out.append(op)
    return out, changed


def diff_text(before: str, after: str, name: str) -> str:
    """Unified diff of two texts, labelled with the file name."""
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{name}",
            tofile=f"b/{name}",
        )
    )
