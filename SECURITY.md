# Security policy

This repository holds small tools that let you reach your own omp sessions from other places on your own machines. Each
plugin has its own policy and threat model next to its code:

- [`plugins/omp-session-control/SECURITY.md`](plugins/omp-session-control/SECURITY.md) and
  [`plugins/omp-session-control/docs/threat-model.md`](plugins/omp-session-control/docs/threat-model.md)
- [`plugins/omp-services/SECURITY.md`](plugins/omp-services/SECURITY.md) and
  [`plugins/omp-services/docs/threat-model.md`](plugins/omp-services/docs/threat-model.md)
- [`plugins/omp-private-relay/SECURITY.md`](plugins/omp-private-relay/SECURITY.md) and
  [`plugins/omp-private-relay/docs/threat-model.md`](plugins/omp-private-relay/docs/threat-model.md)
- [`plugins/omp-dashboard/SECURITY.md`](plugins/omp-dashboard/SECURITY.md) and
  [`plugins/omp-dashboard/docs/threat-model.md`](plugins/omp-dashboard/docs/threat-model.md)

## Reporting a vulnerability

Please report it privately, not in a public issue: use GitHub's **Report a vulnerability** button on this repository's
Security tab (private vulnerability reporting). Say which plugin and version, your omp version and operating system, and
how to reproduce it.

This is a one-person project. I read every report and answer as soon as I can, without a promised response time. Please
give me reasonable time to fix a problem before you publish it.

## Scope in one paragraph

These plugins run inside your omp sessions or next to them, on one trusted account on one machine. Reports about a process
that already runs as your user, or as root, are out of scope: it can already do more than any of these tools allow. Reports
about anyone else (another account, a web page, a repository you opened, the network) reaching your sessions are exactly
what I want to hear about.
