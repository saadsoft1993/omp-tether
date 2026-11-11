# Security policy: omp-services

## Reporting a vulnerability

Please report it privately, not in a public issue: use GitHub's **Report a vulnerability** button on the repository's
Security tab (private vulnerability reporting). Include the omp version, your operating system, the plugin version, and
the smallest steps that reproduce the problem.

This is a one-person project. I will read every report and answer as soon as I can; I cannot promise a fixed response time.
Please give me reasonable time to fix a problem before you publish it, and tell me if you intend to.

## What counts

In scope: anything that lets a process or user that is **not you** make omp-services run a command, load or remove a unit,
write or delete a file, or read a secret; a way to make it touch a unit or a directory that is not its own (label
allowlist, purge marker, protected flag); a path-handling flaw (symbolic links, races, permissions) in how it creates,
replaces or removes files; the heartbeat URL appearing in a log, a status file, an error message, a process listing or the
configuration; a heartbeat request that follows a redirect, skips certificate verification, or sends a body; a health check
that runs unbounded or reports a URL, a path or command output; anything that makes the extension crash or hang an omp
session.

Out of scope, by design (see [`docs/threat-model.md`](docs/threat-model.md)): a process that already runs as your user or
as root. It can already do everything this tool can. A drop-in file you wrote yourself that runs a command you chose.

## Supported versions

The latest release only, while the version is below 1.0.

## What is not claimed

It is not certified and has had no external penetration test. The threat model lists what is defended against and what is accepted. The Tailscale
Serve route check was written against the documented JSON shape of `tailscale serve status --json` and has not been run
against a real tailnet.
