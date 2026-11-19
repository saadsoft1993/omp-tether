---
name: debug-frontend
description: Debug a frontend bug in a web app (React/Next.js/TypeScript) — reproduce in a real browser, read console and network, isolate the component/state/API cause, minimal fix, regression test. Use for 'page is broken', 'button does nothing', 'blank screen', 'wrong data shown', 'UI bug', 'works locally not on staging'.
---

# Frontend debugging

## 1. Reproduce (no repro → no fix)
- Open the exact URL/env/role in an available browser tool. Record steps; screenshot before/after.
- `read_console_messages` (filter with a pattern) and `read_network_requests` — note failing requests:
  status, URL, response body excerpt (never paste tokens/cookies).

## 2. Classify — where does it break?
| Signal | Likely layer |
|---|---|
| 4xx/5xx or wrong JSON in network | backend → hand evidence to a backend engineer |
| Request correct, UI wrong | state/rendering (props, memo, stale cache, react-query key) |
| JS error in console | code path at the stack's top app frame (source maps) |
| Only on staging/prod | env vars/build config, CDN/CloudFront cache, base URL, CORS, mixed content |
| Only some roles | permission checks / route guards / feature flags |

## 3. Isolate
Find the component and its callers. Narrow with a breakpoint/log or by
toggling inputs; `git log -p -- <file>` / `git bisect` between last-good and first-bad deploy SHAs.

## 4. Fix + prove

Follow the applicable planning and approval rules; for a fix plan include files, approach, risks and rollback.
Smallest change at the root cause (not a guard around the symptom). Add a test that fails before and passes after
(component test, or a browser scenario if UI-only). Re-run the repro in the browser; screenshot. Lint/type-check.

## 5. Write up
`root-cause-report` format → `code-reviewer` → draft MR via the project’s review workflow → `regression-run` by QA.
