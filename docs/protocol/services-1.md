# services/1

How a plugin asks `omp-services` to keep one of its programs running. A plugin cannot install a daemon itself (omp
plugins ship no install hooks), so it writes one JSON file; `omp-services init` renders the launchd or `systemd --user`
unit from it, shows the plan, and loads it after a yes. The schema is
`plugins/omp-services/python/omp_services/schemas/services-1.schema.json`.

Words in capitals (MUST, SHOULD, MAY) have their usual RFC 2119 meaning.

## 1. The file

| Item | Rule |
|---|---|
| Location | `$XDG_CONFIG_HOME/omp-services/services.d/<plugin>.json` (default `~/.config/omp-services/services.d/`) |
| Name, owner, mode, size | the same rules as a health drop-in ([`health-1.md`](health-1.md) section 2): a plain file of this user that nobody else can write, in a directory nobody else can write, at most 64 KiB, `<plugin>` equals the `plugin` field |
| An unusable file | is not used; `init` and `doctor` say why. Its services are not installed |

```json
{
  "protocol": "services/1",
  "plugin": "omp-example",
  "services": [
    {
      "name": "hub",
      "description": "Example hub",
      "program": { "type": "omp-script", "script": "/home/me/.example/hub.ts", "args": ["--port", "8123"] },
      "env": { "EXAMPLE_MODE": "local" },
      "workingDirectory": "/home/me/.example",
      "protected": false,
      "throttleSeconds": 10
    }
  ]
}
```

## 2. Programs

| `program.type` | Runs | Notes |
|---|---|---|
| `omp-script` | `<pinned omp> <script> <args...>` with `BUN_BE_BUN=1` | The script runs on the Bun that omp embeds, so no Bun or Node install is needed. The omp path is the absolute path pinned by `init` (never looked up at start time). A service that starts `omp` itself MUST clear `BUN_BE_BUN` for the child |
| `python` | `<pinned python> <script> <args...>` | The Python (3.10 or newer) that `init` pinned |
| `exec` | `argv[0] argv[1...]` | `argv[0]` MUST be an absolute path |

There is no shell: arguments are passed as given. `script` is an absolute path. Every unit starts with a small `PATH`:
omp-services' own `bin` directory first (a `bun` that is omp's embedded Bun, an `omp` that is the pinned binary), the
directory of the pinned omp, then the system directories.

`env` holds non-secret values only; a secret belongs in a `0600` file in a `0700` directory that the service reads
itself. Names MUST match `[A-Z_][A-Z0-9_]*`. `PATH`, `HOME`, `BUN_BE_BUN`, `LD_PRELOAD`, `LD_LIBRARY_PATH`,
`DYLD_INSERT_LIBRARIES`, `DYLD_LIBRARY_PATH` and `OMP_SERVICES_*` are reserved and refused.

## 3. What every unit gets

| | launchd | systemd (`--user`) |
|---|---|---|
| Label / unit | `<prefix>.<plugin>.<name>` | `<prefix>.<plugin>.<name>.service` |
| Restart | `KeepAlive`, `ThrottleInterval` = `throttleSeconds` (default 10) | `Restart=always`, `RestartSec` = `throttleSeconds` |
| File mode | `Umask 63` (077) | `UMask=0077` |
| Log | `StandardOutPath` and `StandardErrorPath` in `<state>/logs/<plugin>.<name>.log` | `StandardOutput=append:` and `StandardError=append:` the same file |
| Other | `RunAtLoad` | `NoNewPrivileges=yes`, `WantedBy=default.target` |
| Unit file | `~/Library/LaunchAgents/<label>.plist` | `$XDG_CONFIG_HOME/systemd/user/<label>.service` |

The label prefix is configurable (default `omp-tether`). The renderer and the installer only ever touch labels that start
with `<prefix>.` and that omp-services wrote (carrying its "Managed by omp-services" comment, or byte for byte what its
state file recorded). The unit file is derived from the label, never from a path in a file, and ownership is checked again
at the moment of every action; a unit with the same label that it did not write is reported and left alone: a typo or a hostile drop-in cannot make it unload a unit it did not install.

Only long-running programs are supported. A job that should run on a schedule belongs in its own plugin.

## 4. When units change: reload only what changed

`init` renders every unit and compares the text with the file on disk.

- Unchanged and loaded: **left running**. Nothing is stopped.
- Unchanged and not loaded: loaded.
- Changed: reloaded (stopped, rewritten, started). Only that unit.
- No longer declared: stopped and deleted.
- **`protected: true`**: a changed or no-longer-declared protected unit is **never** reloaded, restarted or removed. `init`
  prints the warning and leaves the unit and its file untouched until the person names it:
  `omp-services init --restart-protected <plugin>.<name>`. Use it for a program whose restart would destroy something the
  person is in the middle of (a terminal multiplexer server holding live sessions, for example). `uninstall` leaves
  protected units, and the wrappers, code release and state they still need, alone unless given `--include-protected`;
  `purge` refuses while any exist. Protection is remembered from what was installed: a later declaration that drops the
  flag does not lift it, and a protected unit whose file went missing is not reloaded either.
- Nothing is installed or started until the person confirms the plan, and `init --plan` shows it without doing anything.

## 5. Watching and restarting

A plugin watches its own service with a `watchdog` check in its `health.d` drop-in ([`health-1.md`](health-1.md)
section 4): bounded restarts per window, never for a protected service (no setting overrides that), never from `doctor` or `health`.

## 6. Versioning

`services/1` changes only by this document. A file with another `protocol` is unusable.
