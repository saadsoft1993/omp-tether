---
name: decision-log
description: Record a product decision in <project-docs>/decisions.md and keep status.md current. Use whenever the user decides, agrees, rejects or locks something ('we will go with', 'locked', 'agreed', 'no for now'), and when asked 'what did we decide' or 'where do we stand'.
---

# Decision log

## Record
Prepend under the header in `<project-docs>/decisions.md`:
`- YYYY-MM-DD — <decision, one line> — <why> (source: session <date> / <file>)`
Superseding an older decision: add `(supersedes YYYY-MM-DD)` and leave the old line untouched.

## Answer "what did we decide / where do we stand"
Read decisions.md + status.md and answer from them, quoting dates. If they look stale (>7 days or
contradicted by git log), say so and offer to refresh.

## status.md format (≤40 lines, always rewrite whole file)
```
# <Product> — Status
Stage: <stage> — <one-line summary>
Updated: YYYY-MM-DD
## Done
## In progress
## Next
## Blockers / open questions
```
