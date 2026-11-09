"""Test support: a scratch machine (home, config, units, fake launchctl/systemctl/tailscale/omp) and local servers.

Everything lives under one temporary directory; nothing touches the real home, the real service manager or the
network. The fake service manager binaries are small Python scripts that keep their state in plain files, so a test
can read what was asked of them and make a service look stopped.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import socket
import ssl
import stat
import subprocess
import sys
import tempfile
import textwrap
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

PLUGIN = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PLUGIN / "python"))

from omp_services import cli

LAUNCHCTL = """#!{py}
import pathlib, sys
d = pathlib.Path({dir!r}); (d / "loaded").mkdir(exist_ok=True)
args = sys.argv[1:]
with open(d / "calls.log", "a") as f:
    f.write(" ".join(args) + "\\n")
cmd = args[0]
if (d / "sleep-calls").exists():
    import time
    time.sleep(float((d / "sleep-calls").read_text()))
if cmd == "print":
    spec = args[1]
    if spec.count("/") == 1:
        sys.exit(0)
    state = d / "loaded" / spec.split("/")[-1]
    if (d / "manager-down").exists():
        print("Could not talk to the service manager", file=sys.stderr)
        sys.exit(1)
    if not state.exists():
        print('Could not find service "' + spec.split("/")[-1] + '" in domain for user: 501', file=sys.stderr)
        sys.exit(113)
    print("{{\\n\\tstate = " + (state.read_text().strip() or "running") + "\\n}}")
elif cmd == "bootstrap":
    if (d / "fail-bootstrap").exists():
        print("Bootstrap failed: 5: Input/output error", file=sys.stderr)
        sys.exit(5)
    text = pathlib.Path(args[2]).read_text()
    label = text.split("<key>Label</key>")[1].split("<string>")[1].split("</string>")[0]
    (d / "loaded" / label).write_text("running")
elif cmd == "bootout":
    p = d / "loaded" / args[1].split("/")[-1]
    if p.exists():
        p.unlink()
elif cmd == "kickstart":
    if (d / "fail-kickstart").exists():
        sys.exit(1)
    with open(d / "restarts.log", "a") as f:
        f.write(args[-1].split("/")[-1] + "\\n")
    p = d / "loaded" / args[-1].split("/")[-1]
    if not p.exists():
        sys.exit(113)
    p.write_text("running")
"""

SYSTEMCTL = """#!{py}
import pathlib, sys
d = pathlib.Path({dir!r}); (d / "loaded").mkdir(exist_ok=True)
args = sys.argv[1:]
with open(d / "calls.log", "a") as f:
    f.write(" ".join(args) + "\\n")
assert args[0] == "--user", args
args = args[1:]
cmd = args[0]
if (d / "sleep-calls").exists():
    import time
    time.sleep(float((d / "sleep-calls").read_text()))
unit = args[-1].removesuffix(".service") if len(args) > 1 else ""
state = d / "loaded" / unit
if cmd == "show-environment" or cmd == "daemon-reload":
    sys.exit(0)
word = state.read_text().strip() if state.exists() else "absent"
if cmd in ("is-enabled", "is-active") and (d / "manager-down").exists():
    print("Failed to connect to bus: No medium found", file=sys.stderr)
    sys.exit(1)
if cmd == "is-enabled":
    if not state.exists():
        print("Failed to get unit file state for " + unit + ".service: No such file or directory", file=sys.stderr)
        sys.exit(1)
    enabled = not word.endswith("-disabled")
    print("enabled" if enabled else "disabled")
    sys.exit(0 if enabled else 1)
if cmd == "is-active":
    answer = {{
        "running": "active", "running-disabled": "active",
        "reloading": "reloading", "reloading-disabled": "reloading",
        "activating": "activating", "activating-disabled": "activating",
        "deactivating": "deactivating", "deactivating-disabled": "deactivating",
    }}.get(word, "inactive")
    print(answer)
    sys.exit(0 if answer == "active" else 3)
if cmd == "enable":
    if "--now" in args or word in ("running", "running-disabled"):
        state.write_text("running")
    else:
        state.write_text("enabled-stopped")
elif cmd == "disable":
    if "--now" not in args and word == "running":
        state.write_text("running-disabled")
    elif state.exists():
        state.unlink()
elif cmd == "start":
    if word in ("enabled-stopped", "inactive"):
        state.write_text("running")
    elif not state.exists():
        state.write_text("running-disabled")
elif cmd == "restart":
    with open(d / "restarts.log", "a") as f:
        f.write(unit + "\\n")
    state.write_text("running")
elif cmd == "stop":
    if (d / "fail-stop").exists():
        sys.exit(1)
    if word == "running":
        state.write_text("enabled-stopped")
    elif word == "running-disabled":
        state.unlink()
"""

LOGINCTL = """#!{py}
print("no")
"""

TAILSCALE = """#!{py}
import pathlib, sys
d = pathlib.Path({dir!r})
rc = int((d / "tailscale.rc").read_text()) if (d / "tailscale.rc").exists() else 0
if (d / "tailscale.json").exists():
    sys.stdout.write((d / "tailscale.json").read_text())
sys.exit(rc)
"""

OMP = """#!{py}
import json, os, pathlib, subprocess, sys
d = pathlib.Path({dir!r})
args = sys.argv[1:]
with open(d / "omp-calls.log", "a") as f:
    f.write(" ".join(args) + " BUN_BE_BUN=" + os.environ.get("BUN_BE_BUN", "") + "\\n")
state_file = d / "omp-plugins.json"
state = json.loads(state_file.read_text()) if state_file.exists() else {{"installed": [], "catalogs": {{}}}}
if args == ["--version"]:
    print("omp/" + ((d / "omp.version").read_text().strip() if (d / "omp.version").exists() else "18.18.0"))
elif args[:2] == ["plugin", "list"]:
    print(json.dumps({{"npm": [], "marketplace": [
        {{"id": p["id"], "scope": "user", "entries": [{{"scope": "user", "installPath": p["installPath"], "version": p["version"]}}]}}
        for p in state["installed"]]}}))
elif args[:3] == ["plugin", "marketplace", "update"]:
    new = state.get("refresh", {{}}).get(args[3])
    if new:
        pathlib.Path(new["file"]).write_text(json.dumps({{"name": args[3], "plugins": [{{"name": n, "version": v}} for n, v in new["plugins"].items()]}}))
elif args[:3] == ["plugin", "marketplace", "add"]:
    pass
elif args[:2] == ["plugin", "install"]:
    plan = state.get("installs", {{}}).get(args[2])
    if not plan:
        sys.exit(1)
    state["installed"].append({{"id": args[2], "version": plan["version"], "installPath": plan["installPath"]}})
    state_file.write_text(json.dumps(state))
else:
    sys.exit(2)
"""


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def closed_port() -> int:
    """A port that nothing listens on (it was free a moment ago)."""
    return free_port()


class Scratch:
    """A throwaway machine. `kind` picks the platform: 'launchd' (darwin) or 'systemd' (linux)."""

    def __init__(self, kind: str = "launchd", spaces: bool = False):
        self.kind = kind
        self.root = Path(tempfile.mkdtemp(prefix="osvt")).resolve()
        self.home = self.root / ("my home" if spaces else "home")
        self.fake = self.root / "fake"
        self.bin = self.root / "bin"
        self.units = self.root / "units"
        for d in (self.home, self.fake, self.bin, self.units):
            d.mkdir(parents=True)
        py = sys.executable
        self._script("launchctl", LAUNCHCTL.format(py=py, dir=str(self.fake)))
        self._script("systemctl", SYSTEMCTL.format(py=py, dir=str(self.fake)))
        self._script("loginctl", LOGINCTL.format(py=py))
        self._script("tailscale", TAILSCALE.format(py=py, dir=str(self.fake)))
        self.omp = self._script("omp", OMP.format(py=py, dir=str(self.fake)))
        self.env = {
            "HOME": str(self.home),
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "OMP_SERVICES_PLATFORM": "darwin" if kind == "launchd" else "linux",
            "OMP_SERVICES_UNIT_DIR": str(self.units),
            "OMP_SERVICES_LAUNCHCTL": str(self.bin / "launchctl"),
            "OMP_SERVICES_SYSTEMCTL": str(self.bin / "systemctl"),
            "OMP_SERVICES_LOGINCTL": str(self.bin / "loginctl"),
            "OMP_SERVICES_TAILSCALE": str(self.bin / "tailscale"),
            "OMP_BIN": str(self.omp),
            "OMP_SERVICES_LABEL_PREFIX": "osvtest",
        }

    # -- construction helpers -------------------------------------------------------------------------------------

    def _script(self, name: str, text: str) -> Path:
        path = self.bin / name
        path.write_text(text)
        path.chmod(0o755)
        return path

    def cleanup(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    # -- directories the tool uses ---------------------------------------------------------------------------------

    @property
    def config_dir(self) -> Path:
        return self.home / ".config" / "omp-services"

    @property
    def data_dir(self) -> Path:
        return self.home / ".local" / "share" / "omp-services"

    @property
    def state_dir(self) -> Path:
        return self.home / ".local" / "state" / "omp-services"

    # -- fake service manager --------------------------------------------------------------------------------------

    def loaded(self) -> set[str]:
        d = self.fake / "loaded"
        return {p.name for p in d.iterdir()} if d.exists() else set()

    def set_state(self, label: str, state: str) -> None:
        (self.fake / "loaded").mkdir(exist_ok=True)
        (self.fake / "loaded" / label).write_text(state)

    def calls(self) -> list[str]:
        f = self.fake / "calls.log"
        return f.read_text().splitlines() if f.exists() else []

    def restarts(self) -> list[str]:
        f = self.fake / "restarts.log"
        return f.read_text().splitlines() if f.exists() else []

    def clear_calls(self) -> None:
        for name in ("calls.log", "restarts.log"):
            (self.fake / name).unlink(missing_ok=True)

    def set_tailscale(self, serve_status: dict | str, rc: int = 0) -> None:
        (self.fake / "tailscale.json").write_text(
            serve_status if isinstance(serve_status, str) else json.dumps(serve_status)
        )
        (self.fake / "tailscale.rc").write_text(str(rc))

    # -- fake omp plugin state -------------------------------------------------------------------------------------

    def _omp_state(self) -> dict:
        f = self.fake / "omp-plugins.json"
        return json.loads(f.read_text()) if f.exists() else {"installed": [], "catalogs": {}, "refresh": {}}

    def _save_omp_state(self, state: dict) -> None:
        (self.fake / "omp-plugins.json").write_text(json.dumps(state))

    def plugin_tree(self, plugin: str, version: str) -> Path:
        """A copy of this plugin's code (python, bin) as omp's cache would hold it, with `version` patched in."""
        dst = self.root / "omp" / "cache" / "plugins" / f"omp-tether___{plugin}___{version}"
        if not dst.exists():
            dst.mkdir(parents=True)
            for name in ("python", "bin"):
                shutil.copytree(PLUGIN / name, dst / name, ignore=shutil.ignore_patterns("__pycache__"))
            init = dst / "python" / "omp_services" / "__init__.py"
            init.write_text(init.read_text().replace('__version__ = "0.1.0"', f'__version__ = "{version}"'))
        return dst

    def omp_install(self, plugin: str, version: str, market: str = "omp-tether", tree: Path | None = None) -> Path:
        path = tree or self.root / "omp" / "cache" / "plugins" / f"{market}___{plugin}___{version}"
        path.mkdir(parents=True, exist_ok=True)
        state = self._omp_state()
        state["installed"] = [p for p in state["installed"] if p["id"] != f"{plugin}@{market}"]
        state["installed"].append({"id": f"{plugin}@{market}", "version": version, "installPath": str(path)})
        self._save_omp_state(state)
        return path

    def omp_catalog(self, versions: dict[str, str], market: str = "omp-tether") -> Path:
        directory = self.root / "omp" / "cache" / "marketplaces" / market
        directory.mkdir(parents=True, exist_ok=True)
        file = directory / "marketplace.json"
        file.write_text(
            json.dumps({"name": market, "plugins": [{"name": n, "version": v} for n, v in versions.items()]})
        )
        return file

    def omp_will_install(self, plugin: str, version: str, tree: Path, market: str = "omp-tether") -> None:
        state = self._omp_state()
        state.setdefault("installs", {})[f"{plugin}@{market}"] = {"version": version, "installPath": str(tree)}
        self._save_omp_state(state)

    def omp_calls(self) -> list[str]:
        f = self.fake / "omp-calls.log"
        return f.read_text().splitlines() if f.exists() else []

    # -- drop-ins and config ---------------------------------------------------------------------------------------

    def write_config(self, **sections) -> None:
        self.config_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.config_dir, 0o700)
        values = {"schemaVersion": 1, **sections}
        (self.config_dir / "config.json").write_text(json.dumps(values))
        os.chmod(self.config_dir / "config.json", 0o600)

    def dropin(self, kind: str, plugin: str, body: dict | str, mode: int = 0o600) -> Path:
        directory = self.config_dir / ("health.d" if kind == "health" else "services.d")
        directory.mkdir(parents=True, exist_ok=True)
        os.chmod(self.config_dir, 0o700)
        os.chmod(directory, 0o700)
        path = directory / f"{plugin}.json"
        path.write_text(body if isinstance(body, str) else json.dumps(body))
        os.chmod(path, mode)
        return path

    def health(self, plugin: str, *checks: dict, mode: int = 0o600) -> Path:
        return self.dropin("health", plugin, {"protocol": "health/1", "plugin": plugin, "checks": list(checks)}, mode)

    def services(self, plugin: str, *services: dict, mode: int = 0o600) -> Path:
        return self.dropin(
            "services", plugin, {"protocol": "services/1", "plugin": plugin, "services": list(services)}, mode
        )

    # -- running the CLI in this process ---------------------------------------------------------------------------

    @contextlib.contextmanager
    def patched(self, extra: dict[str, str] | None = None):
        with mock.patch.dict(os.environ, {**self.env, **(extra or {})}, clear=True):
            yield

    def cli(self, *args: str, extra_env: dict[str, str] | None = None, answers: list[str] | None = None):
        """Run `omp-services <args>` in this process. Returns (exit status, stdout, stderr)."""
        out, err = io.StringIO(), io.StringIO()
        asked: list[str] = []
        queue = list(answers or [])

        def fake_ask(prompt: str):
            asked.append(prompt)
            return queue.pop(0) if queue else None

        with (
            self.patched(extra_env),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
            mock.patch("omp_services.console.ask", fake_ask),
        ):
            try:
                rc = cli.main(list(args))
            except SystemExit as exc:
                rc = int(exc.code or 0)
        self.asked = asked
        return rc, out.getvalue(), err.getvalue()

    def run_process(
        self, argv: list[str], extra_env: dict[str, str] | None = None, **kw
    ) -> subprocess.CompletedProcess:
        """Run a real child process with the scratch environment and no controlling terminal."""
        return subprocess.run(
            argv,
            env={**self.env, **(extra_env or {})},
            capture_output=True,
            text=True,
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            timeout=kw.pop("timeout", 120),
            **kw,
        )


class Case(unittest.TestCase):
    kind = "launchd"
    spaces = False

    def setUp(self) -> None:
        self.s = Scratch(self.kind, self.spaces)
        self.addCleanup(self.s.cleanup)


# ---------------------------------------------------------------------------------------------------------------------
# A local HTTP(S) server
# ---------------------------------------------------------------------------------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args) -> None:
        pass

    def _serve(self) -> None:
        srv: Server = self.server.owner  # type: ignore[attr-defined]
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        srv.requests.append({"method": self.command, "path": self.path, "body": body, "headers": dict(self.headers)})
        status, payload, headers, delay = srv.routes.get(self.path.split("?")[0], srv.default)
        if delay:
            threading.Event().wait(delay)
        if callable(payload):
            payload = payload()
        data = payload if isinstance(payload, bytes) else str(payload).encode()
        try:
            self.send_response(status)
            for k, v in headers.items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(data)
        except OSError:
            pass

    do_GET = do_POST = do_HEAD = _serve


class Server:
    """Routes: path -> (status, body, headers, delay seconds). Records every request it receives."""

    def __init__(self, cert: tuple[str, str] | None = None):
        self.routes: dict[str, tuple] = {}
        self.default: tuple = (200, "ok", {}, 0)
        self.requests: list[dict] = []
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.httpd.owner = self  # type: ignore[attr-defined]
        self.httpd.daemon_threads = True
        if cert:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(*cert)
            self.httpd.socket = ctx.wrap_socket(self.httpd.socket, server_side=True)
        self.port = self.httpd.server_address[1]
        self.scheme = "https" if cert else "http"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def route(self, path: str, status: int = 200, body="ok", headers: dict | None = None, delay: float = 0) -> None:
        self.routes[path] = (status, body, headers or {}, delay)

    @property
    def base(self) -> str:
        return f"{self.scheme}://127.0.0.1:{self.port}"

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def make_certs(directory: Path) -> tuple[str, str, str] | None:
    """(ca file, cert file, key file) for 127.0.0.1, or None when the openssl command is not available.

    The CA carries the extensions that strict verification (the default since Python 3.13) insists on."""
    openssl = shutil.which("openssl")
    if not openssl:
        return None
    ca_key, ca_crt, key, csr, crt = (directory / n for n in ("ca.key", "ca.crt", "srv.key", "srv.csr", "srv.crt"))
    ca_cnf, ext = directory / "ca.cnf", directory / "ext.cnf"
    ca_cnf.write_text(
        "[req]\ndistinguished_name=dn\nx509_extensions=v3_ca\nprompt=no\n[dn]\nCN=osv test ca\n"
        "[v3_ca]\nbasicConstraints=critical,CA:TRUE\nkeyUsage=critical,keyCertSign,cRLSign\nsubjectKeyIdentifier=hash\n"
    )
    ext.write_text(
        "subjectAltName=IP:127.0.0.1\nbasicConstraints=critical,CA:FALSE\n"
        "keyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\n"
        "authorityKeyIdentifier=keyid\nsubjectKeyIdentifier=hash\n"
    )
    steps = [
        [
            openssl,
            "req",
            "-x509",
            "-config",
            str(ca_cnf),
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(ca_key),
            "-out",
            str(ca_crt),
            "-days",
            "2",
        ],
        [
            openssl,
            "req",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(csr),
            "-subj",
            "/CN=127.0.0.1",
        ],
        [
            openssl,
            "x509",
            "-req",
            "-in",
            str(csr),
            "-CA",
            str(ca_crt),
            "-CAkey",
            str(ca_key),
            "-CAcreateserial",
            "-out",
            str(crt),
            "-days",
            "2",
            "-extfile",
            str(ext),
        ],
    ]
    for argv in steps:
        if subprocess.run(argv, capture_output=True, cwd=directory).returncode != 0:
            return None
    return str(ca_crt), str(crt), str(key)


def mode_of(path: Path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


def dedent(text: str) -> str:
    return textwrap.dedent(text).lstrip("\n")
