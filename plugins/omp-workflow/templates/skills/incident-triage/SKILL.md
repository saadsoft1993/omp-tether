---
name: incident-triage
description: Triage a live production problem — site down, errors spiking, slow, data looks wrong, security alert — by establishing impact, timeline, and the most likely cause fast, proposing the safest mitigation for approval, then handing off to a root-cause report. Use for 'site is down', 'prod is broken', 'users can't log in', 'getting 502', 'GuardDuty alert', 'urgent'.
---

# Incident triage

Goal: restore service safely first, explain later. **Read-only until the user approves a mitigation (release approval).**

## First 10 minutes
1. **Impact:** which product/URL, who is affected (all / one tenant / one role), since when. Check yourself:
   `curl -sS -o /dev/null -w '%{http_code} %{time_total}\n' <url>`; open it in the browser.
2. **What changed:** recent deployments, infrastructure or configuration changes, certificate expiry, DNS and billing.
3. **Health:** service status, running capacity, database health, CPU, connections, free storage, logs and alerts using authorized read-only access.
4. **Logs:** the error that started at the incident time (`debug-backend` sources).

## Classify + mitigate (propose, don't do)
| Cause | Safest mitigation |
|---|---|
| Bad deploy | roll back to previous SHA (the project deployment procedure) |
| Resource exhausted (disk, memory, connections) | free space / restart service / scale — say exactly what |
| Dependency down (RDS, Redis, external API) | fail over or degrade; communicate |
| Certificate/DNS | renew / fix record |
| Security | contain: revoke key, restrict network access — hand to a security engineer |
Give the user: impact, likely cause with evidence, 1–2 mitigation options with risk and rollback. Wait for yes.

## After
Verify recovery from outside (curl + browser + logs quiet 10 min). Use `work-message-writing` for the team/customer update if installed; otherwise draft a concise update stating impact, recovery status, remaining uncertainty and next update. Send only after approval. Write the `root-cause-report` in `docs/products/<product>/rca/`, create follow-up tracking tickets.
