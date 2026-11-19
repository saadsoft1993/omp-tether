---
name: debug-backend
description: Debug a backend bug in a service (Django/DRF, Celery, FastAPI, PostgreSQL) — gather application and infrastructure logs, reproduce with a test, query the DB read-only, bisect, find the root cause, minimal fix. Use for '500 error', 'API returns wrong data', 'job not running', 'slow endpoint', 'traceback', 'works on staging not prod'.
---

# Backend debugging

**Read-only on any shared/prod system.** No restarts, data fixes, migrations or config changes without release approval.

## Production access
Use only authorized read-only logs, metrics and queries. Obtain explicit approval before any production write, restart, interactive access or potentially disruptive query.

## 1. Evidence first
Inspect logs and access methods documented by the project, using authorized read-only access.
Capture: exact traceback, request ID/time, user/tenant, commit SHA deployed.

## 2. Reproduce
Write a failing test first using the project’s test framework.
If it only fails with real data, find the shape of the data with a **read-only** query
(use a read-only role; never print secrets or PII rows — counts/ids only).

## 3. Isolate
- Traceback → top in-repo frame → read it with its callers .
- Regression? `git bisect` between last-good and first-bad SHAs (deploy history / pipelines).
- Common causes: tenant filter missing in a queryset, N+1 / missing index (`EXPLAIN ANALYZE` on a copy),
  Celery task not registered / wrong queue / retry storm, timezone mismatches, missing configuration,
  migration not applied on that environment, incorrect ports.

## 4. Fix + prove

Follow the applicable planning and approval rules; for a fix plan include files, approach, risks and rollback.
Minimal change at the cause; the new test fails before, passes after; full suite green; `django-verification` for Django.
Write up with `root-cause-report`; follow the project’s review workflow; QA runs `regression-run`.
