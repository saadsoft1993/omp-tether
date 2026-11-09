"""Test kit: a scratch HOME, a stand-in `omp`, a hub on a free loopback port (in this process or as a child), and a small
HTTP client that sends what a browser sends. Nothing here touches the real home directory, a fixed port or the network.
"""

from __future__ import annotations

import contextlib
import http.client
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

PLUGIN = Path(__file__).resolve().parents[2]
PYTHON_DIR = PLUGIN / "python"
MAIN = PYTHON_DIR / "omp_dashboard_main.py"
sys.dont_write_bytecode = True
sys.path.insert(0, str(PYTHON_DIR))

from omp_dashboard import config as config_module
from omp_dashboard import paths as paths_module
from omp_dashboard.server import HubApp, HubServer

FAKE_OMP = """#!{python}
import json, os, sys
here = os.path.dirname(os.path.abspath(__file__))
state = json.load(open(os.path.join(here, "fake-omp.json")))
args = sys.argv[1:]
if os.environ.get("BUN_BE_BUN"):
    sys.exit(97)  # the real omp would behave as a plain Bun: it must never be reached with this variable
if state.get("fail"):
    sys.exit(1)
if args[:1] == ["--version"]:
    print(state.get("version", "omp/18.18.3"))
elif args[:2] == ["collab", "list"]:
    print(json.dumps({{"version": 1, "hosts": state["hosts"]}}))
elif args[:2] == ["collab", "link"]:
    host = next((h for h in state["hosts"] if h["instanceId"] == args[2]), None)
    if host is None or state.get("linkFails"):
        sys.exit(1)
    kind = "view" if "--view" in args else "control"
    print(json.dumps({{"version": 1, "url": state["links"][host["instanceId"]][kind], "generation": host.get("generation", 1)}}))
elif args[:2] == ["config", "get"]:
    print(state.get("config", {{}}).get(args[2], "undefined"))
else:
    sys.exit(2)
"""


def free_port() -> int:
    with contextlib.closing(socket.socket()) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Scratch:
    """A scratch HOME under /tmp (short paths: Unix sockets have a length limit), removed by `close()`."""

    def __init__(self, hosts: list[dict[str, Any]] | None = None, **settings: Any):
        self.root = Path(tempfile.mkdtemp(prefix="odb", dir="/tmp")).resolve()
        os.chmod(self.root, 0o700)
        self.home = self.root / "h"
        self.home.mkdir(mode=0o700)
        self.bin = self.root / "bin"
        self.bin.mkdir(mode=0o700)
        self.port = free_port()
        self.hosts = hosts or []
        self.fake_state: dict[str, Any] = {"hosts": self.hosts, "links": {}, "config": {}}
        self.omp_path = self.bin / "omp"
        self.omp_path.write_text(FAKE_OMP.format(python=sys.executable))
        os.chmod(self.omp_path, 0o700)
        self.save_fake()
        self.settings = settings
        self.env = {
            "HOME": str(self.home),
            "PATH": "/usr/bin:/bin",
            "XDG_CONFIG_HOME": str(self.home / ".config"),
            "XDG_STATE_HOME": str(self.home / ".local" / "state"),
            "OMP_BIN": str(self.omp_path),
            "OMP_DASHBOARD_PORT": str(self.port),
            "LANG": "C.UTF-8",
        }
        self.env.update({k: str(v) for k, v in settings.items()})

    def save_fake(self) -> None:
        (self.bin / "fake-omp.json").write_text(json.dumps(self.fake_state))

    def paths(self) -> paths_module.Paths:
        return paths_module.from_env(self.env)

    def write_config(self, values: dict[str, Any]) -> Path:
        paths = self.paths()
        paths.config_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        doc = {"schemaVersion": 1, **values}
        paths.config_file.write_text(json.dumps(doc))
        os.chmod(paths.config_file, 0o600)
        return paths.config_file

    def config(self) -> config_module.Config:
        return config_module.load_config(self.paths(), self.env)

    def close(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)


class Response:
    def __init__(self, status: int, headers: dict[str, str], body: bytes):
        self.status = status
        self.headers = headers
        self.body = body

    def json(self) -> Any:
        return json.loads(self.body)


class Client:
    """Sends what a browser sends to the hub: the right Host, an Origin and the custom header on POSTs, a cookie."""

    def __init__(self, port: int, host: str | None = None, origin: str | None = None):
        self.port = port
        self.host = host or f"127.0.0.1:{port}"
        self.origin = origin if origin is not None else f"http://{self.host}"
        self.cookie: str | None = None

    def request(
        self,
        method: str,
        path: str,
        body: Any = None,
        headers: dict[str, str] | None = None,
        post_headers: bool = True,
        raw_body: bytes | None = None,
    ) -> Response:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        try:
            h: dict[str, str] = {"Host": self.host, "User-Agent": "Test Browser/1.0"}
            if method == "POST" and post_headers:
                h["Origin"] = self.origin
                h["X-Omp-Dashboard"] = "1"
                h["Content-Type"] = "application/json"
            if self.cookie:
                h["Cookie"] = self.cookie
            h.update(headers or {})
            data = raw_body if raw_body is not None else (json.dumps(body).encode() if body is not None else None)
            conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
            for key, value in h.items():
                if value is not None:
                    conn.putheader(key, value)
            if data is not None:
                conn.putheader("Content-Length", str(len(data)))
            conn.endheaders(data)
            resp = conn.getresponse()
            return Response(resp.status, {k: v for k, v in resp.getheaders()}, resp.read())
        finally:
            conn.close()

    def get(self, path: str, **kw: Any) -> Response:
        return self.request("GET", path, **kw)

    def post(self, path: str, body: Any = None, **kw: Any) -> Response:
        return self.request("POST", path, body=body if body is not None else {}, **kw)

    def pair_with(self, token: str) -> Response:
        resp = self.post("/api/hub/pair", {"token": token})
        set_cookie = resp.headers.get("Set-Cookie", "")
        if resp.status == 200 and set_cookie:
            self.cookie = set_cookie.split(";", 1)[0]
        return resp


class InProcessHub:
    """The hub's real server on the scratch port, in a thread of this process."""

    def __init__(self, scratch: Scratch, config_values: dict[str, Any] | None = None, platform: Any = None):
        self.scratch = scratch
        if config_values is not None:
            scratch.write_config(config_values)
        self.config = scratch.config()
        if self.config.problems:
            raise AssertionError(f"scratch config invalid: {self.config.problems}")
        self.app = HubApp(self.config, scratch.env, platform)
        self.server = HubServer(("127.0.0.1", scratch.port), self.app)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.client = Client(scratch.port)

    def pair(self, client: Client | None = None) -> Client:
        client = client or self.client
        token = self.app.pairing.writer.create_token()
        resp = client.pair_with(token)
        assert resp.status == 200, (resp.status, resp.body)
        return client

    def close(self) -> None:
        self.app.stop.set()
        self.server.shutdown()
        self.server.server_close()


def host_entry(i: int = 1, **over: Any) -> dict[str, Any]:
    """A host as `omp collab list --json` prints it."""
    base: dict[str, Any] = {
        "instanceId": f"inst{i:04d}",
        "generation": 1,
        "pid": 4000 + i,
        "sessionId": f"01a1b65b-93f3-7000-8000-{i:012d}",
        "sessionName": f"session {i}",
        "cwd": f"/work/project{i}",
        "model": {"provider": "test", "id": "tether-lite"},
        "startedAt": int(time.time() * 1000) - 600_000,
        "participants": 1,
        "relayConnected": True,
        "inputRequired": False,
        "busy": False,
    }
    base.update(over)
    return base


def run_hub_process(scratch: Scratch) -> subprocess.Popen[bytes]:
    """`omp-dashboard run` as a child, with the scratch environment; returns once /healthz answers."""
    proc = subprocess.Popen(
        [sys.executable, str(MAIN), "run"],
        env={**scratch.env, "PYTHONDONTWRITEBYTECODE": "1"},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            out = proc.stdout.read().decode() if proc.stdout else ""
            raise AssertionError(f"the hub exited early ({proc.returncode}): {out}")
        try:
            resp = Client(scratch.port).get("/healthz")
            if resp.status == 200:
                return proc
        except OSError:
            pass
        time.sleep(0.1)
    proc.kill()
    raise AssertionError("the hub did not start")


def stop_process(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
    if proc.stdout:
        proc.stdout.close()


class ScratchTestCase(unittest.TestCase):
    """A TestCase with a scratch HOME per test."""

    def make(self, hosts: list[dict[str, Any]] | None = None, **settings: Any) -> Scratch:
        scratch = Scratch(hosts, **settings)
        self.addCleanup(scratch.close)
        return scratch

    def hub(self, scratch: Scratch, config: dict[str, Any] | None = None, platform: Any = None) -> InProcessHub:
        hub = InProcessHub(scratch, config, platform)
        self.addCleanup(hub.close)
        return hub


def shared_group(case: unittest.TestCase) -> None:
    """For the rest of the test, every group counts as one with other members, as it does on macOS and on a Linux machine whose groups are
    shared. Without this, a Linux user whose own group has no other member (Ubuntu's default) would have a group-writable directory accepted."""
    patcher = mock.patch("omp_dashboard.trustwalk.group_is_private", return_value=False)
    patcher.start()
    case.addCleanup(patcher.stop)
