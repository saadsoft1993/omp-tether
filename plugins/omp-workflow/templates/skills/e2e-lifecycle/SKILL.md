---
name: e2e-lifecycle
description: Run an end-to-end lifecycle test of a product in a real browser — every role, main workflows, form validation, error and success messages — and produce a pass/fail report; also prepare manual QA handoff instructions for the human QA team. Use for 'run complete testing', 'test the whole platform', 'give QA team access and instructions'.
---

# E2E lifecycle test

1. Scenarios from PRD acceptance criteria + roles (glossary). Cover: create → read → update → delete,
   permissions per role, tenant isolation, invalid input, empty states, success and error messages.
2. Run in a real browser (an available browser tool). Fresh test data; note IDs created.
3. Each scenario: PASS / FAIL with screenshot or response as evidence. FAIL → bug-validate format.
4. Report: totals per area, failing list ranked by severity, coverage gaps (what was not tested and why).
5. Clean up test data unless the user wants it kept.

## Human QA handoff (when asked)
Environment URL, which roles/test accounts exist (point to where credentials are stored, never paste them),
feature list, step-by-step workflows, what to check, how to report bugs. Use `work-message-writing` if installed; otherwise draft a concise, actionable handoff with those details. Send only after approval.
