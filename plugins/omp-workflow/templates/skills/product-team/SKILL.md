---
name: product-team
description: "Run the product team pipeline for a requirement or bug: product-manager → requirements approval PRD approval → ui-designer + architect + plan-verifier → plan approval plan approval → senior-frontend/senior-backend/devops in parallel → qa-engineer + code-reviewer → release approval ship approval → status update. Use when the user says /product-team, 'build this feature end to end', 'let the team handle it', or gives a new requirement."
---

# /product-team — conductor playbook

You (the main session) are the conductor. Dispatch each stage using the available delegation tools and carry outputs between them. Follow the project’s rules and approval policy.

## 0. Intake
- Identify the product and repository. Ask if unclear.
- Read `<project-docs>/status.md` + `decisions.md`. Say in one line where the product stands.
- Size it: **small-fix path** (as defined by the applicable workflow) → skip to step 3 with one engineer, then code-reviewer and release approval. No PRD.
  **bug path** (something broken, not new scope; an obvious ≤1-file bug takes the small-fix path instead) →
  `qa-engineer` `bug-validate` (skip if already validated) → REAL items to frontend / backend engineers by layer.
  When the workflow requires a plan, the engineer presents one → **plan approval** before coding.
  Fix + regression test → `code-reviewer` → draft MR (the project’s review workflow) → step 4. Live outage → an infrastructure engineer `incident-triage` first.
  **normal** → full pipeline.

## 1. Requirements — `product-manager`
Dispatch with the user's words verbatim + product key. It returns PRD path + questions.
Relay questions to the user (max 3 at a time, with recommended defaults). Loop until PRD is complete.
**requirements approval:** show the PRD summary (scope in/out, acceptance criteria count, open questions). Wait for "approved".

## 2. Design — `ui-designer` + `architect`, then `plan-verifier`
Dispatch in ONE message when the feature has UI: `ui-designer` → flow + HTML mockups with all states and copy
(`<project-docs>/design/`); `architect` → design review + API contract + ADRs + phased plan (`<project-docs>/plans/<date>-<slug>.md`).
Then reconcile: mockup fields ↔ API contract.
plan-verifier (configured plan-verification role) → per-step HOLDS/RISKY/BROKEN and an overall PROCEED / PROCEED WITH CHANGES / DO NOT PROCEED.
- PROCEED WITH CHANGES → architect applies the required changes; re-verify only the changed parts; then plan approval.
- DO NOT PROCEED → back to architect once for a rework and a full re-verify; still DO NOT PROCEED → escalate to the user.
**plan approval:** show plan phases, mockup screenshots (UI work), cost/risk, verifier verdict. Wait for "approved".
Optional: `product-manager` drafts tracking tickets per plan task; show the list, create only after the user approves it (the project tracker).

## 3. Build — engineers in parallel
Dispatch independent tasks in ONE message: `senior-backend` (API contract first), `senior-frontend`,
an infrastructure engineer (infra), a security engineer (if auth/access/secrets/network touched).
Use project branch conventions and isolated worktrees when concurrent writers touch the same repo.

## 4. Verify — never the author
`code-reviewer` on each branch diff, then a **draft** MR via the project’s review workflow (no gate); `qa-engineer` runs acceptance criteria + regression in a browser.
CHANGES REQUIRED / failures → back to the engineer, then re-verify. Max 3 loops, then escalate to the user.

## 5. Ship — release approval
Show: what changed, test/QA evidence, reviewer verdict, deploy steps + rollback. Wait for explicit yes —
confirm separately which of marking the change ready, merging and deploying the user authorizes.
Then merge/deploy (the project deployment procedure), and verify in the target environment.

## 6. Close
`product-manager`: prepend decisions (newest first), rewrite `status.md` (≤40 lines), list follow-ups.
Final message to the user: outcome, links (MR, the project tracker, page), VERIFIED/UNVERIFIED.
