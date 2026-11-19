---
name: ui-mockup
description: Design a screen or flow for a product before it is built — user flow, wireframe, then a clickable HTML mockup in the product's design system with every state (empty, loading, error, success, validation, permission-denied) and the exact UI copy. Use for 'design this screen', 'mockup', 'wireframe', 'how should this page look', 'UX for', after the PRD (requirements approval) and before frontend build.
---

# UI mockup

## 1. Understand
PRD user stories + ACs, roles, `<project-docs>/glossary.md`, the product's `design-system` notes, and 2–3 existing
screens of the same app (screenshot them in the browser) so the new screen looks like it belongs.

## 2. Flow first
List the journey as steps (entry → actions → outcome) per role, including failure paths. Mermaid flowchart in the doc.

## 3. Mockup
- One self-contained HTML file per screen: `<project-docs>/design/<date>-<feature>/<screen>.html`
  (use the product’s existing styles and tokens).
- Show every state side by side or with a state switcher: default, empty, loading, error, success, validation errors per field,
  permission-denied, long text/large numbers, mobile width (375px).
- Real UI copy: labels, help text, button verbs, validation and success/error messages (the user reviews these closely).
- Accessibility: labels on inputs, focus order, contrast ≥ 4.5:1, not colour-only status, keyboard reachable.

## 4. Check + hand off
Open each file in the browser, screenshot desktop + mobile, attach to the doc. Count ACs covered by the screens (list gaps).
Show the user for approval (part of plan approval for UI work). Handoff note to `senior-frontend`: components to reuse/create,
states, copy table, API fields needed (align with interface contract).
