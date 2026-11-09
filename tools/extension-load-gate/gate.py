#!/usr/bin/env python3
"""Headless extension-load gate: every extension plugin in the catalog must install, load and uninstall cleanly.

Why it exists: omp's marketplace install path does not validate extensions. A plugin whose extension throws on load
installs with exit code 0, stays installed, and fails again in every later session. So CI loads each plugin for real.

For every catalog entry with a relative source (and, with --plugin, only those) the gate, in a scratch HOME:
  1. checks that every extension file named in package.json `omp.extensions` exists and is not called index.*
     (omp derives the extension id from the file name; index.* would take the folder name and break self-disable);
  2. adds the repository as a local marketplace and installs the plugin;
  3. runs a headless session with the fake provider (`omp -p`): exit code 0, the fake reply arrives, and nothing says
     "Failed to load extension";
  4. starts `omp --mode rpc` and asks for the available commands: the extension must show up with every command listed
     for it in expectations.json (the positive proof that it loaded, not just that nothing complained);
  5. uninstalls it and checks that the plugin directory entry, the lockfile entry and the command are gone.
A plugin that ships no extension (a service, a command line) is declared with {"extensions": false} in
expectations.json: it must then declare no omp.extensions and ship executable `bin` files, steps 3 and 5 still apply,
and step 4 only checks that a session starts without a load failure.
No network, no model cost, nothing outside the scratch directory. `--selftest` proves the gate itself catches a
throwing extension, a missing extension file and an extension that does not register what it should.

Usage: gate.py [--repo DIR] [--plugin NAME ...] [--expectations FILE] [--selftest]
Environment: OMP_BIN (default: omp on PATH). Exit codes: 0 all plugins pass, 1 a plugin fails, 2 usage or setup error.
Standard library only; Python 3.10 or newer.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import selectors
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "testkit"))

import omp_scratch as kit

LOAD_FAILURE = "Failed to load extension"
# Commands omp itself always offers: an RPC answer without them is not a real answer.
BASELINE_COMMANDS = ["session"]


class GateError(Exception):
    """A plugin failed the gate; the message says why."""


def catalog(repo: Path) -> dict:
    return json.loads((repo / ".omp-plugin" / "marketplace.json").read_text())


def local_plugins(repo: Path, names: list[str] | None) -> list[tuple[str, Path]]:
    found = []
    for entry in catalog(repo)["plugins"]:
        source = entry["source"]
        if not isinstance(source, str):
            continue  # git sources are fetched, not part of this tree
        if names and entry["name"] not in names:
            continue
        found.append((entry["name"], (repo / source).resolve()))
    return found


def check_manifest(plugin_dir: Path) -> list[str]:
    """Extension files declared in package.json; raises GateError when one is missing or called index."""
    pkg = json.loads((plugin_dir / "package.json").read_text())
    files = (pkg.get("omp") or {}).get("extensions") or []
    if not files:
        raise GateError("package.json declares no omp.extensions")
    for rel in files:
        path = plugin_dir / rel
        if not path.is_file():
            raise GateError(f"extension file {rel} does not exist")
        if path.stem == "index":
            raise GateError(
                f"extension file {rel} is called index: its id would be the folder name, name it after the plugin"
            )
    return files


def check_plain_manifest(plugin_dir: Path) -> list[str]:
    """A plugin declared as shipping no extension declares none, and every `bin` file it names is executable."""
    pkg = json.loads((plugin_dir / "package.json").read_text())
    if (pkg.get("omp") or {}).get("extensions"):
        raise GateError("expectations.json says this plugin has no extension, but package.json declares omp.extensions")
    bins = pkg.get("bin") or {}
    if isinstance(bins, str):
        bins = {pkg.get("name", "bin"): bins}
    if not bins:
        raise GateError("a plugin without an extension must at least ship a command (package.json `bin`)")
    for rel in bins.values():
        path = plugin_dir / rel
        if not path.is_file():
            raise GateError(f"bin file {rel} does not exist")
        if not os.access(path, os.X_OK):
            raise GateError(f"bin file {rel} is not executable")
    return [f"bin {rel}" for rel in bins.values()]


def headless_run(home: kit.ScratchHome, timeout: float = 180) -> str:
    r = home.cli("-e", str(kit.FAKE_PROVIDER), "--model", kit.MODEL, "-p", "hello", timeout=timeout)
    out = r.stdout + r.stderr
    if r.returncode != 0:
        raise GateError(f"headless run exited {r.returncode}:\n{out.strip()[-800:]}")
    if LOAD_FAILURE in out:
        raise GateError(f"headless run reports a load failure:\n{out.strip()[-800:]}")
    if "test reply" not in out:
        raise GateError(f"headless run produced no model reply:\n{out.strip()[-800:]}")
    return out


@dataclass
class RpcResult:
    """What one headless RPC session said. `ok` means omp answered the command query with a success frame."""

    ok: bool
    commands: dict[str, str]
    text: str
    reason: str = ""


def rpc_timeout() -> float:
    try:
        return float(os.environ.get("GATE_RPC_TIMEOUT", "60"))
    except ValueError:
        return 60.0


def rpc_commands(home: kit.ScratchHome, timeout: float | None = None) -> RpcResult:
    """Starts `omp --mode rpc`, asks for the available commands and waits at most `timeout` seconds in total.

    Both pipes are read without blocking (a full stderr pipe cannot stall omp, and a line that never ends cannot stall
    us); on every exit path the process is terminated and reaped.
    """
    timeout = rpc_timeout() if timeout is None else timeout
    omp = kit.find_omp()
    assert omp
    proc = subprocess.Popen(
        [omp, "--mode", "rpc", "-e", str(kit.FAKE_PROVIDER), "--model", kit.MODEL],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=home.env,
        cwd=home.repo,
    )
    assert proc.stdin and proc.stdout and proc.stderr
    out_tail, err_tail, partial = "", "", b""
    frame: dict | None = None
    reason = ""
    sel = selectors.DefaultSelector()
    try:
        for stream in (proc.stdout, proc.stderr):
            os.set_blocking(stream.fileno(), False)
            sel.register(stream, selectors.EVENT_READ)
        try:
            proc.stdin.write((json.dumps({"id": "gate", "type": "get_available_commands"}) + "\n").encode())
            proc.stdin.flush()
        except OSError:
            reason = "omp closed its input before the query was sent"
        deadline = time.monotonic() + timeout
        while frame is None and sel.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                reason = f"no answer within {timeout:g} s"
                break
            for key, _ in sel.select(timeout=min(0.2, remaining)):
                chunk = os.read(key.fd, 65536)
                if not chunk:
                    sel.unregister(key.fileobj)
                    continue
                if key.fileobj is proc.stderr:
                    err_tail = (err_tail + chunk.decode("utf-8", "replace"))[-20000:]
                    continue
                out_tail = (out_tail + chunk.decode("utf-8", "replace"))[-20000:]
                partial = (partial + chunk)[-1_000_000:]
                *lines, partial = partial.split(b"\n")
                for line in lines:
                    try:
                        candidate = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(candidate, dict) and candidate.get("id") == "gate":
                        frame = candidate
        if frame is None and not reason:
            reason = "omp ended the session without answering"
    finally:
        sel.close()
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            with contextlib.suppress(OSError):
                stream.close()
    text = out_tail + err_tail
    if frame is None:
        return RpcResult(False, {}, text, reason)
    if not frame.get("success"):
        return RpcResult(False, {}, text, "omp answered with a failure")
    try:
        commands = {c["name"]: c.get("source", "") for c in frame["data"]["commands"]}
    except (KeyError, TypeError):
        return RpcResult(False, {}, text, "omp answered with an unexpected shape")
    return RpcResult(True, commands, text)


def gate_one(repo: Path, name: str, plugin_dir: Path, expectation: dict | None, marketplace: str) -> list[str]:
    """Runs every step for one plugin. Returns human-readable evidence lines; raises GateError on failure.

    A plugin that ships no extension (a service, a command line) is declared with {"extensions": false} in
    expectations.json: it must then really declare none, its `bin` files must exist, and the gate still proves that it
    installs, that a session starts without a load failure, and that uninstalling leaves nothing behind.
    """
    evidence = []
    has_extension = (expectation or {}).get("extensions", True) is not False
    expected_commands = (expectation or {}).get("commands") if has_extension else []
    files = check_manifest(plugin_dir) if has_extension else check_plain_manifest(plugin_dir)
    evidence.append(f"manifest ok: {', '.join(files)}")
    if expected_commands is None:
        raise GateError(f"no entry for {name} in expectations.json: add the commands its extension registers")
    home = kit.ScratchHome(prefix="olg")
    try:
        try:
            home.install_plugin(repo, name, marketplace)
        except RuntimeError as exc:
            raise GateError(str(exc)) from exc
        evidence.append("installed from the local marketplace")
        headless_run(home)
        evidence.append("headless run: exit 0, model reply received, no load failure")
        rpc = rpc_commands(home)
        if LOAD_FAILURE in rpc.text:
            raise GateError(f"rpc session reports a load failure:\n{rpc.text.strip()[-800:]}")
        if not rpc.ok:
            raise GateError(
                f"rpc session gave no valid answer to get_available_commands ({rpc.reason}):\n{rpc.text.strip()[-800:]}"
            )
        if has_extension:
            missing = [c for c in expected_commands if rpc.commands.get(c) != "extension"]
            if missing:
                raise GateError(f"extension did not register command(s) {missing}: it did not load")
            evidence.append(f"loaded: command(s) {expected_commands} registered by an extension")
        else:
            evidence.append(
                "no extension to load (declared so in expectations.json); no load failure in a headless session"
            )
        r = home.cli("plugin", "uninstall", f"{name}@{marketplace}")
        if r.returncode != 0:
            raise GateError(f"uninstall failed:\n{(r.stdout + r.stderr).strip()[-800:]}")
        lock = (home.home / ".omp" / "plugins" / "omp-plugins.lock.json").read_text()
        installed = (home.home / ".omp" / "plugins" / "installed_plugins.json").read_text()
        if f'"{name}"' in lock or name in installed:
            raise GateError("uninstall left an entry in the plugin lockfile or installed_plugins.json")
        if (home.home / ".omp" / "plugins" / "node_modules" / name).exists():
            raise GateError("uninstall left the plugin directory link")
        after = rpc_commands(home)
        if not after.ok:
            raise GateError(f"rpc session after uninstall gave no valid answer ({after.reason}); removal is not proven")
        missing_baseline = [c for c in BASELINE_COMMANDS if after.commands.get(c) != "builtin"]
        if missing_baseline:
            raise GateError(
                f"rpc session after uninstall lacks omp's own command(s) {missing_baseline}; its answer is not trusted"
            )
        if any(after.commands.get(c) == "extension" for c in expected_commands):
            raise GateError("the command is still registered after uninstall")
        evidence.append("uninstalled cleanly: lockfile, link and command are gone")
    finally:
        home.cleanup()
    return evidence


def run_gate(repo: Path, names: list[str] | None, expectations: dict) -> int:
    plugins = local_plugins(repo, names)
    if not plugins:
        print("error: no plugin with a relative source selected", file=sys.stderr)
        return 2
    marketplace = catalog(repo)["name"]
    failed = 0
    for name, plugin_dir in plugins:
        try:
            evidence = gate_one(repo, name, plugin_dir, expectations.get(name), marketplace)
        except GateError as exc:
            failed += 1
            print(f"FAIL {name}: {exc}")
            continue
        except (subprocess.TimeoutExpired, OSError) as exc:
            failed += 1
            print(f"FAIL {name}: a step did not finish ({type(exc).__name__}); the gate fails closed")
            continue
        print(f"PASS {name}")
        for line in evidence:
            print(f"     {line}")
    print(f"{len(plugins) - failed} passed, {failed} failed")
    return 1 if failed else 0


def selftest() -> int:
    """Builds a scratch catalog with three broken plugins and one good one; the gate must reject exactly the broken."""
    root = Path(tempfile.mkdtemp(prefix="olgst", dir="/tmp"))
    try:
        plugins = {
            "st-good": (
                "good.ts",
                "export default function (pi: any) {\n"
                '  pi.registerCommand("st-good-cmd", { handler: async () => {} });\n'
                "}\n",
                ["st-good-cmd"],
            ),
            "st-throws": (
                "throws.ts",
                'export default function () { throw new Error("deliberate load failure"); }\n',
                ["st-throws-cmd"],
            ),
            "st-silent": ("silent.ts", "export default function () {}\n", ["st-silent-cmd"]),
            "st-missing": ("missing.ts", None, ["st-missing-cmd"]),
        }
        entries = []
        for name, (file, body, _) in plugins.items():
            d = root / "plugins" / name
            d.mkdir(parents=True)
            if body is not None:
                (d / file).write_text(body)
            (d / "package.json").write_text(
                json.dumps({"name": name, "version": "0.0.1", "type": "module", "omp": {"extensions": [f"./{file}"]}})
            )
            entries.append({"name": name, "source": f"./plugins/{name}", "version": "0.0.1"})
        # Plugins that ship no extension (declared so in the expectations): one honest, three that must be rejected.
        plain = {
            "st-plain-good": ({"bin": {"tool": "./bin/tool"}}, "bin/tool", 0o755, True),
            "st-plain-lies": (
                {"bin": {"tool": "./bin/tool"}, "omp": {"extensions": ["./x.ts"]}},
                "bin/tool",
                0o755,
                False,
            ),
            "st-plain-nobin": ({}, None, 0o755, False),
            "st-plain-notexec": ({"bin": {"tool": "./bin/tool"}}, "bin/tool", 0o644, False),
        }
        for name, (extra, binfile, mode, _) in plain.items():
            d = root / "plugins" / name
            d.mkdir(parents=True)
            if binfile:
                (d / binfile).parent.mkdir(parents=True)
                (d / binfile).write_text("#!/bin/sh\nexit 0\n")
                (d / binfile).chmod(mode)
            (d / "package.json").write_text(json.dumps({"name": name, "version": "0.0.1", "type": "module", **extra}))
            entries.append({"name": name, "source": f"./plugins/{name}", "version": "0.0.1"})
        (root / ".omp-plugin").mkdir()
        (root / ".omp-plugin" / "marketplace.json").write_text(
            json.dumps({"name": "gate-selftest", "owner": {"name": "selftest"}, "plugins": entries})
        )
        expectations = {n: {"commands": c} for n, (_, _, c) in plugins.items()}
        expectations.update({n: {"extensions": False} for n in plain})
        wanted = {n: n == "st-good" for n in plugins} | {n: ok for n, (_, _, _, ok) in plain.items()}
        bad = 0
        for name, passes in wanted.items():
            print(f"-- selftest: {name} (expected {'PASS' if passes else 'FAIL'})")
            code = run_gate(root, [name], expectations)
            if code != (0 if passes else 1):
                print(f"SELFTEST BAD: {name} gave exit {code}")
                bad += 1
        print(f"selftest: {len(wanted) - bad} as expected, {bad} unexpected")
        return 1 if bad else 0
    finally:
        import shutil

        shutil.rmtree(root, ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", type=Path, default=HERE.parent.parent, help="repository root (default: this repository)")
    ap.add_argument("--plugin", action="append", help="gate only this plugin (repeatable)")
    ap.add_argument("--expectations", type=Path, default=HERE / "expectations.json")
    ap.add_argument("--selftest", action="store_true", help="check that the gate itself rejects broken plugins")
    args = ap.parse_args()
    if not kit.find_omp():
        print("error: omp not found (set OMP_BIN or put omp on PATH)", file=sys.stderr)
        return 2
    if args.selftest:
        return selftest()
    try:
        expectations = json.loads(args.expectations.read_text())
    except (OSError, ValueError) as exc:
        print(f"error: cannot read {args.expectations}: {exc}", file=sys.stderr)
        return 2
    return run_gate(args.repo.resolve(), args.plugin, expectations)


if __name__ == "__main__":
    sys.exit(main())
