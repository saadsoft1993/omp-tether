---
name: test-plan
description: Write a test plan for a feature or release from the PRD acceptance criteria — scenarios per role, positive/negative/edge cases, validation and error/success messages, data setup, environments, automation vs manual split, and exit criteria. Use for 'test plan', 'what should QA test', 'test cases for', before e2e-lifecycle or a human QA handoff.
---

# Test plan

Output: `docs/products/<product>/qa/<date>-<feature>-test-plan.md`.

## Build it
1. Source: PRD acceptance criteria (each AC gets ≥1 scenario — keep the AC id), roles from the PRD/glossary, decisions.md.
2. For each scenario: ID · role · preconditions/data · steps · expected result (including the exact message text) · type
   (positive / negative / edge / permission / regression) · priority P0–P2 · automated? (unit / API / browser) .
3. Always include: permission matrix (each role × each action, including "must be denied"), tenant isolation
   (user of org A cannot see org B), empty/loading/error states, validation on every field, double-submit,
   back-button/refresh, mobile width, and data that already exists in production shape.
4. Environments: which URL/server, test accounts (point to where credentials live — never paste passwords).
5. Exit criteria: all P0 pass, no open P0/P1 bugs, regression suite green, evidence stored.

## Check
Count ACs and scenarios; every AC id appears at least once (list any uncovered). State both counts.
Then run with `e2e-lifecycle` (browser) and `regression-run` after fixes; human QA handoff uses the same IDs.
