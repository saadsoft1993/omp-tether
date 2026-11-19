# Model lifecycle (plan, verify, execute, review)

Session opens in plan mode on the `plan` model role. For any task that edits code or touches more than one file:

1. PLAN: read, investigate and write the plan yourself (main session). Include files, steps, risks and the check that proves each step.
2. VERIFY THE PLAN: before proposing it, spawn `plan-verifier` through `task` (runs on the `plan_verifier` role; give that role your strongest reasoning model). Fix valid findings. Propose only after that. The user approves the plan; never self-approve.
3. EXECUTE: delegate implementation through `task`. Well-specified, mechanical edits go to `sonic`; anything needing judgment or multi-file changes goes to the default `task` agent. They run on the `exec_fast` and `exec_strong` roles. Give each worker the exact files, the change, and the check to run. Never run the same step on a stronger tier until the cheaper one has failed its check.
4. REVIEW (mandatory for every code change, no exceptions): after code is written, spawn `code-reviewer` through `task` (on the `code_review` role; a different vendor from the author when you have one). Fix findings, rerun checks.
5. REPORT with evidence: commands run and their results (tests, lint, types), tests added or updated when behaviour changed, plus what was not verified.

Small-fix path (one file, obvious, no schema/auth/infra): skip step 2 only. Steps 3, 4 and 5 still apply, including a test for the changed behaviour and the independent review.

Q&A and read-only investigation need no delegation: answer directly in the main session.

# Deliverable choice for audits, analysis and drafts

Audits, analyses, reviews, investigations, status checks and message drafts produce findings or a draft, not an execution plan.

- Never end this work with an execution plan to approve, or with action items that assume you will execute them. Do not call `propose` for it.
- If the user already named the deliverable ("just a draft message", "report only"), produce exactly that and stop. Do not ask, plan or execute anything else.
- If the user did not name it, finish the read-only work, then ask once with `ask` (it works in plan mode). Offer these choices:
  - **Report only:** findings, no further action.
  - **Draft message:** to whom, and on which channel (chat, tracker or merge request).
  - **Plan fixes for approval:** this enters the normal lifecycle above.
  - **Execute selected items:** list the findings as checkboxes.

  Include an "Execution notes" text field in the same form.
- The user's execution notes are orders. When notes are given (in the form, at plan approval, or in chat), do exactly what they say and nothing more; they override your own plan. When no notes are given, follow your own plan for the option the user chose.
