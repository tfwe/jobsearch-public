"""Cover-letter layout and compilation.

Prose is never generated here. The body is authored text read from a plain-text
file; this module only escapes it, lays it out in a LaTeX letter and compiles it
with tectonic.
"""
from __future__ import annotations

import os
import subprocess
from datetime import date
from pathlib import Path

from .store import Store

# facts.yaml is the contact source; prefer passing facts_path explicitly.
DEFAULT_FACTS = str(Path.home() / "resume" / "facts.yaml")
# The LaTeX engine, on PATH by default. Override with JOBS_TECTONIC or per call.
DEFAULT_TECTONIC = os.environ.get("JOBS_TECTONIC") or "tectonic"
PACKETS_DIR = Path.home() / "jobsearch" / "packets"
CONTACT_KEYS = ("name", "location", "phone", "email", "linkedin_url", "github_url")

_LATEX_SPECIALS = {
    "\\": r"\textbackslash{}",
    "&": r"\&",
    "%": r"\%",
    "$": r"\$",
    "#": r"\#",
    "_": r"\_",
    "{": r"\{",
    "}": r"\}",
    "~": r"\textasciitilde{}",
    "^": r"\textasciicircum{}",
}


def escape_latex(text: str) -> str:
    """Escape every LaTeX special character in one pass (no double escaping).

    Also breaks the f|i, f|l and f|f ligature pairs with an empty italic
    correction (``\\/``). Tectonic is XeTeX, where microtype's
    \\DisableLigatures is unavailable and fontspec's Ligatures=NoCommon leaves
    ff/ffi ligated, so a per-pair break is the only engine-safe switch. The
    break is invisible when rendered but stops pdftotext from emitting
    U+FB00-U+FB06, which would corrupt emails, URLs and words for ATS parsers.
    """
    out = []
    for i, ch in enumerate(text):
        out.append(_LATEX_SPECIALS.get(ch, ch))
        if ch == "f" and i + 1 < len(text) and text[i + 1] in "fil":
            out.append(r"\/")
    return "".join(out)


def slug_for_uid(uid: str) -> str:
    return uid.replace(":", "_").replace("/", "_")


def default_out_dir(uid: str) -> Path:
    return PACKETS_DIR / slug_for_uid(uid)


def _load_contact(facts_path: str) -> dict:
    """Read the `contact:` block of facts.yaml.

    Stdlib-only on purpose: this module never imports yaml, so the reader below is enough. The
    contact block is flat `key: value` lines, so a small reader is enough.
    """
    contact: dict = {}
    in_contact = False
    for raw in Path(facts_path).expanduser().read_text(encoding="utf-8").splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        if not raw[0].isspace():
            in_contact = raw.rstrip() == "contact:"
            continue
        if not in_contact:
            continue
        key, sep, val = raw.strip().partition(":")
        if not sep:
            continue
        val = val.strip()
        if val[:1] in ("'", '"'):
            end = val.find(val[0], 1)
            val = val[1:end] if end > 0 else val[1:]
        else:
            val = val.split(" #", 1)[0].strip()
        contact[key.strip()] = val
    missing = [k for k in CONTACT_KEYS if not contact.get(k)]
    if missing:
        raise KeyError(f"facts.yaml contact is missing: {', '.join(missing)}")
    return {k: contact[k] for k in CONTACT_KEYS}


def contact_location(facts_path: str | None = None) -> str:
    """The contact location from facts.yaml ('Winnipeg, MB'), or '' when unreadable.

    DEFAULT_FACTS is read at call time rather than bound as a default argument, so a
    test or an operator with the resume somewhere else can point it elsewhere.
    """
    try:
        return _load_contact(facts_path or DEFAULT_FACTS).get("location", "")
    except (KeyError, OSError, UnicodeDecodeError):
        return ""


def _body_blocks(text: str) -> list[tuple[str, object]]:
    """Split plain text into ('p', str) prose paragraphs and ('ul', [str]) bullet lists.

    Blank lines end a paragraph. Consecutive '- ' lines form one list. Other
    consecutive lines are soft-wrapped into one paragraph.
    """
    blocks: list[tuple[str, object]] = []
    para: list[str] = []

    def flush() -> None:
        if para:
            blocks.append(("p", " ".join(para)))
            para.clear()

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            flush()
        elif line.startswith("- "):
            flush()
            item = line[2:].strip()
            if blocks and blocks[-1][0] == "ul":
                blocks[-1][1].append(item)  # type: ignore[union-attr]
            else:
                blocks.append(("ul", [item]))
        else:
            para.append(line)
    flush()
    return blocks


def _body_latex(text: str) -> str:
    blocks = _body_blocks(text)
    if not blocks:
        raise ValueError("letter body is empty")
    parts = []
    for kind, val in blocks:
        if kind == "p":
            parts.append(escape_latex(str(val)))
        else:
            items = "\n".join(f"\\item{{}} {escape_latex(i)}" for i in val)  # type: ignore[union-attr]
            parts.append("\\begin{itemize}\n" + items + "\n\\end{itemize}")
    return "\n\n".join(parts)


def _today() -> str:
    d = date.today()
    return f"{d:%B} {d.day}, {d.year}"


_PREAMBLE = r"""\documentclass[11pt,letterpaper]{article}
\usepackage[letterpaper,margin=0.85in]{geometry}
\usepackage[T1]{fontenc}
\usepackage[utf8]{inputenc}
\pagestyle{empty}
\raggedright
\setlength{\parindent}{0pt}
\setlength{\parskip}{0.7em}
\setlength{\itemsep}{2pt}
\setlength{\topsep}{2pt}
\setlength{\leftmargini}{1.5em}
\begin{document}
"""


def _build_tex(contact: dict, title: str, company: str, body: str) -> str:
    name = escape_latex(contact["name"])
    sep = r" \textbullet{} "
    head = (
        r"\begin{center}" "\n"
        rf"{{\LARGE\bfseries {name}}}\\[4pt]" "\n"
        + sep.join(escape_latex(contact[k]) for k in ("location", "phone", "email")) + r"\\" "\n"
        + sep.join(escape_latex(contact[k]) for k in ("linkedin_url", "github_url")) + "\n"
        r"\end{center}" "\n"
        r"\vspace{0.8em}" "\n"
    )
    subject = f"Re: {escape_latex(title)} -- {escape_latex(company)}"
    closing = (
        "Best regards,\\\\[14pt]\n"
        f"{name}\n"
    )
    return (
        _PREAMBLE
        + head
        + escape_latex(_today()) + "\\par\n"
        + r"\vspace{0.4em}" "\n"
        + rf"\textbf{{{subject}}}\par" "\n"
        + r"\vspace{0.4em}" "\n"
        + _body_latex(body) + "\n\n"
        + r"\vspace{0.6em}" "\n"
        + closing
        + r"\end{document}" "\n"
    )


def _compile(tex_path: Path, out_dir: Path, tectonic: str) -> Path:
    pdf = out_dir / (tex_path.stem + ".pdf")
    if pdf.exists():
        pdf.unlink()  # never let a stale PDF mask a failed build
    try:
        r = subprocess.run(
            [tectonic, "--outdir", str(out_dir), "--chatter", "minimal", str(tex_path)],
            capture_output=True, text=True, timeout=300)
    except OSError as e:
        raise RuntimeError(f"cannot run tectonic at {tectonic}: {e}") from e
    if r.returncode != 0 or not pdf.exists():
        raise RuntimeError(
            f"tectonic failed (exit {r.returncode}): {(r.stderr or r.stdout)[-2000:]}")
    return pdf


def render_letter(uid: str, body_path: str, out_dir: str | None = None,
                  facts_path: str = DEFAULT_FACTS,
                  tectonic: str = DEFAULT_TECTONIC) -> str:
    """Lay out and compile the cover letter for `uid`. Returns the absolute PDF path."""
    store = Store()
    try:
        row = store.get(uid)
    finally:
        store.close()
    if row is None:
        raise KeyError(f"no posting with uid {uid!r}")
    contact = _load_contact(facts_path)
    body = Path(body_path).expanduser().read_text(encoding="utf-8")
    target = Path(out_dir).expanduser() if out_dir else default_out_dir(uid)
    target.mkdir(parents=True, exist_ok=True)
    tex = _build_tex(contact, row["title"] or "", row["company"] or "", body)
    tex_path = target / "cover_letter.tex"
    tex_path.write_text(tex, encoding="utf-8")
    pdf = _compile(tex_path, target, tectonic)
    return str(pdf.resolve())


def letter_brief(uid: str, store=None) -> str:
    """Plain-text writer input for a posting. Facts only, description verbatim."""
    owned = store is None
    if owned:
        store = Store()
    try:
        row = store.get(uid)
    finally:
        if owned:
            store.close()
    if row is None:
        raise KeyError(f"no posting with uid {uid!r}")
    score = row.get("score")
    reasons = row.get("reasons") or []
    lines = [
        f"Company: {row.get('company') or ''}",
        f"Title: {row.get('title') or ''}",
        f"Location: {row.get('location') or ''}",
        f"URL: {row.get('url') or ''}",
        f"Variant: {row.get('variant') or ''}",
        f"Score: {'%g' % score if score is not None else '(not scored)'}",
        "Reasons:",
    ]
    lines += [f"- {r}" for r in reasons] or ["- (none)"]
    lines += ["Description:", row.get("description") or ""]
    return "\n".join(lines) + "\n"
