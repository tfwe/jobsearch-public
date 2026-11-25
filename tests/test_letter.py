import shutil
import subprocess
import unicodedata
from pathlib import Path

import pytest

from jobsearch import letter
from jobsearch.letter import escape_latex, letter_brief, render_letter
from jobsearch.store import Store

# The PDF tests shell out to tectonic, pdftotext and pdfinfo. Without them the file
# stays importable and the pure-function tests still run, so a bare clone is green.
_PDF_TOOLS = ("tectonic", "pdftotext", "pdfinfo")
_MISSING = [t for t in _PDF_TOOLS if shutil.which(t) is None]
needs_pdf_tools = pytest.mark.skipif(bool(_MISSING), reason="requires " + ", ".join(_MISSING))

UID = "greenhouse:dropbox:8328549"
DESC = ("Security Engineers at Dropbox safeguard our digital ecosystem. "
        "Build detection for LLM misuse & prompt injection (50% of the work).")

VOCAB = ("security detection platform engineer model review incident triage signal "
         "analysis pipeline customer team automation response design quality research "
         "threat data review careful measured steady work code tests logs alerts").split()


def _words(n, start=0):
    out = [VOCAB[(start + i) % len(VOCAB)] for i in range(n)]
    return " ".join(out)


def _body(total_words, marker_start="ALPHASTART", marker_end="ZULUEND"):
    words = _words(total_words - 2).split()
    paras = []
    for i in range(0, len(words), 55):
        paras.append(" ".join(words[i:i + 55]))
    paras[0] = marker_start + " " + paras[0]
    paras[-1] = paras[-1] + " " + marker_end
    return "\n\n".join(paras) + "\n"


def _pages(pdf):
    out = subprocess.run(["pdfinfo", pdf], capture_output=True, text=True, check=True).stdout
    line = [l for l in out.splitlines() if l.startswith("Pages:")][0]
    return int(line.split()[1])


def _text(pdf):
    raw = subprocess.run(["pdftotext", "-layout", pdf, "-"], capture_output=True,
                         text=True, check=True).stdout
    return unicodedata.normalize("NFKC", raw)  # undo fi/fl ligatures in the text layer


FACTS = """version: 1

contact:
  name: "Alex Morgan"
  phone: "(555) 010-0199"
  location: "Winnipeg, MB"
  email: "alex.morgan@example.com"
  github: "github.com/example-applicant"
  github_url: "https://github.com/example-applicant"
  linkedin: "/in/example-applicant/"
  linkedin_url: "https://www.linkedin.com/in/example-applicant/"

certifications:
  - id: sc200
"""


@pytest.fixture
def env(tmp_path, monkeypatch):
    db = tmp_path / "jobs.db"
    monkeypatch.setenv("JOBS_DB", str(db))
    s = Store(str(db))
    s.upsert_postings([{
        "uid": UID, "source": "greenhouse", "company": "Dropbox",
        "title": "Senior Security Engineer, AI Security",
        "location": "Remote - Canada: Select locations",
        "url": "https://jobs.dropbox.com/listing/8328549?gh_jid=8328549",
        "description": DESC, "posted_at": "2026-11-10", "remote": True}])
    s.set_score(UID, 24.0, "ai-security", ["title family keyword 'ai security' (+16)"])
    s.close()
    facts = tmp_path / "facts.yaml"
    facts.write_text(FACTS, encoding="utf-8")
    return {"tmp": tmp_path, "facts": str(facts), "db": str(db)}


# --- escape_latex -----------------------------------------------------------

@pytest.mark.parametrize("ch,expected", [
    ("&", r"\&"), ("%", r"\%"), ("$", r"\$"), ("#", r"\#"), ("_", r"\_"),
    ("{", r"\{"), ("}", r"\}"), ("~", r"\textasciitilde{}"),
    ("^", r"\textasciicircum{}"), ("\\", r"\textbackslash{}"),
])
def test_escape_each_special(ch, expected):
    assert escape_latex(ch) == expected


def test_escape_all_specials_in_one_string_no_double_escape():
    out = escape_latex("a&b%c$d#e_f{g}h~i^j\\k")
    assert out == (r"a\&b\%c\$d\#e\_f\{g\}h\textasciitilde{}i\textasciicircum{}j"
                   r"\textbackslash{}k")


def test_escape_bare_url():
    url = "https://example.com/a_b?x=1&y=50%#top~^"
    out = escape_latex(url)
    assert out == (r"https://example.com/a\_b?x=1\&y=50\%\#top"
                   r"\textasciitilde{}\textasciicircum{}")
    # no unescaped special survives
    stripped = out
    for cmd in (r"\&", r"\%", r"\_", r"\#", r"\textasciitilde{}", r"\textasciicircum{}"):
        stripped = stripped.replace(cmd, "")
    assert not any(c in stripped for c in "&%$#_{}~^")


def test_escape_leaves_plain_text_alone():
    assert escape_latex("Senior Security Engineer, AI Security") == \
        "Senior Security Engineer, AI Security"


@needs_pdf_tools
def test_special_characters_compile_to_literal_text(env):
    body = env["tmp"] / "b.txt"
    body.write_text("Rate is 50% & $5 #1 snake_case {x} ~ ^ \\ done.\n", encoding="utf-8")
    pdf = render_letter(UID, str(body), out_dir=str(env["tmp"] / "out"),
                        facts_path=env["facts"])
    txt = _text(pdf)
    assert "50% & $5 #1 snake_case {x} ~ ^" in txt
    assert "\\" in txt


# --- body layout -------------------------------------------------------------

def test_body_blocks_paragraphs_and_bullets():
    text = "First para line one\nline two.\n\nSecond para.\n\n- alpha\n- beta\n\nThird."
    blocks = letter._body_blocks(text)
    assert blocks == [
        ("p", "First para line one line two."),
        ("p", "Second para."),
        ("ul", ["alpha", "beta"]),
        ("p", "Third."),
    ]


def test_body_latex_bullets_and_paragraphs_are_latex_structures():
    latex = letter._body_latex("Para one.\n\n- item & one\n- item two\n\nPara two.")
    assert latex.count("\\begin{itemize}") == 1
    assert latex.count("\\end{itemize}") == 1
    assert "\\item{} item \\& one" in latex
    assert "\\item{} item two" in latex
    assert latex.index("Para one.") < latex.index("\\begin{itemize}") < latex.index("Para two.")
    assert "\n\n" in latex


def test_empty_body_rejected():
    with pytest.raises(ValueError):
        letter._body_latex("\n\n   \n")


@needs_pdf_tools
def test_paragraphs_and_bullets_render_in_pdf(env):
    body = env["tmp"] / "b.txt"
    body.write_text("PARAONEMARK opening line.\n\nPARATWOMARK second.\n\n"
                    "- BULLETAAA point\n- BULLETBBB point\n\nCLOSINGMARK.\n", encoding="utf-8")
    pdf = render_letter(UID, str(body), out_dir=str(env["tmp"] / "out"),
                        facts_path=env["facts"])
    assert _pages(pdf) == 1
    txt = _text(pdf)
    for m in ("PARAONEMARK", "PARATWOMARK", "BULLETAAA", "BULLETBBB", "CLOSINGMARK"):
        assert m in txt
    assert txt.index("PARATWOMARK") < txt.index("BULLETAAA") < txt.index("CLOSINGMARK")


# --- rendering ---------------------------------------------------------------

@needs_pdf_tools
def test_body_is_ragged_right_and_letterhead_centred(env):
    body = env["tmp"] / "rag300.txt"
    body.write_text(_body(300), encoding="utf-8")
    out = env["tmp"] / "rag_out"
    pdf = render_letter(UID, str(body), out_dir=str(out), facts_path=env["facts"])
    tex = (out / "cover_letter.tex").read_text(encoding="utf-8")
    preamble, _, document = tex.partition(r"\begin{document}")
    assert r"\raggedright" in preamble  # body is left-aligned, not justified
    assert document.index(r"\begin{center}") < document.index("ALPHASTART")  # letterhead still centred
    assert r"\raggedright" not in document.split(r"\end{center}")[0]  # not inside the letterhead
    assert _pages(pdf) == 1
    assert "ZULUEND" in _text(pdf)


@needs_pdf_tools
def test_300_words_is_exactly_one_page(env):
    body = env["tmp"] / "b300.txt"
    body.write_text(_body(300), encoding="utf-8")
    pdf = render_letter(UID, str(body), out_dir=str(env["tmp"] / "o300"),
                        facts_path=env["facts"])
    assert _pages(pdf) == 1
    assert "ZULUEND" in _text(pdf)


@needs_pdf_tools
def test_600_words_valid_pdf_no_clipping(env):
    body = env["tmp"] / "b600.txt"
    body.write_text(_body(600), encoding="utf-8")
    pdf = render_letter(UID, str(body), out_dir=str(env["tmp"] / "o600"),
                        facts_path=env["facts"])
    assert _pages(pdf) in (1, 2)
    txt = _text(pdf)
    assert "ALPHASTART" in txt
    assert "ZULUEND" in txt


@needs_pdf_tools
def test_letterhead_subject_and_closing_in_pdf(env):
    body = env["tmp"] / "b.txt"
    body.write_text("Short body.\n", encoding="utf-8")
    pdf = render_letter(UID, str(body), out_dir=str(env["tmp"] / "hd"),
                        facts_path=env["facts"])
    txt = _text(pdf)
    assert "Alex Morgan" in txt
    assert "alex.morgan@example.com" in txt
    assert "Re: Senior Security Engineer, AI Security -- Dropbox" in txt or \
        "Re: Senior Security Engineer, AI Security – Dropbox" in txt
    assert "Best regards," in txt


@needs_pdf_tools
def test_render_returns_absolute_path_and_writes_tex(env):
    body = env["tmp"] / "b.txt"
    body.write_text("Hello.\n", encoding="utf-8")
    out = env["tmp"] / "abs"
    pdf = render_letter(UID, str(body), out_dir=str(out), facts_path=env["facts"])
    assert pdf == str((out / "cover_letter.pdf").resolve())
    assert (out / "cover_letter.tex").exists()


def test_default_out_dir_uses_packets_slug():
    assert letter.slug_for_uid(UID) == "greenhouse_dropbox_8328549"
    assert letter.default_out_dir(UID).parts[-2:] == ("packets", "greenhouse_dropbox_8328549")


def test_unknown_uid_raises_keyerror(env):
    body = env["tmp"] / "b.txt"
    body.write_text("x\n", encoding="utf-8")
    with pytest.raises(KeyError):
        render_letter("greenhouse:nope:1", str(body), out_dir=str(env["tmp"] / "u"),
                      facts_path=env["facts"])


def test_compile_failure_raises_runtimeerror(env):
    body = env["tmp"] / "b.txt"
    body.write_text("x\n", encoding="utf-8")
    with pytest.raises(RuntimeError):
        render_letter(UID, str(body), out_dir=str(env["tmp"] / "bad"),
                      facts_path=env["facts"], tectonic="/nonexistent/tectonic")


def test_missing_contact_key_raises(env, tmp_path):
    f = tmp_path / "f.yaml"
    f.write_text("contact:\n  name: \"X\"\n", encoding="utf-8")
    with pytest.raises(KeyError):
        letter._load_contact(str(f))


def test_fixture_facts_contact_loads(tmp_path):
    f = tmp_path / "facts.yaml"
    f.write_text(FACTS, encoding="utf-8")
    c = letter._load_contact(str(f))
    assert c["name"] == "Alex Morgan"
    assert c["email"] == "alex.morgan@example.com"


# --- letter_brief -------------------------------------------------------------

def test_letter_brief_contains_posting_facts(env, monkeypatch):
    brief = letter_brief(UID)
    assert "Dropbox" in brief
    assert "Senior Security Engineer, AI Security" in brief
    assert "https://jobs.dropbox.com/listing/8328549?gh_jid=8328549" in brief
    assert DESC in brief
    assert "ai-security" in brief
    assert "title family keyword 'ai security' (+16)" in brief


def test_letter_brief_with_given_store(env):
    s = Store(env["db"])
    try:
        brief = letter_brief(UID, store=s)
    finally:
        s.close()
    assert "Dropbox" in brief and DESC in brief


def test_letter_brief_unknown_uid(env):
    with pytest.raises(KeyError):
        letter_brief("nope:nope:0")


# --- ligatures in the text layer ---------------------------------------------

LIGATURE_BODY = (
    "I built a config-driven Artificial triage agent. Write to alex.morgan@example.com "
    "for the office and sufficient flow of work.\n\n"
    "Affiliated fl ffi ff fi ffl words: effective, floor, offline, reflection.\n"
)


@needs_pdf_tools
def test_no_ligature_codepoints_in_pdf_text_layer(env):
    """ATS parsers read the raw text layer; ligature glyphs corrupt emails and words.

    Deliberately does NOT NFKC-normalise: the assertion is on what pdftotext emits.
    """
    body = env["tmp"] / "lig_body.txt"
    body.write_text(LIGATURE_BODY, encoding="utf-8")
    pdf = render_letter(UID, str(body), out_dir=str(env["tmp"] / "lig"),
                        facts_path=env["facts"])
    raw = subprocess.run(["pdftotext", pdf, "-"], capture_output=True, text=True,
                         check=True).stdout
    assert "alex.morgan@example.com" in raw
    assert "config" in raw
    assert "Artificial" in raw
    assert "effective" in raw
    bad = [hex(ord(c)) for c in raw if "\uFB00" <= c <= "\uFB06"]
    assert bad == [], f"ligature codepoints in text layer: {bad}"
    assert _pages(pdf) == 1
