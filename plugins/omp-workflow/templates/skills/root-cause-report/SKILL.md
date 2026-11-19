---
name: root-cause-report
description: The shared write-up format for a bug investigation or incident — symptom, reproduction, root cause with evidence, fix, regression test, prevention. Use at the end of any debugging or incident triage, and when the user asks 'why did this happen' or 'write up the RCA'.
---

# Root-cause report

Save as `<project-docs>/rca/<date>-<slug>.md` (incidents and anything that hit users), otherwise put it in the MR description.

```
# <one-line symptom>
Product / env / first seen / reported by
## Symptom        what the user saw, how many affected, since when (evidence: screenshot/log/metric)
## Reproduction   minimal steps or command that triggers it every time (or "intermittent: N of M")
## Root cause     the actual defect at file:line (or config/resource ID) and WHY it produces the symptom
## Evidence       the log line / query result / bisect commit / failing test that proves it — not a guess
## Fix            what changed (MR link), why this is the minimal correct fix, alternatives rejected
## Regression     the test that now fails without the fix and passes with it
## Prevention     check/alert/lint/test that would have caught it earlier; tracking ticket if follow-up work
VERIFIED / UNVERIFIED
```

Rules: "root cause" must explain the mechanism, not restate the symptom. If you only have a hypothesis, label it
HYPOTHESIS and list the experiment that would confirm it. Use real names for who reported/fixed.
