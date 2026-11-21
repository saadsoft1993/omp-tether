# Install prompt

Paste the text below into a new omp session on the machine you want to set up. It asks what you want before it changes
anything, and it uses only your own accounts and machines.

```text
Set up omp on this machine the way Saad's setup works, using only my own accounts and machines. Sources: github.com/saadsoft1993/omp-tether (plugins and the omp-workflow pack), github.com/saadsoft1993/omp-insights (stats dashboard), github.com/saadsoft1993/omp-hindsight-guard (optional memory server guard). Read each component's README before acting on it.

Step 1. Ask me first, in one form, with these defaults:
- Model setup: Anthropic only (default) / Anthropic + OpenAI Codex (needs my ChatGPT/Codex login) / keep my current models.
- Workflow pack: yes (default) / no. Say what it changes: plan mode at session start, the plan → verify → execute → review rules, 8 role agents and the skills copied into my omp folder, hashline edits, auto-learn, local per-project memory (Mnemopi). It keeps any setting I already have.
- Advisor (a second model reviews each turn; costs extra model calls): yes (default) / no.
- Saad's look (status line, titles, display): no (default) / yes.
- Plugins: omp-session-control, omp-services, omp-private-relay, omp-dashboard, omp-memory (all checked by default).
- Redesigned stats dashboard (clones omp-insights, needs Bun 1.4.4+, tested on Linux x64 and macOS arm64): yes (default) / no.
- Optional third parties, all off by default, one line each: Tailscale (reach the dashboard and relay from my other devices), ntfy push alerts (ntfy.sh or my own server), heartbeat monitor URL (e.g. healthchecks.io), herdr or tmux (start sessions from the dashboard), self-hosted Hindsight memory server with omp-hindsight-guard.
Do nothing until I answer. Never configure anything that sends data off this machine unless I chose it and gave you the value.

Step 2. Check `omp --version` (tested: omp-session-control on 18.18.0 and 18.19.0; omp-services on 18.18.0, 18.19.0 and 19.0.0; omp-private-relay, omp-dashboard and omp-memory on 18.18.0, 18.18.3 and 19.0.0; omp-workflow on 19.0.0; on another version tell me which pieces are untested and ask), Python 3.10+, git, and Bun 1.4.4+ if I chose the dashboard or omp-memory. Report anything missing; don't install system packages without asking.

Step 3. `omp plugin marketplace add saadsoft1993/omp-tether`, then `omp plugin install <name>@omp-tether` for each chosen plugin, plus `omp-workflow@omp-tether` if I chose the workflow pack. Plugin commands are not on PATH: get each plugin's directory from `omp plugin list --json` and run `<dir>/bin/<command>`. Tell me to restart running omp sessions afterwards.

Step 4. Workflow pack: run `<omp-workflow dir>/bin/omp-workflow init --dry-run` with `--models anthropic`, `--models anthropic-codex` or `--models none` to match my answer, plus `--no-advisor` if I said no to the advisor and `--display` if I chose Saad's look. Show me the plan, then run it without `--dry-run` after I confirm. Undo is `omp-workflow uninstall`.

Step 5. Turn on each chosen plugin by following its README: session-control on; `omp-services init` (show me its plan, wait for yes); omp-private-relay `init` with the loopback/token adapter unless I chose Tailscale, then register it with omp-services; omp-dashboard run on loopback and walk me through pairing my browser; omp-memory `doctor` only (no `setup` download and no `--apply` import until I ask). Use ntfy, a heartbeat URL, Tailscale or herdr/tmux only if I chose them.

Step 6. Stats dashboard, if chosen: follow "Run the dashboard yourself" in FORK.md of github.com/saadsoft1993/omp-insights (branch insights) and run it on 127.0.0.1:3850. Offer to make it start at login (launchd agent on macOS, systemd --user unit on Linux); show me the unit file before loading it.

Step 7. Hindsight, if chosen: follow docs/server-setup.md in omp-hindsight-guard to run my own Hindsight, then `uv tool install git+https://github.com/saadsoft1993/omp-hindsight-guard@v0.1.3`, `omp-hindsight-guard init` with my own bank-to-folder mapping (ask me for it), `overlays`, `doctor`. Never put API keys in a file that gets committed or in a chat message.

Step 8. Finish with one table: component | version | enabled | check result | URL or command to use it. Then list what I skipped and how to add it later.
```

## Enigmatix team

If you work at Enigmatix and have access to the private github.com/saadsoft1993/omp-fleet, follow its TEAM-SETUP.md instead of this prompt. It sets up the same plugins plus Saad's hub dashboard (the control center on port 8794), his agents, rules and skills, and a template of his settings. Secrets stay in your own AWS account; the repos only hold their names.
