"""Start backends: how a new omp session is opened in a folder from the dashboard.

    off     (default) Start is not offered
    herdr   a new tab labelled `omp` in the folder's workspace (created when there is none)
    tmux    a new window in one tmux session (created when there is none)

Every backend takes only a path from the dashboard's own project list, passes it as one argv element (no shell), and types
exactly one fixed text into the new pane's shell: `omp`. Nothing is started unless a person clicks Start, and Start never
edits a project.

A backend also says where a running session lives (`place`: backend, workspace, tab, pane), so the Sessions page can show
"in herdr: sms > omp" or "in tmux: omp > api". For herdr that is what herdr reports; for tmux a session lives where its
process is below a pane's process.
"""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Protocol

from .hostplat import Platform
from .omp import OmpError, find_tool
from .util import DashboardError, RunResult, printable, run_bounded

OMP_COMMAND = "omp"  # the only text Start types into a new pane's shell
SESSION_FILE_ID = re.compile(r"_([0-9a-fA-F-]{36})\.jsonl\Z")
PANE_ID = re.compile(r"%[0-9]{1,9}\Z")
TMUX_FORMAT = "#{pane_id}\t#{pane_pid}\t#{session_name}\t#{window_name}"
START_WAIT_S = 25.0


class StartError(DashboardError):
    """Start was refused or failed; `status` is the HTTP status the hub answers with."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class Backend(Protocol):
    name: str

    def locations(self, hosts: list[dict[str, Any]]) -> dict[str, dict[str, Any]]: ...

    def start(self, path: str, list_hosts: Callable[[], list[dict[str, Any]]]) -> dict[str, Any]: ...


def _same_dir(a: str | None, b: str | None) -> bool:
    return bool(a and b) and os.path.realpath(a) == os.path.realpath(b)


def _inside(cwd: str | None, path: str) -> bool:
    """cwd is the project folder or a folder inside it (real paths)."""
    if not cwd:
        return False
    cwd, root = os.path.realpath(cwd), os.path.realpath(path)
    return cwd == root or cwd.startswith(root.rstrip("/") + "/")


def _check_path(path: object) -> str:
    if not isinstance(path, str) or not path.startswith("/") or "\x00" in path or "\n" in path:
        raise StartError(400, "unknown project")
    if Path(path).name.startswith("-"):
        raise StartError(400, "that project's folder name would be read as an option")
    return path


def _wait_for_session(
    path: str,
    before: set[str],
    list_hosts: Callable[[], list[dict[str, Any]]],
    wait_s: float,
    sleep: Callable[[float], None],
) -> str | None:
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        try:
            hosts = list_hosts()
        except (OmpError, OSError, ValueError):
            hosts = []
        for h in hosts:
            if h.get("instanceId") not in before and _same_dir(h.get("cwd"), path):
                return h["instanceId"]
        sleep(1)
    return None


# ---------------------------------------------------------------------------------------------------------------------
# herdr
# ---------------------------------------------------------------------------------------------------------------------


class Herdr:
    name = "herdr"

    def __init__(
        self,
        binary: Callable[[], str | None],
        env: Mapping[str, str] | None = None,
        run: Callable[..., RunResult] = run_bounded,
        wait_s: float = START_WAIT_S,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._binary = binary
        self._env = dict(os.environ if env is None else env)
        self._run = run
        self.wait_s = wait_s
        self.sleep = sleep

    def _call(self, *args: str, parse: bool = True) -> Any:
        binary = self._binary()
        if not binary:
            raise StartError(503, "herdr was not found: set start.herdr.bin or put herdr on PATH")
        result = self._run([binary, *args], 15.0, env=self._env, max_output=1024 * 1024)
        if not result.ok:
            raise StartError(502, "herdr did not answer")
        if not parse:
            return None
        try:
            return json.loads(result.stdout)["result"]
        except (ValueError, KeyError, TypeError):
            raise StartError(502, "herdr answered something this dashboard does not understand") from None

    def locations(self, hosts: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        """sessionId -> {backend, workspace, tab, pane} for omp sessions running in herdr panes ({} without herdr).

        herdr's omp integration reports each pane's session file; its name ends in `_<sessionId>.jsonl`.
        """
        try:
            panes = self._call("pane", "list")["panes"]
            workspaces = {w["workspace_id"]: w["label"] for w in self._call("workspace", "list")["workspaces"]}
            tabs = {t["tab_id"]: t["label"] for t in self._call("tab", "list")["tabs"]}
        except (DashboardError, KeyError, TypeError):
            return {}
        found: dict[str, dict[str, Any]] = {}
        for p in panes:
            s = p.get("agent_session") or {}
            if s.get("agent") != "omp" or not isinstance(s.get("value"), str):
                continue
            match = SESSION_FILE_ID.search(s["value"]) if s.get("kind") == "path" else None
            session_id = match.group(1) if match else s["value"] if s.get("kind") == "id" else None
            if session_id:
                found[session_id] = {
                    "backend": "herdr",
                    "workspace": _text(workspaces.get(p.get("workspace_id"))),
                    "tab": _text(tabs.get(p.get("tab_id"))),
                    "pane": _text(p.get("pane_id")),
                }
        return found

    def workspace_for(self, path: str) -> dict[str, Any] | None:
        """The herdr workspace for a project, never one shared with another folder.

        First a workspace with a pane in exactly this folder. Else one labelled with the folder name, but only when
        none of its panes is outside the project (two projects named `frontend` must not share one).
        """
        workspaces = self._call("workspace", "list")["workspaces"]
        panes = self._call("pane", "list")["panes"]
        exact = {p.get("workspace_id") for p in panes if _same_dir(p.get("cwd"), path)}
        for w in workspaces:
            if w.get("workspace_id") in exact:
                return w
        elsewhere = {p.get("workspace_id") for p in panes if p.get("cwd") and not _inside(p.get("cwd"), path)}
        name = Path(path).name
        return next((w for w in workspaces if w.get("label") == name and w.get("workspace_id") not in elsewhere), None)

    def start(self, path: str, list_hosts: Callable[[], list[dict[str, Any]]]) -> dict[str, Any]:
        path = _check_path(path)
        name = Path(path).name
        try:
            before = {h["instanceId"] for h in list_hosts()}
        except (OmpError, OSError, ValueError):
            raise StartError(503, "could not list sessions (omp collab list failed); nothing was started") from None
        try:
            workspace = self.workspace_for(path)
            if workspace is None:
                made = self._call("workspace", "create", "--cwd", path, "--label", name, "--no-focus")
                workspace, tab, pane = made["workspace"], made["tab"], made["root_pane"]["pane_id"]
                self._call("tab", "rename", tab["tab_id"], "omp")
                tab = {**tab, "label": "omp"}
            else:
                made = self._call(
                    "tab",
                    "create",
                    "--workspace",
                    workspace["workspace_id"],
                    "--cwd",
                    path,
                    "--label",
                    "omp",
                    "--no-focus",
                )
                tab, pane = made["tab"], made["root_pane"]["pane_id"]
            self._call("pane", "run", pane, OMP_COMMAND, parse=False)  # prints nothing on success
        except (KeyError, TypeError):
            raise StartError(502, "herdr answered something this dashboard does not understand") from None
        started = {
            "backend": "herdr",
            "workspace": _text(workspace.get("label")),
            "tab": _text(tab.get("label")),
            "pane": _text(pane),
            "instanceId": None,
        }
        started["instanceId"] = _wait_for_session(path, before, list_hosts, self.wait_s, self.sleep)
        return started


# ---------------------------------------------------------------------------------------------------------------------
# tmux
# ---------------------------------------------------------------------------------------------------------------------


class Tmux:
    name = "tmux"

    def __init__(
        self,
        binary: Callable[[], str | None],
        session: str,
        platform: Platform,
        env: Mapping[str, str] | None = None,
        run: Callable[..., RunResult] = run_bounded,
        wait_s: float = START_WAIT_S,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._binary = binary
        self.session = session
        self.platform = platform
        # Whatever tmux the hub was started inside, the default socket is the one used.
        self._env = {k: v for k, v in (os.environ if env is None else env).items() if k not in ("TMUX", "TMUX_PANE")}
        self._run = run
        self.wait_s = wait_s
        self.sleep = sleep

    def _tmux(self, *args: str, check: bool = True) -> RunResult:
        binary = self._binary()
        if not binary:
            raise StartError(503, "tmux was not found: set start.tmux.bin or put tmux on PATH")
        result = self._run([binary, *args], 15.0, env=self._env, max_output=256 * 1024)
        if check and not result.ok:
            raise StartError(502, "tmux did not do that (is its server reachable?)")
        return result

    def locations(self, hosts: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        """sessionId -> {backend, workspace (tmux session), tab (window), pane}: the pane whose process is the session
        process or one of its ancestors."""
        try:
            listing = self._tmux("list-panes", "-a", "-F", TMUX_FORMAT, check=False)
            if not listing.ok:
                return {}
            panes: dict[int, tuple[str, str, str]] = {}
            for line in listing.stdout.splitlines():
                parts = line.split("\t")
                if len(parts) == 4 and PANE_ID.match(parts[0]) and parts[1].isdigit():
                    panes[int(parts[1])] = (parts[0], parts[2], parts[3])
            if not panes:
                return {}
            table = self.platform.process_table()
        except (DashboardError, OSError):
            return {}
        found: dict[str, dict[str, Any]] = {}
        for h in hosts:
            pid, session_id = h.get("pid"), h.get("sessionId")
            if not isinstance(pid, int) or isinstance(pid, bool) or not session_id:
                continue
            seen: set[int] = set()
            current = pid
            for _ in range(64):  # the pane's own process, or one of omp's parents
                if current in panes:
                    pane, session, window = panes[current]
                    found[session_id] = {
                        "backend": "tmux",
                        "workspace": _text(session),
                        "tab": _text(window),
                        "pane": pane,
                    }
                    break
                entry = table.get(current)
                if entry is None or entry[0] <= 1 or entry[0] in seen:
                    break
                seen.add(current)
                current = entry[0]
        return found

    def start(self, path: str, list_hosts: Callable[[], list[dict[str, Any]]]) -> dict[str, Any]:
        path = _check_path(path)
        if ";" in path:  # tmux reads a word that ends in ';' as the end of one command and the start of the next
            raise StartError(
                400, "tmux cannot open a folder whose path has a ';' in it; use another backend or rename it"
            )
        label = printable(Path(path).name, 40).strip() or "omp"
        # tmux expands #{...} formats in a window name and in a start directory: '#' is doubled so that a folder named
        # "a#(command)" is that folder, not a command.
        label_arg, path_arg = label.replace("#", "##"), path.replace("#", "##")
        try:
            before = {h["instanceId"] for h in list_hosts()}
        except (OmpError, OSError, ValueError):
            raise StartError(503, "could not list sessions (omp collab list failed); nothing was started") from None
        exists = self._tmux("has-session", "-t", f"={self.session}", check=False).ok
        if exists:
            made = self._tmux(
                "new-window", "-d", "-t", f"={self.session}:", "-n", label_arg, "-c", path_arg, "-P", "-F", "#{pane_id}"
            )
        else:
            made = self._tmux(
                "new-session", "-d", "-s", self.session, "-n", label_arg, "-c", path_arg, "-P", "-F", "#{pane_id}"
            )
        pane = made.stdout.strip()
        if not PANE_ID.match(pane):
            raise StartError(502, "tmux answered something this dashboard does not understand")
        # One fixed text, typed literally (-l: no key names are looked up), then Enter.
        self._tmux("send-keys", "-t", pane, "-l", "--", OMP_COMMAND)
        self._tmux("send-keys", "-t", pane, "Enter")
        return {
            "backend": "tmux",
            "workspace": self.session,
            "tab": label,
            "pane": pane,
            "instanceId": _wait_for_session(path, before, list_hosts, self.wait_s, self.sleep),
        }


def _text(value: object) -> str | None:
    return printable(value, 80) if isinstance(value, str) and value else None


def build_backend(
    config: Any,
    platform: Platform,
    env: Mapping[str, str] | None = None,
) -> Backend | None:
    """The backend the configuration names, or None for `off`."""
    environ = dict(os.environ if env is None else env)
    kind = config.get("start.backend")
    if kind == "herdr":
        return Herdr(lambda: find_tool("herdr", config.get("start.herdr.bin"), environ, "HERDR_BIN"), environ)
    if kind == "tmux":
        return Tmux(
            lambda: find_tool("tmux", config.get("start.tmux.bin"), environ),
            config.get("start.tmux.session"),
            platform,
            environ,
        )
    return None
