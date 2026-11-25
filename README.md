# jobsearch

A local, single-user job search pipeline that finds postings on public ATS boards, scores them against one person's profile, prepares an application packet per posting, and serves the result to a browser on the local machine. It never sends anything anywhere: it prepares, it does not apply.

## What it does

The pipeline runs in this order.

1. **Scan.** `jobsearch scan` fetches every board in the `companies.yaml` registry, plus the Remotive, RemoteOK and Arbeitnow aggregators. It normalises the payloads into one SQLite table and prints the new postings, ranked.
2. **Score.** Each posting is scored deterministically from its title and skills text, seniority, location tier, blocked keywords and remote text. See [How a posting is scored](#how-a-posting-is-scored).
3. **Rescore.** `jobsearch rescore` re-scores every stored row with the current rules. It makes no network calls.
4. **Prepare.** `jobsearch prep <uid>` builds the resume PDF and stages a packet directory for one posting. See [Application packets](#application-packets).
5. **Letter.** `jobsearch letter --body-file <path> <uid>` renders a cover-letter PDF from a plain-text body.
6. **Dashboard.** `jobsearch dashboard` writes one self-contained `dashboard.html`.
7. **Serve.** `python -m jobsearch.serve` serves the dashboard and the packets over HTTP Basic auth.

Board adapters: Greenhouse, Lever, Ashby, Workable and SmartRecruiters. Aggregators: Remotive, RemoteOK and Arbeitnow.

Posting liveness: each stored posting carries `live_status` and `live_checked_at`, set by probing its URL. A posting that has gone is reported as a hard blocker rather than silently reappearing.

Model side: `jobsearch/copilot.py` runs a background worker that dispatches one job at a time to an external model CLI. The four job kinds are:

- `analysis`: a fit assessment written into the packet.
- `letter`: a cover-letter body.
- `questions`: answers to an employer's application questions.
- `facts_update`: a proposed edit to the profile file, staged for review and never applied automatically.

Every model answer is validated per job kind. Every kind must be plain ASCII, have no line starting with a greeting, contain no code tokens (backslash, `{`, `}` or triple backticks), and pass a boilerplate-token check. A letter must also be 240-320 words, every numeric token must appear verbatim in `facts.yaml`, no project name from `facts.yaml` may be used as a proper noun, and no parenthetical may contain two or more commas. An analysis must include four required section headings and stay within a word-count bound. Questions must include at least one answer block. A `facts_update` gets only the generic checks. Each job has a state machine with a blocker message and per-stage timings.

The reach gate: the AI actions refuse to spend a model call on a posting that is out of reach. A posting is out of reach when its title's seniority level is staff, principal, lead, manager, director or architect (executive titles such as VP or CISO count as director), or when its description asks for five or more years of experience. A senior title, or a description asking for 3-4 years, returns stretch. The gate never looks at location or liveness. When a posting is out of reach, the AI actions return HTTP 409 with the cause, and the caller can force the call.

## Quick start

Requires Python 3.13 or later.

```sh
python3 -m venv .venv
uv pip install --python .venv/bin/python pytest pyyaml
.venv/bin/python -m pytest -q
```

The suite should report 917 passed. It needs no network and no model. Without the PDF toolchain and node it reports 908 passed and 9 skipped. If uv is not installed, activate the venv and run `pip install pytest pyyaml` instead.

Then, from the repository root:

```sh
python -m jobsearch.cli sources          # show the company registry (offline)
python -m jobsearch.cli scan             # fetch the boards, score, show new ranked postings (network)
python -m jobsearch.cli list             # list stored postings
python -m jobsearch.cli stats            # counts by status
python -m jobsearch.cli dashboard        # write dashboard.html and print its path
python -m jobsearch.serve --host 127.0.0.1 --port 8770 --db ~/jobsearch/jobsearch.db
```

The server refuses to start (exit code 78) unless both `JOBSEARCH_SERVE_USER` and `JOBSEARCH_SERVE_PASSWORD` are set to non-empty values, because it may be bound to a LAN address. Every request then requires HTTP Basic authentication with those two values.

Other commands: `show`, `status`, `rescore`, `prep`, `letter`, `sources --check`. Run `python -m jobsearch.cli --help` for the list.

An editable install works with `pip install -e ".[dev]"`, and running from the source tree does not need it.

## Running with no model and no network

Everything except `scan` and the live probes runs offline.

**Stub model CLI.** `tools/stub-model-cli.sh` stands in for the model CLI. It uses the same calling convention as the real one:

```sh
<cmd> "<prompt>" --context <bundle dir> --max-chars <N>
```

It writes the answer to stdout and, per stage, a `# stage NAME start` line and a `# stage NAME SECS` line to stderr. It prints a canned answer for each job kind. Set `COPILOT_NEON_CMD` to its path so the copilot worker runs the queue, the validator, the artifact writer and the 409 gate with no model and no API key:

```sh
export COPILOT_NEON_CMD="$PWD/tools/stub-model-cli.sh"
```

**Fixture store.** `tools/make_fixture.py` builds a SQLite store from the captured board payloads in `samples/`. It runs the repository's own fetch and normalise code, with the single network call replaced by the captured bytes. The output is deterministic:

```sh
python tools/make_fixture.py            # writes fixture/jobsearch-fixture.db
JOBS_DB=fixture/jobsearch-fixture.db python -m jobsearch.cli stats
```

The fixture build reports 9 postings written.

**Environment variables.**

| Variable | Effect | Default |
|---|---|---|
| `JOBS_DB` | SQLite store path, used by the CLI and the server | `~/jobsearch/jobsearch.db` |
| `JOBS_RESUME_DIR` | Resume project the PDFs are built from | `~/resume` |
| `JOBS_TECTONIC` | LaTeX engine binary | `tectonic` on PATH |
| `COPILOT_NEON_CMD` | Model CLI the copilot worker calls | `neon` on PATH |
| `JOBSEARCH_SERVE_USER` | Username for HTTP Basic authentication on every request; the server will not start unless it is non-empty | None (required) |
| `JOBSEARCH_SERVE_PASSWORD` | Password for HTTP Basic authentication on every request; the server will not start unless it is non-empty | None (required) |

## How a posting is scored

Scoring is deterministic: the same posting and the same rules give the same score. It uses five inputs:

- title and skills text
- seniority
- location tier
- remote text
- blocked keywords

`rescore` applies the current rules to stored rows without touching the network. It reports how many postings are eligible and how many changed from a positive score to ineligible.

Seniority, and the years of experience a description asks for, feed the reach gate described above. Location and liveness do not.

## Application packets

`jobsearch prep <uid>` builds the resume PDF for one posting and stages a packet directory. `jobsearch letter` renders a cover-letter PDF from a plain-text body file. The copilot worker writes the packet's model artifacts (fit analysis, letter body, answers to application questions) only after they pass validation.

Packets and the dashboard are served by `python -m jobsearch.serve` over HTTP Basic auth. The server binds to `127.0.0.1` on port `8770` by default. `--db` selects the store, defaulting to `$JOBS_DB` or `~/jobsearch/jobsearch.db`.

Nothing in the pipeline submits an application, sends an email or posts to a site. Every output is a local file for a person to read.

## Repository layout

| Path | Purpose |
|---|---|
| `jobsearch/` | The package: eighteen modules (`cli`, `sources`, `registry`, `store`, `match`, `reach`, `staleness`, `checks`, `triage`, `analysis_findings`, `facts_ops`, `copilot`, `letter`, `dashboard`, `workspace`, `ui`, `serve`, `__init__`) |
| `tests/` | Sixteen test modules, including the offline dispatch tests, with `conftest.py` and a synthetic `fixture_facts.yaml` |
| `samples/` | Nine captured board payloads used by the fixture builder |
| `tools/` | `stub-model-cli.sh` (stand-in model CLI) and `make_fixture.py` (offline store builder) |
| `companies.yaml` | Starter board registry: eight entries, none probed yet |
| `pyproject.toml` | Package metadata, the PyYAML dependency and the `dev` extra |
| `.github/workflows/` | CI: runs the test suite on every push |
| `LICENSE` | MIT license |

## Requirements

- Python 3.13 or later.
- PyYAML 6 or later. It is the only runtime dependency. The rest is the standard library, including the HTTP server, the HTML dashboard renderer and the SQLite layer.
- For PDFs: `tectonic` (LaTeX) and `poppler-utils` (`pdftotext`, `pdfinfo`).
- For the test suite: pytest 8 or later.
- For the dashboard lint test: `node` on the PATH. Without it that one test skips.

The letter tests that compile a PDF skip themselves when `tectonic`, `pdftotext` or `pdfinfo` is missing, so a clone without the toolchain passes with those tests skipped rather than failed.

The starter registry in `companies.yaml` has eight entries, all marked `verified: false`, because none has been probed. `python -m jobsearch.cli sources --check` probes every entry live and names the dead ones. It needs network access.

## What is not in this repository

This repository is a sanitized mirror of a private deployment of the same code. The tests, the commands and the scoring rules are the same; the profile, the packets, the letters, the registry as used, and the store are not.

The repository holds code, tests and nine captured public board responses under `samples/`. The responses are verbatim, except for two values replaced with placeholders: the requisition creator's name in the two SmartRecruiters captures, and a base64-encoded caller IP in the RemoteOK capture. It does not hold anything that is one person's data:

- The profile files: `facts.yaml` (certifications, employment, education and projects) and `preferences.yaml`. The tests use a synthetic profile instead.
- The SQLite store. It is rebuilt by one `scan`, so it is never committed.
- Packet contents, including the fit analyses, the answers to application questions, and the staged profile edits.
- Cover letters, both the bodies and the rendered PDFs.

These stay out because they describe one person, and they are generated or written for one application at a time. Publishing them would publish that person's history and intentions.

## License

MIT. See [LICENSE](LICENSE).
