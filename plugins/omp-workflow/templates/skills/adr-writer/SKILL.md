---
name: adr-writer
description: Write an architecture decision record (ADR) for a product. Use when choosing a framework, database, hosting, service boundary, license, integration, or any decision that is expensive to reverse.
---

# ADR writer

Path: `<project-docs>/adr/NNNN-<slug>.md` (NNNN = next number).
```
# ADR-NNNN: <title>
Date · Status: Proposed|Accepted|Superseded by ADR-x
## Context        — forces, constraints (team skills, budget, hosting, portability, compliance)
## Options
| Option | Pros | Cons | Cost/month | Ops effort | Reversibility |
## Decision       — and why this beats the runner-up
## Consequences   — what gets easier/harder, follow-up work, risks
## Sources        — docs, pricing pages, benchmarks (with dates)
```
Licenses: state the license, whether it allows commercial SaaS use, copyleft obligations.
When Accepted, add a line to decisions.md via `decision-log`.
