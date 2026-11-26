# Offline demo walkthrough

> Back to the main [README](../README.md). Prerequisite: Python 3.13 or later. Use `uv` if it is installed; if not, use `pip` (see [Quick start](../README.md#quick-start)). Recorded against commit `7b75865`.

This repository can be exercised end to end with no network, no model and no personal data. `tools/make_fixture.py` builds a synthetic SQLite store from the captured board payloads in `samples/`, and everything below runs against it. Every command was run once in a fresh clone and its output is recorded verbatim.

## 1. Run the test suite

```sh
python3 -m venv .venv
uv pip install --python .venv/bin/python pytest pyyaml
.venv/bin/python -m pytest -q
```

Recorded output (summary line; the per-test dots are omitted):

```
917 passed in 115.15s (0:01:55)
```

On a clone without the PDF toolchain or node, the same command reports 908 passed and 9 skipped. The main README's [Quick start](../README.md#quick-start) and [Requirements](../README.md#requirements) sections have the details.

Steps 2 to 6 use bare `python`. Activate the venv first (`source .venv/bin/activate`), or the commands run under the system interpreter.

## 2. Build the fixture store

```sh
python tools/make_fixture.py            # writes fixture/jobsearch-fixture.db
```

Recorded output:

```
greenhouse.json                 1 parsed    1 new    0 refreshed
lever.json                      1 parsed    1 new    0 refreshed
ashby.json                      1 parsed    1 new    0 refreshed
workable.json                   1 parsed    1 new    0 refreshed
smartrecruiters.json            2 parsed    2 new    0 refreshed
remotive.json                   1 parsed    1 new    0 refreshed
remoteok.json                   1 parsed    1 new    0 refreshed
arbeitnow.json                  1 parsed    1 new    0 refreshed
<clone>/fixture/jobsearch-fixture.db    9 postings written
```

(The tool prints the absolute path of the clone it ran in; shown elided here.)

The build is deterministic: two runs produce byte-identical files.

```
17869867504f742066d8d50f6419267f7bb94a5b25c4a3a7c1a21c2f88061d13  fixture/jobsearch-fixture.db
17869867504f742066d8d50f6419267f7bb94a5b25c4a3a7c1a21c2f88061d13  fixture/jobsearch-fixture.db
```

## 3. Counts by status

```sh
JOBS_DB=fixture/jobsearch-fixture.db python -m jobsearch.cli stats
```

Recorded output:

```
total:          9
new in last 24h: 9
by status:
  new          9
  shortlisted  0
  prepped      0
  applied      0
  interview    0
  offer        0
  rejected     0
  closed       0
  skipped      0
```

## 4. List the stored postings

```sh
JOBS_DB=fixture/jobsearch-fixture.db python -m jobsearch.cli list
```

Recorded output:

```
score | status | company       | title                                                                | location                                                                                          | uid
------+--------+---------------+----------------------------------------------------------------------+---------------------------------------------------------------------------------------------------+------------------------------------------------------------------------------------------
0.0   | new    | AMALOCK       | Werkstudent Marketplace Manager (m/w/d) – Amazon, TikTok Shop & Temu | Berlin                                                                                            | arbeitnow:arbeitnow:werkstudent-marketplace-manager-amazon-tiktok-shop-temu-berlin-179896
0.0   | new    | Sample Corp   | Senior Commercial Account Executive (San Francisco/Bay Area)         | United States                                                                                     | ashby:sample:febe5d7f-25c9-4d77-a184-0f99e748fe7f
0.0   | new    | Sample Corp   | Account Executive, FedCiv                                            | Hybrid                                                                                            | greenhouse:sample:7695702
0.0   | new    | Sample Corp   | [Wattpad] Delivery Manager                                           | Toronto, Ontario                                                                                  | lever:sample:4b103011-eec6-4944-bdfd-1461a51a6932
0.0   | new    | Reddit        | Product Manager, Developer Ecosystem                                 | US Only                                                                                           | remoteok:remoteok:1137977
0.0   | new    | TELUS Digital | Content Reviewer - United States                                     | USA                                                                                               | remotive:remotive:2091177
0.0   | new    | Sample Corp   | R&D Scientist - LC/MS                                                | Mississauga, ON, Canada                                                                           | smartrecruiters:sample:744000163478879
0.0   | new    | Sample Corp   | Environmental Planner (Contract)                                     | Kamloops, BC, Canada                                                                              | smartrecruiters:sample:744000163545151
0.0   | new    | Sample Corp   | Customer Success Manager                                             | Orlando, Florida, United States / Atlanta, Georgia, United States / Tampa, Florida, United States | workable:sample:908332A8B3
```

The `show <uid>` command is omitted from this walkthrough; see [What the demo does not do](#what-the-demo-does-not-do).

## 5. Render the dashboard

```sh
JOBS_DB=fixture/jobsearch-fixture.db python -m jobsearch.cli dashboard
```

Recorded output:

```
fixture/dashboard.html
```

The file is self-contained HTML, written inside `fixture/`, and can be opened directly in a browser.

## 6. Serve and verify auth

The server requires `JOBSEARCH_SERVE_USER` and `JOBSEARCH_SERVE_PASSWORD` and answers HTTP Basic auth on every request. It exits 78 if either variable is unset. The values below are throwaway demo credentials; bind to `127.0.0.1`. `COPILOT_NEON_CMD` points at `tools/stub-model-cli.sh` so the copilot actions in the served UI run offline, with no model and no API key.

```sh
export COPILOT_NEON_CMD="$PWD/tools/stub-model-cli.sh"
JOBSEARCH_SERVE_USER=demo JOBSEARCH_SERVE_PASSWORD=demo-passphrase \
  python -m jobsearch.serve --host 127.0.0.1 --port 8790 --db fixture/jobsearch-fixture.db
```

Server log, recorded:

```
2026-11-25 22:23:40,692 INFO serving on http://127.0.0.1:8790 (auth: Basic, user from JOBSEARCH_SERVE_USER)
```

Without credentials the request is rejected:

```sh
curl -sS -i http://127.0.0.1:8790/ | head -6
```

Recorded output:

```
HTTP/1.0 401 Unauthorized
Server: jobsearch-serve
Date: Thu, 26 Nov 2026 03:23:46 GMT
Content-Type: text/plain; charset=utf-8
Content-Length: 13
X-Content-Type-Options: nosniff
```

With the demo credentials the dashboard is served:

```sh
curl -sS -u demo:demo-passphrase http://127.0.0.1:8790/ | head -10
```

Recorded output (first lines of the dashboard HTML):

```
<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Job board — triage</title><style>
*,*::before,*::after{box-sizing:border-box}
:root{color-scheme:light;--bg:#f6f5f1;--bg2:#efede6;--bg3:#fbfaf7;--card:#ffffff;--ink:#1b1b1a;
--ink2:#3d3c38;--muted:#5f5e59;--line:#dedcd3;--line2:#cfcdc4;--line3:#e6e4dc;
--acc:#0f5f52;--onacc:#ffffff;--acc2:#bfe0d8;--s2t:#0b3d35;--warnb:#fde9d2;
--warnt:#7a3b00;--warnl:#f0c79a;--warnbar:#b4540a;
--r:8px;--rl:12px;
--font:Inter,system-ui,-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
--mono:"JetBrains Mono",ui-monospace,SFMono-Regular,"DejaVu Sans Mono",Menlo,monospace}
```

Stop the server with Ctrl-C. It writes nothing; the only files the demo created are `.venv/` and everything under `fixture/`.

## What the demo does not do

- Two commands are deliberately absent: `python -m jobsearch.cli scan` and `python -m jobsearch.cli sources --check` both need network access. Nothing here fetches anything. The demo never runs either command.
- `show <uid>` also works offline and prints the full stored posting, including the posting's real source URL from the captured payload; its output is omitted here for that reason.
- It writes nothing outside `fixture/` (and `.venv/` if you created it).
- It prepares nothing, applies to nothing and sends nothing. That is the pipeline's own rule, not just the demo's: every output is a local file for a person to read.
- The fixture holds nine synthetic postings built from the captured public payloads in `samples/`. No profile, packet, letter or store from the private deployment is in this repository; see [What is not in this repository](../README.md#what-is-not-in-this-repository) in the main README.
