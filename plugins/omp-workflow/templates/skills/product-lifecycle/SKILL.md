---
name: product-lifecycle
description: Determine and advance the product development lifecycle stage of a product (Discovery → Requirements → Design → Build → QA → Release → Live/Iterate) and tell the user the next step and its exit criteria. Use for 'what is the next step', 'where are we in the lifecycle', 'start from scratch'.
---

# Product lifecycle

| Stage | Exit criteria (all must be true) | Owner |
|---|---|---|
| Discovery | problem, users, competitors/alternatives, constraints written in vision.md | PM |
| Requirements | PRDs for the release scope approved (requirements approval), glossary complete | PM |
| Design | architecture + ADRs + UX flows/wireframes, plan verified and approved (plan approval) | Architect (+FE for UX) |
| Build | all plan tasks merged to integration branch, unit/API tests green | FE/BE/DevOps |
| QA | e2e lifecycle pass, no open P0/P1, human QA sign-off | QA |
| Release | deployed, smoke-tested in prod, rollback documented, users informed | DevOps + Tech lead |
| Live/Iterate | metrics tracked, feedback → new PRDs | PM |

## Steps
1. Read status.md, prd/, plans/, git log. Determine the stage from evidence, not from the label.
2. List unmet exit criteria for the current stage → those are the next steps, in order.
3. Update `Stage:` in status.md if it changed and log the transition in decisions.md.
