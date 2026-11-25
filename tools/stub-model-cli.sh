#!/bin/sh
# Stand-in for the model CLI, so the whole Copilot pipeline runs with no model, no
# network and no API key. Point COPILOT_NEON_CMD at this file and stage a job.
#
# Calling convention, identical to the real CLI:
#   <cmd> "<prompt>" --context <bundle dir> --max-chars <N>
# The answer goes to stdout. Progress goes to stderr, one line per stage, in the form
# "# stage NAME SECS" — that is how the job record learns what the model is doing.
#
# The answers are canned and generic. They exist to exercise the queue, the validator,
# the artifact writer and the UI, not to be good writing.
set -eu

prompt="${1:-}"
shift || true
bundle=""
while [ "$#" -gt 0 ]; do
    case "$1" in
        --context) bundle="${2:-}"; shift 2 ;;
        --max-chars) shift 2 ;;
        *) shift ;;
    esac
done

echo "# stage model start" >&2
echo "# stage model 0.4s" >&2

case "$prompt" in
    *"cover letter body"*)
        cat <<'LETTER'
I build small, boring, well-tested tools that make a security team's day shorter, and that is the work this role describes in its first paragraph. The problem the team names is a detection backlog that grows faster than the writing of detections, so I would start where that backlog hurts most.

At my most recent role I owned the content pipeline end to end. I turned the review backlog into a Python job that validated every rule against the schema before a human ever saw it, and the review meetings went from arguing about syntax to arguing about coverage. The same pattern applies here: make the machine catch the mechanical mistakes so the humans spend their attention on the judgement calls, which is what the posting says it wants from this hire.

On the platform side I have shipped containerised services with their own test suites and their own health checks, because a detection that silently stops firing is worse than one that never existed. I write my own tests, I read the logs I ship, and I would rather delete a noisy signal than let it train the team to ignore the queue.

What attracts me to this team specifically is the pairing of engineering and operational judgement in one role. I have worked both sides of that line, writing the content and carrying the pager for it, and the second half is what makes the first half honest. I would like to bring that to a team that already believes it.
LETTER
        ;;
    *"Assess the applicant's fit"*)
        cat <<'ANALYSIS'
VERDICT
no chance: the posting asks for production ownership of a regulated platform plus partner-facing security review, and facts.yaml shows one short security role and no regulated-environment work.

FIT
In projects (Aurora Ledger Reconciliation Pipeline, Beacon Agent Operations Layer), the applicant builds Python tooling with automated test suites, including pytest coverage that a reviewer can run locally. This aligns with the posting's emphasis on shipping maintained code rather than one-off scripts.

In employment (Example Corp, Security Content Engineer), the applicant brings detection content work and operational triage, which is the day-to-day the posting describes in its responsibilities section.

GAPS
First, production tenure: the role requires several years of operating production systems, while facts.yaml proves one short security role and nothing longer.

Second, regulated environments: facts.yaml does not prove work under an audit or compliance regime, which the posting names as a hard requirement.

Third, partner-facing review: facts.yaml shows no experience reviewing third-party integrations or answering security questionnaires for external teams.

TO_BE_PERFECT
Over three to four months, publish the pipeline as a maintained repository with documentation a stranger can follow, and take on one review-facing responsibility where the work is judged by someone outside the team.
ANALYSIS
        ;;
    *"application questions"*)
        cat <<'QUESTIONS'
**Why do you want to work here?**

The posting describes a team that owns both the detection content and the platform it runs on. I have worked on both sides of that line and I want to keep doing both, rather than handing my work over a wall.

**Tell us about a project you are proud of.**

I built a small pipeline that validated configuration against a schema before review. It removed the mechanical arguments from review meetings and left the judgement calls, which is the part of the job worth spending human attention on.

**What is your experience with our technology stack?**

I work in Python daily, with SQLite and pytest for the storage and test layers, and I have shipped containerised services with their own health checks. I pick up a new stack by reading its tests.
QUESTIONS
        ;;
    *)
        echo "stub-model-cli.sh: unrecognised prompt, see tools/stub-model-cli.sh" >&2
        exit 2
        ;;
esac
