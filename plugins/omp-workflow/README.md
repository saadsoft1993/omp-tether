# omp-workflow

Installs optional workflow settings, role agents and skills into your omp profile. Installing the plugin alone changes nothing in your profile. Python 3.10 or newer and `omp` on PATH are required.

Plugin commands are not added to PATH. Find this plugin's directory with `omp plugin list --json`, then run:

```sh
<plugin-directory>/bin/omp-workflow init --models anthropic --dry-run
<plugin-directory>/bin/omp-workflow init --models anthropic
```

Choose `--models anthropic-codex` for Anthropic and Codex, or `--models none` to keep your existing model setup. Add `--no-advisor` to omit advisor settings, `--display` for the optional display settings, or `--overwrite` to replace existing settings. The plan is printed before confirmation; `--yes` skips confirmation. Restart existing omp sessions to pick up changes.

`<plugin-directory>/bin/omp-workflow status` reports whether installed settings and files have changed. `<plugin-directory>/bin/omp-workflow uninstall` restores unchanged settings and removes unchanged installed files. It leaves user-modified items in place. The state file is stored in `${XDG_STATE_HOME:-$HOME/.local/state}/omp-workflow/`; a backup of existing `config.yml` is made before changes.
