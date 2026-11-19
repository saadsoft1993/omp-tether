---
name: bug-validate
description: Validate a list of reported bugs for a product — decide which are real, not bugs, duplicates or not reproducible, with evidence and the responsible layer (frontend/backend/infra). Use for 'are these bugs genuine', 'verify the bugs we found', 'which reported issues are valid', before any fix.
---

# Bug validation

## Steps
1. Collect the list. **Count it** and state the count; re-count from the source before reporting (exact, not approximate).
2. For each: reproduce on the stated environment (browser for UI, curl/API for backend, logs for jobs).
3. Verdict per item: REAL (P0–P3) · NOT A BUG (expected per PRD/decision) · CAN'T REPRODUCE · DUPLICATE of #n.
4. For REAL: steps, expected vs actual, evidence (screenshot/response/log line), layer FE/BE/infra, suspected file:line.
5. Summary table + counts per verdict (they must add up to the total).
6. Offer: group REAL items into tickets (the project tracker) and route fixes to the right engineer. Do not fix here.
