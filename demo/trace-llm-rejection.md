# A real model interaction that failed (sanitized)

The demo in this directory runs every copilot action through `tools/stub-model-cli.sh`, a
stub that answers offline. This file records what a real interaction looks like when the
configured model CLI answers instead — specifically, a real `facts_update` job that the
pipeline itself rejected. The rejection was the model's, not the tool's: the model chose
an edit form the grammar does not allow against a file that did not exist yet, and the
apply check caught it. Everything below is taken from the deployment's job record and
session logs, sanitized: no personal identifiers, no device identifiers, no network
addresses, no document names. Redactions are marked `[redacted]`.

## The request

A plain-English instruction, delivered as one `facts_update` job:

```
I'm open to relocation anywhere in Canada and I require US visa sponsorship
to work in the US.
```

The model CLI is pointed at a document that contains the instruction, it translates the
instruction into a structured edit, and the pipeline applies it. The delivery details of
that first hop (which device, which document, how the text was injected) are
deliberately not described here; the interesting part starts when the model answers.

## The model's answer

The real model session returned, after a ~24 s model stage, this structured edit:

```json
{"ops": [{"op": "set", "path": "relocation.countries", "value": ["CA"]},
         {"op": "set", "path": "work_authorization.US.status",
          "value": "requires_sponsorship"}]}
```

Both paths are things the instruction clearly asked for. The problem is the `op`: both
target keys were absent — `preferences.yaml` did not exist at all — and `set` is defined
as "write a key that is already there". `upsert` is the operator for creating one.

## The rejection

The apply check rejected the edit and the job failed:

```
state:  failed
error:  check_failed: apply: path_absent: relocation.countries
```

Nothing was written: the job failed closed, leaving both `facts.yaml` and
`preferences.yaml` byte-identical to before. That is the design goal the failure
exercised — a bad model answer is caught by a structural check, not by a file that
silently loses or corrupts a value.

## The fix

The failure led to a deliberate rule change: a `set` on an absent key is rewritten into
an `upsert` at proposal time, so the same answer now lands as an edit to review
(`state: pending_review`, `ops` upgraded to `upsert`) instead of failing. The regression
test `tests/test_copilot.py::test_facts_propose_rewrites_a_set_on_an_absent_key_into_an_upsert`
pins the rewrite with this exact answer shape, and its docstring cites the observed
failure.

## Stage timings

The real interaction went through the full trigger → await path (the demo's stub
short-circuits most of these). Elapsed 235 s wall clock:

| stage    | secs  |
|----------|-------|
| context  | 3.11  |
| submit   | 3.55  |
| device   | 0.01  |
| model    | 24.15 |
| composer | 4.54  |
| focus    | 14.11 |
| inject   | 50.78 |
| send     | 3.30  |
| confirm  | 3.36  |
| trigger  | 100.39|
| await    | 126.24|
| record   | 0.20  |

The `inject` and `trigger` stages are where the session types the task into the device's
input; the first injection attempt can be rejected mid-typing and retried (here: attempt
1 of 3 failed, attempt 2 succeeded), which is why those stages dominate.

## What is sanitized

- The instruction is quoted verbatim because this repository already quotes the same
  facts in its own tests (`tests/test_copilot.py`); it contains no identifiers.
- The operator's name, the device address, the model CLI's product name, the delivery
  document name and the deployment's file paths are removed.
- The job id (`577ab6ba04d2`) and timestamps are kept: they are random/hex identifiers
  and anchor the trace in time (2026-11-24T23:26:06Z to 23:30:01Z).
