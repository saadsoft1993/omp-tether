---
name: system-design-review
description: Review or produce the architecture of a product or feature — components, service boundaries, data ownership, multi-tenancy, auth flow, integrations, scaling, failure modes, cost — and output a design doc with a diagram and risk list. Use for 'review the architecture', 'how should we design', 'is this design right', 'internal vs external service', before an implementation plan.
---

# System design review

## Inputs
Approved PRD (or the question), `<project-docs>/{vision,decisions,status}.md`, the code
and its relevant callers, infra (Terraform, CI, compose files).

## Checklist — answer each with evidence (file:line, resource ID) or "unknown"
1. **Components:** services/apps, who owns which data, sync vs async (Celery/queues), external services.
2. **Boundaries:** what is in-house vs external SaaS; what must stay portable between servers.
3. **Tenancy & identity:** tenant isolation at DB/query/storage level; the identity source of truth; roles/permissions.
4. **Data:** schema ownership, migrations strategy, backups/restore tested?, PII.
5. **Interfaces:** API contracts (interface contract), versioning, error format, idempotency.
6. **Failure modes:** each dependency down → what the user sees; retries/timeouts; single points of failure.
7. **Operations:** deploy path, configuration and secrets, logs/metrics/alerts, runbook exists?
8. **Cost:** monthly estimate with numbers and source from the available cost data.
9. **Security:** authN/Z, network exposure, secrets — hand detail to a security engineer.
10. **Team fit:** the team’s existing skills; avoid stacks nobody can maintain.

## Output — `<project-docs>/architecture/<date>-<slug>.md`
Context · component diagram (mermaid) · data flow for the 2–3 main journeys · checklist results ·
risks ranked (likelihood × impact) with mitigation · decisions needing an ADR (`adr-writer`) · open questions.
Send to `plan-verifier` before the user sees it at plan approval.
