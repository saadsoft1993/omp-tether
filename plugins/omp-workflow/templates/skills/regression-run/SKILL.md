---
name: regression-run
description: Re-test a product after a fix or before a release — re-run the original bug repro, the neighbouring scenarios and the product's core flows, compare with the last run, and give a GO/NO-GO with evidence. Use for 'regression test', 'did the fix break anything', 'retest', 'ready to release', after any merge to staging.
---

# Regression run

## Scope (write it down first)
1. The fixed items: their original repro steps (from `bug-validate` / the project tracker).
2. Neighbours: same screen, same API, same model, same permission — find them by inspecting callers and affected flows.
3. Core flows: the P0 scenarios of the product's test plan (`docs/products/<product>/qa/`) or the latest UAT doc (`docs/uat/`).

## Run
- Automated first: the repo's test suite / CI pipeline result for the exact commit (the project CI status).
- Then browser: an available browser tool on the stated environment; screenshot each P0 result.
- Record per scenario: PASS / FAIL / BLOCKED, evidence (screenshot path, response, log line), commit SHA tested.

## Report
Table + counts (must add up to the scope count — count twice). Compare with the previous run: newly failing = regression.
Verdict: **GO** (all P0 pass, no new failures) or **NO-GO** (list blockers with owner). A NO-GO goes back to the engineer;
you never fix product code. GO is input to release approval, not the approval itself.
