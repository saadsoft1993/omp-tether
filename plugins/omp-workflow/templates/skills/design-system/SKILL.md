---
name: design-system
description: Document, audit or extend the design system of a product — tokens (colour, type, spacing, radius), components (the existing component library and tokens), patterns for forms, tables, empty/error states, and copy tone — and check screens for inconsistencies. Use for 'design system', 'make the UI consistent', 'UI audit', 'which component should I use', 'style guide'.
---

# Design system

One file per product: `docs/products/<product>/design-system.md` (create it from the code if missing — never invent tokens).

## Extract from the code
- Inspect the project’s styles, CSS variables, component library and existing form/table/dialog wrappers.
- Record tokens and the canonical component for each job; consult existing design documentation.

## Document
Tokens table · components (name, when to use, props/variants, do/don't) · patterns: forms (labels above, inline validation,
required marker, submit states), tables (sort/filter/pagination/empty), feedback (toast vs inline vs dialog), dates/money
(the product’s locale and timezone), copy tone (short, verb-first buttons, no blame in errors).

## Audit a screen or app
Browser screenshots → list deviations (wrong token, duplicate component, inconsistent spacing, missing state, contrast fail)
with file:line where possible → count them twice → table with severity and fix owner (`senior-frontend`).
