# The hub's HTTP API

All routes are on the hub's loopback address. Every request first passes the sign-in adapter (403, or 503 while Tailscale's owner is unknown); with `auth.adapter` `both` each request is judged by exactly one of the two rule sets, chosen by whether it carries any sign of a proxy (see the README), and a 503 then reaches proxied requests only. Every
`/api/hub/*` route except pairing then needs a paired browser's cookie (401). POSTs need `X-Omp-Dashboard: 1` and an `Origin` equal to the origin of the address the request came by (`http://127.0.0.1:<port>` and the like on the local address, `https://<name>:<servePort>` through Serve; the other one is refused)
(403 otherwise), `Content-Type: application/json`, a `Content-Length` of at most 4096 bytes and a JSON object body. Answers are JSON.
Errors are `{"error": "..."}` with a fixed text; some add `code`. The API is for the dashboard page; it is not a stable interface for other programs.

| Route | Answer |
|---|---|
| `GET /healthz` | `{"ok": true, "updatedAt": <epoch seconds>}`; only to a direct loopback client with the hub's own `Host`, no `Origin`, and none of the proxy-sign headers (any `Tailscale-*` or `X-Forwarded-*`, `Forwarded`, `Via`, `X-Real-IP`) |
| `GET /`, `/index.html`, `/theme.js`, `/index.js`, `/index.css`, `/favicon.svg` | the page (unpaired too, so the Pair screen renders); 503 if it is damaged |
| `GET /api/hub/sessions` | `{sessions: [...], now, controlProblem}`; each session has the known fields of `omp collab list --json` plus `relay` (`private`, `public`, `unknown`, `off`), `control` (Stop/Rename possible) and `place` (herdr or tmux location or null) |
| `GET /api/hub/machine` | `{name, noun}` |
| `GET /api/hub/about` | `{version, adapter ("loopback-only", "tailscale-serve" or "both"), start, relay, statsUrl}` |
| `GET /api/hub/relay` | `{enabled, ok, error, rooms, guests, uptime_s, build}`, counts believed only with the relay's MAC |
| `GET /api/hub/projects` | `{backend, projects: [{path, name, label, last_used}]}` |
| `GET /api/hub/alerts` | `{enabled, server, sent, last_sent_at, last_error}`; never the topic |
| `POST /api/hub/pair` | `{token}` -> `{ok}` and `Set-Cookie` (`Secure` only for a request that came through Serve); 403 for a wrong/used/expired token, whichever address redeems it first; 429 after 10 failures in 5 minutes (all clients together). A token that is on file, unused and unexpired does not count against the limit. The cookie's `Max-Age` is `pairing.maxAgeDays` (default 90 days); with `auth.adapter` `both` a cookie set on the local address lasts `pairing.localMaxAgeDays` instead (default 7 days, never more than `pairing.maxAgeDays`), and the device is no longer paired after the same time |
| `POST /api/hub/link` | `{instanceId, view}` -> `{url, generation, relay}`; 409 `relay_off`, 409 still on the public relay, 503 if the relay does not prove itself (checked after the link is fetched, just before it is handed out) or while the relay's web address is not known; audited, refusals too. With `both` the relay admits one way (`relay.admits`): a request of the other way gets 409 `relay_other_path` and no link, and a local request whose `Host` is not `127.0.0.1` or `localhost` at the hub's own port gets 409 `relay_address`; the link opens at the `Host` name the browser used |
| `POST /api/hub/start` | `{path}` -> `{backend, workspace, tab, pane, instanceId}`; 409 `start_off`; only a path from `/api/hub/projects` |
| `POST /api/hub/alerts/test` | `{ok}`; 400 while no topic is set; audited |
| `POST /api/hub/sessions/<sessionId>/stop` | `{outcome: stopped|was_idle}` |
| `POST /api/hub/sessions/<sessionId>/rename` | `{name}` -> `{outcome: renamed, name}` |
| `POST /api/hub/sessions/<sessionId>/kill` | `{confirm}` (the exact session name or project folder) -> `{outcome: exited|still_running, pid}` |
| `POST /api/hub/sessions/<sessionId>/force-kill` | `{confirm}`; only after a Kill that did not end the session -> `{outcome: force_exited|force_still_running, pid, descendants, remaining, parent_running}`; `descendants` is how many processes below the session were signalled (each re-checked by start time just before), `remaining` how many of them and the session are still running afterwards, `parent_running` whether the session's own process is one of them |

Every other path is 404, PUT, DELETE, PATCH and OPTIONS are 405, any other method is 501, a POST that is not `application/json` is 415, a malformed target 400, an oversized header block 431.
