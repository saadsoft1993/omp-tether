#!/usr/bin/env python3
"""Conformance suite for the health/1 protocol (docs/protocol/health-1.md).

It talks to an implementation through one command only: `<impl> <drop-in file>` runs every check of the file once,
without restart actions, prints a JSON array of results in check order and exits 0 (nothing counts), 1 (something
counts) or 2 (the drop-in is invalid; the output is {"invalid": [...]}). It imports nothing from the implementation, so
it runs against any implementation of the protocol. Standard library only; Python 3.10 or newer.

    python3 health_1_suite.py --impl "bin/omp-services health --json --no-actions --file"
    python3 health_1_suite.py --selftest        # run against deliberately broken implementations

Exit status: 0 every case passed, 1 a case failed, 2 usage error.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

CASES: list[tuple[str, Callable[[Env], None]]] = []
TOKEN = "conf-" + os.urandom(6).hex()  # built at run time so secret scanners have nothing to find


class Fail(Exception):
    pass


def case(name: str):
    def register(fn):
        CASES.append((name, fn))
        return fn

    return register


def expect(condition: bool, message: str) -> None:
    if not condition:
        raise Fail(message)


# ---------------------------------------------------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args) -> None:
        pass

    def do_GET(self) -> None:
        srv: Server = self.server.owner  # type: ignore[attr-defined]
        srv.requests.append(self.path)
        status, body, headers, delay = srv.routes.get(self.path.split("?")[0], (200, b"ok", {}, 0))
        if delay:
            time.sleep(delay)
        try:
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
        except OSError:
            pass


class Server:
    def __init__(self) -> None:
        self.routes: dict[str, tuple] = {}
        self.requests: list[str] = []
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.owner = self  # type: ignore[attr-defined]
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def route(self, path: str, status: int = 200, body: str | bytes = b"ok", headers=None, delay: float = 0) -> None:
        self.routes[path] = (status, body.encode() if isinstance(body, str) else body, headers or {}, delay)

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Env:
    def __init__(self, impl: list[str]) -> None:
        self.impl = impl
        self.root = Path(tempfile.mkdtemp(prefix="h1c"))
        self.home = self.root / "home"
        self.home.mkdir()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.servers: list[Server] = []
        self.counter = 0
        self.tailscale_json: str | None = None

    def server(self) -> Server:
        srv = Server()
        self.servers.append(srv)
        return srv

    def close(self) -> None:
        for srv in self.servers:
            srv.stop()
        shutil.rmtree(self.root, ignore_errors=True)

    def script(self, name: str, text: str) -> str:
        path = self.root / name
        path.write_text(text)
        path.chmod(0o755)
        return str(path)

    def fake_tailscale(self, serve_status: str | None, rc: int = 0) -> None:
        text = f"#!{sys.executable}\nimport sys\nsys.stdout.write({(serve_status or '')!r})\nsys.exit({rc})\n"
        (self.bin / "tailscale").write_text(text)
        (self.bin / "tailscale").chmod(0o755)

    def dropin(
        self, checks: list[dict], plugin: str = "conf", mode: int = 0o600, raw: str | None = None, doc=None
    ) -> Path:
        self.counter += 1
        directory = self.root / f"d{self.counter}"
        directory.mkdir(mode=0o700)
        path = directory / f"{plugin}.json"
        body = (
            raw if raw is not None else json.dumps(doc or {"protocol": "health/1", "plugin": plugin, "checks": checks})
        )
        path.write_text(body)
        path.chmod(mode)
        return path

    def run(self, checks: list[dict] | None = None, timeout: float = 60, **kw):
        """Returns (exit status, parsed output, seconds)."""
        path = self.dropin(checks or [], **kw)
        env = {
            "HOME": str(self.home),
            "PATH": f"{self.bin}:{os.environ.get('PATH', '/usr/bin:/bin')}",
            "LANG": "C",
            "OSV_CONFORMANCE": "1",
        }
        started = time.monotonic()
        done = subprocess.run(
            [*self.impl, str(path)],
            env=env,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            timeout=timeout,
        )
        elapsed = time.monotonic() - started
        try:
            parsed = json.loads(done.stdout)
        except ValueError:
            parsed = None
        self.last_stdout = done.stdout + done.stderr
        return done.returncode, parsed, elapsed

    def statuses(self, checks: list[dict], **kw) -> tuple[int, list[str], float]:
        rc, parsed, elapsed = self.run(checks, **kw)
        expect(isinstance(parsed, list), f"expected a JSON array, got: {self.last_stdout[:200]!r}")
        for item in parsed:
            expect(
                isinstance(item, dict) and {"id", "status", "reason"} <= set(item),
                f"result lacks id/status/reason: {item}",
            )
        return rc, [r["status"] for r in parsed], elapsed

    def one(self, check: dict, **kw) -> tuple[int, str, float]:
        rc, statuses, elapsed = self.statuses([{"id": "c", **check}], **kw)
        expect(len(statuses) == 1, f"one check must give one result, got {statuses}")
        return rc, statuses[0], elapsed


def want(env: Env, check: dict, status: str, rc: int | None = None, **kw) -> None:
    got_rc, got, _ = env.one(check, **kw)
    expect(got == status, f"expected status {status}, got {got}")
    if rc is not None:
        expect(got_rc == rc, f"expected exit {rc}, got {got_rc}")


# ---------------------------------------------------------------------------------------------------------------------
# the document and the file rules
# ---------------------------------------------------------------------------------------------------------------------

GOOD = {"id": "c", "type": "command", "argv": [sys.executable, "-c", "pass"]}


def invalid(env: Env, **kw) -> None:
    rc, parsed, _ = env.run(**kw)
    expect(rc == 2, f"an unusable drop-in must exit 2, got {rc}: {env.last_stdout[:200]!r}")
    expect(isinstance(parsed, dict) and parsed.get("invalid"), "exit 2 must print {'invalid': [reasons]}")


@case("document: a valid minimal drop-in runs")
def _(env):
    want(env, GOOD, "ok", rc=0)


@case("document: unusable drop-ins are refused with exit 2")
def _(env):
    invalid(env, checks=[GOOD], mode=0o666)
    invalid(env, doc={"protocol": "health/1", "plugin": "other", "checks": [GOOD]})
    invalid(env, doc={"protocol": "health/1", "plugin": "conf", "checks": [GOOD], "extra": 1})
    invalid(env, doc={"protocol": "health/2", "plugin": "conf", "checks": [GOOD]})
    invalid(env, doc={"protocol": "health/1", "plugin": "conf", "checks": [{"id": "c", "type": "magic"}]})
    invalid(env, doc={"protocol": "health/1", "plugin": "conf", "checks": [GOOD, GOOD]})
    invalid(env, doc={"protocol": "health/1", "plugin": "conf", "checks": []})
    invalid(env, doc={"protocol": "health/1", "plugin": "conf", "checks": [{**GOOD, "surprise": True}]})
    invalid(env, raw="{not json")
    invalid(env, plugin="Bad_Name", checks=[GOOD])


@case("document: a symbolic link is refused")
def _(env):
    real = env.dropin([GOOD], plugin="real")
    link = real.parent / "linked.json"
    link.symlink_to(real)
    env_done = subprocess.run(
        [*env.impl, str(link)],
        capture_output=True,
        text=True,
        env={"HOME": str(env.home), "PATH": os.environ.get("PATH", "")},
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    expect(env_done.returncode == 2, f"a symlinked drop-in must exit 2, got {env_done.returncode}")


@case("document: severity note never counts toward the exit status")
def _(env):
    rc, parsed, _ = env.run([{"id": "n", "type": "port", "port": free_port(), "severity": "note"}])
    expect(rc == 0, f"a failing note-only check must exit 0, got {rc}")
    expect(parsed and parsed[0]["status"] == "unreachable", "but it is still reported")


# ---------------------------------------------------------------------------------------------------------------------
# url
# ---------------------------------------------------------------------------------------------------------------------


@case("url: ok, wrong status, nothing listening")
def _(env):
    srv = env.server()
    srv.route("/up", 200)
    srv.route("/down", 503)
    want(env, {"type": "url", "url": f"{srv.base}/up"}, "ok", rc=0)
    want(env, {"type": "url", "url": f"{srv.base}/down"}, "failed", rc=1)
    want(env, {"type": "url", "url": f"{srv.base}/down", "expectStatus": [503]}, "ok")
    want(env, {"type": "url", "url": f"http://127.0.0.1:{free_port()}/"}, "unreachable", rc=1)


@case("url: a redirect is never followed")
def _(env):
    other, srv = env.server(), env.server()
    other.route("/landing", 200)
    srv.route("/old", 302, "", {"Location": f"{other.base}/landing"})
    want(env, {"type": "url", "url": f"{srv.base}/old"}, "failed")
    expect(other.requests == [], "the redirect target was contacted")


@case("url: a slow peer is cut off at the deadline")
def _(env):
    srv = env.server()
    srv.route("/slow", 200, delay=6)
    got_rc, status, elapsed = env.one({"type": "url", "url": f"{srv.base}/slow", "timeoutSeconds": 1})
    expect(status == "unreachable", f"a timeout is unreachable, got {status}")
    expect(elapsed < 5, f"took {elapsed:.1f}s for a 1 s deadline")


@case("url: freshness is stale, ok or failed")
def _(env):
    srv = env.server()
    now = time.time()
    srv.route("/fresh", 200, json.dumps({"at": now - 5}))
    srv.route("/old", 200, json.dumps({"at": now - 5000}))
    srv.route("/junk", 200, "nope")
    f = {"jsonPointer": "/at", "maxAgeSeconds": 60}
    want(env, {"type": "url", "url": f"{srv.base}/fresh", "freshness": f}, "ok")
    want(env, {"type": "url", "url": f"{srv.base}/old", "freshness": f}, "stale", rc=1)
    want(env, {"type": "url", "url": f"{srv.base}/junk", "freshness": f}, "failed")


@case("url and port: a host that is not loopback is invalid")
def _(env):
    want(env, {"type": "url", "url": "http://203.0.113.9/"}, "invalid", rc=1)
    want(env, {"type": "port", "host": "203.0.113.9", "port": 80}, "invalid")


@case("results never contain the URL")
def _(env):
    srv = env.server()
    srv.route(f"/{TOKEN}", 500)
    env.run([{"id": "c", "type": "url", "url": f"{srv.base}/{TOKEN}?k={TOKEN}"}])
    expect(TOKEN not in env.last_stdout, "the URL leaked into the output")
    env.run([{"id": "c", "type": "url", "url": f"http://127.0.0.1:{free_port()}/{TOKEN}"}])
    expect(TOKEN not in env.last_stdout, "the URL leaked into the output of a failed connection")


# ---------------------------------------------------------------------------------------------------------------------
# port, uid-owner
# ---------------------------------------------------------------------------------------------------------------------


@case("port: listening is ok, closed is unreachable")
def _(env):
    srv = env.server()
    want(env, {"type": "port", "port": srv.port}, "ok", rc=0)
    want(env, {"type": "port", "port": free_port()}, "unreachable", rc=1)


@case("uid-owner: own listener ok, another uid failed, nothing listening unreachable")
def _(env):
    srv = env.server()
    want(env, {"type": "uid-owner", "port": srv.port}, "ok")
    want(env, {"type": "uid-owner", "port": srv.port, "uid": os.geteuid() + 1}, "failed")
    want(env, {"type": "uid-owner", "port": free_port()}, "unreachable")


# ---------------------------------------------------------------------------------------------------------------------
# command, file-age
# ---------------------------------------------------------------------------------------------------------------------


@case("command: exit status and output")
def _(env):
    py = sys.executable
    want(env, {"type": "command", "argv": [py, "-c", "print('hello world')"], "expectOutputContains": "hello"}, "ok")
    want(env, {"type": "command", "argv": [py, "-c", "print('x')"], "expectOutputContains": "hello"}, "failed")
    want(env, {"type": "command", "argv": [py, "-c", "raise SystemExit(7)"]}, "failed", rc=1)
    want(env, {"type": "command", "argv": [py, "-c", "raise SystemExit(7)"], "expectExit": 7}, "ok")


@case("command: argv[0] must be absolute; there is no shell")
def _(env):
    want(env, {"type": "command", "argv": ["python3", "-c", "pass"]}, "invalid")
    want(
        env,
        {
            "type": "command",
            "argv": [
                sys.executable,
                "-c",
                "import sys; sys.exit(0 if sys.argv[1] == '$(id); echo' else 9)",
                "$(id); echo",
            ],
        },
        "ok",
    )


@case("command: the deadline kills the whole process group")
def _(env):
    marker = env.root / "grandchild-lived"
    child = env.script(
        "child.py", f"import pathlib, time\ntime.sleep(2.5)\npathlib.Path({str(marker)!r}).write_text('x')\n"
    )
    parent = env.script(
        "parent.py", f"import subprocess, sys, time\nsubprocess.Popen([sys.executable, {child!r}])\ntime.sleep(60)\n"
    )
    rc, status, elapsed = env.one({"type": "command", "argv": [sys.executable, parent], "timeoutSeconds": 1})
    expect(status == "failed", f"a command past its deadline is failed, got {status}")
    expect(elapsed < 6, f"took {elapsed:.1f}s for a 1 s deadline")
    time.sleep(3.5)
    expect(not marker.exists(), "a grandchild outlived the deadline")


@case("command: a small fixed environment and capped, unreported output")
def _(env):
    os.environ["CONF_LEAK"] = "leaked"
    try:
        want(
            env,
            {
                "type": "command",
                "argv": [sys.executable, "-c", "import os, sys; sys.exit(1 if 'CONF_LEAK' in os.environ else 0)"],
            },
            "ok",
        )
    finally:
        os.environ.pop("CONF_LEAK", None)
    env.run(
        [
            {
                "id": "c",
                "type": "command",
                "argv": [
                    sys.executable,
                    "-c",
                    "import sys; sys.stdout.write('A'*3000000); sys.stderr.write('CMD-OUTPUT-SECRET')",
                ],
            }
        ]
    )
    expect("CMD-OUTPUT-SECRET" not in env.last_stdout, "command output leaked into the result")


@case("file-age: fresh, stale, no match")
def _(env):
    d = env.root / "files"
    d.mkdir()
    fresh, old = d / "a.dump", d / "b.old"
    fresh.write_text("x")
    old.write_text("x")
    os.utime(old, (time.time() - 5000, time.time() - 5000))
    want(env, {"type": "file-age", "path": str(fresh), "maxAgeSeconds": 60}, "ok")
    want(env, {"type": "file-age", "path": str(old), "maxAgeSeconds": 60}, "stale", rc=1)
    want(env, {"type": "file-age", "path": f"{d}/*.none", "maxAgeSeconds": 60}, "failed")
    want(env, {"type": "file-age", "path": f"{d}/*.dump", "maxAgeSeconds": 60}, "ok")


# ---------------------------------------------------------------------------------------------------------------------
# serve-route, watchdog
# ---------------------------------------------------------------------------------------------------------------------


@case("serve-route: present is ok, absent or elsewhere is not-on-relay")
def _(env):
    route = json.dumps({"Web": {"host.example:8443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:9000"}}}}})
    env.fake_tailscale(route)
    want(env, {"type": "serve-route", "port": 8443, "target": "http://127.0.0.1:9000/"}, "ok")
    want(env, {"type": "serve-route", "port": 8443, "target": "http://127.0.0.1:9999"}, "not-on-relay", rc=1)
    want(env, {"type": "serve-route", "port": 443, "target": "http://127.0.0.1:9000"}, "not-on-relay")
    env.fake_tailscale("{}")
    want(env, {"type": "serve-route", "port": 8443, "target": "http://127.0.0.1:9000"}, "not-on-relay")
    env.fake_tailscale("", rc=1)
    want(env, {"type": "serve-route", "port": 8443, "target": "http://127.0.0.1:9000"}, "unreachable")


@case("watchdog: a service the plugin does not declare is invalid")
def _(env):
    want(env, {"type": "watchdog", "service": "nothing"}, "invalid")


# ---------------------------------------------------------------------------------------------------------------------
# runner
# ---------------------------------------------------------------------------------------------------------------------


def run_suite(impl: list[str], only: str | None = None) -> int:
    env = Env(impl)
    failed = 0
    try:
        for name, fn in CASES:
            if only and only not in name:
                continue
            try:
                fn(env)
            except Fail as exc:
                failed += 1
                print(f"FAIL {name}: {exc}")
            except Exception as exc:
                failed += 1
                print(f"FAIL {name}: {type(exc).__name__}: {exc}")
            else:
                print(f"PASS {name}")
    finally:
        env.close()
    total = sum(1 for n, _ in CASES if not only or only in n)
    print(f"{total - failed} passed, {failed} failed")
    return 1 if failed else 0


BROKEN = {
    "always ok": "import json,sys; print(json.dumps([{'id':'c','status':'ok','reason':'x'}])); sys.exit(0)",
    "never invalid": "import json,sys; print(json.dumps([])); sys.exit(0)",
}


def selftest() -> int:
    """Each broken implementation must fail at least one case; otherwise the suite proves nothing."""
    root = Path(tempfile.mkdtemp(prefix="h1s"))
    bad = 0
    try:
        for name, code in BROKEN.items():
            script = root / "broken.py"
            script.write_text(f"import sys\nfile = sys.argv[1]\n{code}\n")
            print(f"-- selftest: {name} (must be rejected)")
            if run_suite([sys.executable, str(script)]) == 0:
                print(f"SELFTEST BAD: {name} was accepted")
                bad += 1
    finally:
        shutil.rmtree(root, ignore_errors=True)
    print(f"selftest: {len(BROKEN) - bad} rejected as expected, {bad} wrongly accepted")
    return 1 if bad else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--impl", help="the command prefix of the implementation; the drop-in path is appended")
    ap.add_argument("--only", help="run only cases whose name contains this text")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    if not args.impl:
        ap.error("give --impl or --selftest")
    return run_suite(shlex.split(args.impl), args.only)


if __name__ == "__main__":
    sys.exit(main())
