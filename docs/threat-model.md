# Threat model: the omp-tether repository

Each plugin keeps its own threat model beside its code, because the risks differ. This page states what they share.

| Plugin | Threat model |
|---|---|
| `omp-session-control` | [`plugins/omp-session-control/docs/threat-model.md`](../plugins/omp-session-control/docs/threat-model.md) |
| `omp-services` | [`plugins/omp-services/docs/threat-model.md`](../plugins/omp-services/docs/threat-model.md) |
| `omp-private-relay` | [`plugins/omp-private-relay/docs/threat-model.md`](../plugins/omp-private-relay/docs/threat-model.md) |
| `omp-dashboard` | [`plugins/omp-dashboard/docs/threat-model.md`](../plugins/omp-dashboard/docs/threat-model.md) |
| `omp-memory` | [`plugins/omp-memory/docs/threat-model.md`](../plugins/omp-memory/docs/threat-model.md) |

## What the plugins share

- **One trusted account.** They are built for one person's machine. The user that runs omp, and root, are trusted; any
  other account, any web page and any network peer are not.
- **A process running as you is trusted.** It can already read your omp sessions and credentials, signal omp and run
  anything you can. The plugins do not try to stop it, and say so wherever it matters.
- **Inert until configured.** Installing a plugin must not create files outside omp's own plugin folders, open sockets or
  contact anything. A plugin turns itself on only after a file you create (or a command you run) says so.
- **Loopback and private files only.** No plugin here listens on a network interface (`omp-private-relay` and `omp-dashboard` bind loopback addresses and nothing else, `omp-memory` listens on nothing; reaching it from another device is a tunnel you set up). Local state lives in directories
  with mode `0700` and files with mode `0600`, and refuses to follow a symlink into them.
- **Logs hold categories, no telemetry.** Logs record kinds of events, times, process ids, session ids and error categories. Request content and names are not written deliberately, and exception text is written only when a plugin's debug setting is switched on (and then it can be sensitive). Nothing is sent anywhere.
- **Extensions run inside every session without a sandbox.** An extension in this repository is one file with no
  dependencies and no install scripts (omp runs none for marketplace installs), so that it can be read in one sitting. A
  crash inside it is a crash of your session, so callbacks are wrapped and the source is audited by a test.
- **How you get the code.** Install from this repository's catalog, optionally pinned to a commit. Updates are not
  announced by omp (its default only writes a debug line), so check the changelog of the plugin you use.

- **Private data stays private.** `omp-memory` handles the text of your memory banks. It prints counts, names and keys, not memory text, writes only `0600` files in `0700`
  directories, redacts and holds back anything that still looks like a secret before an import, and writes a bank only when you pass `--apply`, after a dry run and, for existing banks, a backup, and (best effort) when no other process has that bank open.

## Out of scope

What none of the plugins defends against, beyond what each plugin's own threat model lists:

- A process that runs as your user, or root.
- A machine that several people share.
- Flaws in omp, Bun, SQLite, Tailscale, ssh and the npm packages a plugin downloads when you ask it to (`omp-memory setup` runs the install scripts of the packages it fetches with your environment).
- Secrets with no recognisable shape in memory text (`omp-memory` redacts patterns; a bare password in prose is not caught: read the dry run).

## Status

None of the plugins is certified and no third-party audit has been done; treat the first releases as beta software. Each plugin's threat model states what it covers and what it does not.
