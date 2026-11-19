---
name: prd-writer
description: Write or update a product requirements document (PRD) for a product, interviewing the user for missing details. Use for 'write requirements', 'PRD', 'spec this feature', 'what exactly should X do'.
---

# PRD writer

Output: `docs/products/<product>/prd/<YYYY-MM-DD>-<slug>.md`

## Interview
Ask only what blocks a testable acceptance criterion. Max 3 questions per round, each with a
recommended default ("If no preference: X because Y"). Use the glossary's terms.

## Template
```
# <Feature> — PRD
Product: <p> · Stage: <lifecycle stage> · Status: Draft|Approved (requirements approval <date>) · Owner: <user>
## Problem            — who hurts, how, evidence
## Users & roles      — each role and what they can do
## Scope              — In: … / Out (explicitly): …
## User stories
- US-1 As a <role> I want <x> so that <y>
  - AC-1.1 Given … When … Then … (incl. error + success messages)
## Data & rules       — entities, validations, permissions, tenancy
## Non-functional     — performance, security, audit, i18n
## Success metrics
## Open questions     — owner + needed-by
## Decisions          — link to decisions.md entries
```
After approval set `Status: Approved (requirements approval <date>)` and log it with `decision-log`.
