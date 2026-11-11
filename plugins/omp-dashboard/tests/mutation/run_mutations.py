#!/usr/bin/env python3
"""Mutation check of the security guards: each mutation switches one guard off in a COPY of the plugin and runs the test module that is
supposed to notice. The check passes only when every copy fails its tests (and the unmutated copy passes). Nothing in the working tree changes.

    python3 run_mutations.py [--only NAME]
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[2]
PKG = "python/omp_dashboard"

# name, file, exact text to replace (first occurrence), replacement, test module
MUTATIONS = [
    (
        "origin-check",
        "server.py",
        'if self.headers.get("Origin") not in self.app.adapter.origins(self.headers):',
        "if False:",
        "test_server",
    ),
    ("custom-header", "server.py", 'if self.headers.get("X-Omp-Dashboard") != "1":', "if False:", "test_server"),
    (
        "sign-in-adapter",
        "server.py",
        "denied: Denied | None = self.app.adapter.check(self.headers)",
        "denied: Denied | None = None",
        "test_server",
    ),
    (
        "cookie-check",
        "server.py",
        'return self.app.pairing.paired_device(self.headers.get("Cookie"), access)',
        'return "deadbeef"',
        "test_server",
    ),
    ("header-budget", "server.py", "raise HeaderBudgetExceeded", "return data", "test_server"),
    ("ui-hash", "ui.py", "if got != want:", "if False:", "test_server"),
    (
        "control-registry-private",
        "control.py",
        "if not _private(st, self.uid, stat.S_IFDIR):",
        "if False:",
        "test_control",
    ),
    ("control-socket-in-dir", "control.py", "if sock.parent != self.dir or", "if False or", "test_control"),
    ("control-version", "control.py", "version is None\n", "False\n", "test_control"),
    (
        "relay-proof-compare",
        "relay.py",
        'if not hmac.compare_digest(mac, want):\n        raise RelayCheckError(f"the listener on port {port} is not our relay; links stay closed")',
        'if False:\n        raise RelayCheckError(f"the listener on port {port} is not our relay; links stay closed")',
        "test_relay",
    ),
    (
        "relay-owner",
        "relay.py",
        "if uids != {uid}:\n            raise RelayCheckError(\n",
        "if False:\n            raise RelayCheckError(\n",
        "test_relay",
    ),
    ("auth-funnel", "auth.py", 'if headers.get("Tailscale-Funnel-Request") is not None:', "if False:", "test_auth"),
    ("auth-owner", "auth.py", "if not login or login != owner or", "if not login or False or", "test_auth"),
    ("auth-bind", "auth.py", "if not loopback_address(host):", "if False:", "test_auth"),
    ("alerts-3xx-is-an-error", "alerts.py", "if not 200 <= response.status < 300:", "if False:", "test_alerts_audit"),
    (
        "projects-cap",
        "projects.py",
        "if len(found) < MAX_PROJECTS and os.path.lexists",
        "if os.path.lexists",
        "test_start",
    ),
    ("kill-owner", "kill.py", "if info.uid != uid:", "if False:", "test_kill"),
    ("kill-command-name", "kill.py", "if Path(info.command).name != COMMAND_NAME:", "if False:", "test_kill"),
    (
        "force-window",
        "kill.py",
        "if failed is None or failed[:2] != (pid, info.start) or self.clock() - failed[2] > FORCE_WINDOW_S:",
        "if False:",
        "test_kill",
    ),
    (
        "trust-others-writable",
        "trustwalk.py",
        "if info.st_mode & 0o002:\n        return True",
        "if False:\n        return True",
        "test_trustwalk",
    ),
    (
        "trust-group-private",
        "trustwalk.py",
        'return sys.platform == "darwin" or not group_is_private(info.st_gid, os.getuid())',
        "return False",
        "test_review1",
    ),
    (
        "acl-unrecognised",
        "trustwalk.py",
        "if current is not None and not match and _ACL_ANY.match(line):",
        "if False:",
        "test_review1",
    ),
    (
        "acl-inherited",
        "trustwalk.py",
        "(\\S+)\\s+(?:inherited\\s+)?(allow|deny)",
        "(\\S+)\\s+(allow|deny)",
        "test_review1",
    ),
    (
        "killpg-only-at-deadline",
        "util.py",
        "        if timed_out and proc.returncode is None:",
        "        if True:",
        "test_review1",
    ),
    ("duplicate-headers", "auth.py", "if len(get_all(name) or []) > 1:", "if False:", "test_review1"),
    (
        "pair-budget-bypass",
        "server.py",
        "if not known and slot is None:",
        "if slot is None:",
        "test_review1",
    ),
    (
        "pairing-leaf-acl",
        "pairing.py",
        "check_path(self.dir / name, secret=True)\n",
        "pass\n",
        "test_review1",
    ),
    (
        "audit-not-ours",
        "audit.py",
        "if not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid():",
        "if False:",
        "test_review1",
    ),
    (
        "kill-descendant-identity",
        "kill.py",
        "if current is None or current.start != start or current.uid != self.uid:",
        "if current is None:",
        "test_review1",
    ),
    (
        "pairing-max-age",
        "pairing.py",
        "if paired is None or self.now() - paired > limit * 86400:",
        "if False:",
        "test_review1",
    ),
    ("content-type", "server.py", 'if content_type != "application/json":', "if False:", "test_review1"),
    (
        "writers-check-ancestors",
        "util.py",
        "    trust_ancestors(path.parent)\n    changed = False",
        "    changed = False",
        "test_review1",
    ),
    (
        "tmux-format-escape",
        "start.py",
        'label_arg, path_arg = label.replace("#", "##"), path.replace("#", "##")',
        "label_arg, path_arg = label, path",
        "test_review1",
    ),
    (
        "control-code-shape",
        "control.py",
        "return value if isinstance(value, str) and _CODE.match(value) else",
        "return value if isinstance(value, str) else",
        "test_review1",
    ),
    ("bidi-format-characters", "util.py", ' or unicodedata.category(c) == "Cf")', ")", "test_review1"),
    ("ntfy-topic-length", "alerts.py", "{16,64}", "{4,64}", "test_review1"),
    ("dot-dot-refused", "config.py", 'and ".." in value.split("/")', 'and "..x" in value.split("/")', "test_review1"),
    ("pairing-verdict-fingerprint", "pairing.py", "and cached[2] == fingerprint:", "and True:", "test_review2"),
    (
        "killpg-needs-an-unreaped-leader",
        "util.py",
        "if timed_out and proc.returncode is None:",
        "if timed_out:",
        "test_review2",
    ),
    ("force-kill-root-binding", "kill.py", "                below = []\n", "                pass\n", "test_review2"),
    (
        "revoke-everything-lock",
        "pairing.py",
        "        with self._locked():\n            devices, pending = self._load(DEVICES_FILE)",
        "        if True:\n            devices, pending = self._load(DEVICES_FILE)",
        "test_review2",
    ),
    (
        "secret-acl-before-write",
        "util.py",
        "                _private_acl(tmp)\n            view = memoryview(data)",
        "                pass\n            view = memoryview(data)",
        "test_review2",
    ),
    (
        "profile-variable-always-refused",
        "dropins.py",
        "(name in ALWAYS or not config.get",
        "(not config.get",
        "test_review2",
    ),
    ("home-dot-dot", "paths.py", 'lexical(env["HOME"], "HOME")', "pass", "test_review2"),
    # the `both` adapter: one rule set per request, chosen by the sign of a proxy
    (
        "both-proxied-judged-by-tailnet-rules",
        "auth.py",
        'return self.tailnet if self.access(headers) == "tailnet" else self.local',
        "return self.local",
        "test_both",
    ),
    (
        "both-direct-judged-by-local-rules",
        "auth.py",
        'return self.tailnet if self.access(headers) == "tailnet" else self.local',
        "return self.tailnet",
        "test_both",
    ),
    (
        "both-access-by-proxy-sign",
        "auth.py",
        'return "tailnet" if proxy_sign(headers) else "local"',
        'return "local"',
        "test_both",
    ),
    (
        "proxy-sign-prefixes",
        "auth.py",
        "if low.startswith(PROXY_PREFIXES) or low in PROXY_NAMES:",
        "if low in PROXY_NAMES:",
        "test_both",
    ),
    (
        "proxy-sign-via",
        "auth.py",
        'PROXY_NAMES = ("forwarded", "via", "x-real-ip")',
        'PROXY_NAMES = ("forwarded", "x-real-ip")',
        "test_both",
    ),
    (
        "both-origins-of-its-own-path",
        "auth.py",
        "return self._by(headers).origins()",
        "return self.local.origins() | self.tailnet.origins()",
        "test_both",
    ),
    (
        "both-secure-cookie-through-serve",
        "auth.py",
        "return self._by(headers).secure_cookie()",
        "return False",
        "test_both",
    ),
    ("both-relay-admits-one-way", "server.py", "if access != admits:", "if False:", "test_both"),
    (
        "both-local-link-only-at-the-machines-own-names",
        "server.py",
        'if not sep or name not in LOOPBACK_NAMES or port != str(self.config.get("listen.port")):',
        "if not sep:",
        "test_both",
    ),
    (
        "both-local-link-keeps-the-browsers-host-name",
        "server.py",
        "return f\"http://{name}:{int(self.config.get('relay.guestPort'))}\"",
        "return f\"http://127.0.0.1:{int(self.config.get('relay.guestPort'))}\"",
        "test_both",
    ),
    (
        "serve-route-elsewhere-fails-the-health-check",
        "doctor.py",
        'if state == "elsewhere":\n        return False,',
        'if state == "elsewhere":\n        return True,',
        "test_both",
    ),
    (
        "serve-funnel-fails-the-health-check",
        "doctor.py",
        "if funnel:\n        return False,",
        "if funnel:\n        return True,",
        "test_both",
    ),
    (
        "pair-asks-tailscale-whether-it-is-up",
        "cli.py",
        'reason = reasons.get(state, f"Tailscale is not running (state: {state})")',
        "reason = None",
        "test_both",
    ),
    ("access-local-is-asked-of-the-sign-in", "doctor.py", "elif status == 401:", "elif True:", "test_both"),
    (
        "healthz-sees-every-proxy-sign",
        "server.py",
        "proxied = proxy_sign(self.headers) is not None or",
        "proxied = False or",
        "test_both",
    ),
    ("both-serve-route-is-a-note", "dropins.py", 'route["severity"] = "note"', "pass", "test_both"),
    ("both-tailscale-off-is-a-warning", "doctor.py", "bad = WARN if soft else FAIL", "bad = FAIL", "test_both"),
    (
        "both-device-is-let-in-only-its-way",
        "pairing.py",
        "if digest in view.hashes and (access is None or (view.access or {}).get(digest) == access):",
        "if digest in view.hashes:",
        "test_both",
    ),
    (
        "both-server-passes-the-way-of-the-request",
        "server.py",
        'access = self.app.adapter.access(self.headers) if self.app.adapter.name == "both" else None',
        "access = None",
        "test_both",
    ),
    (
        "both-pairing-records-the-way",
        "pairing.py",
        '                record["access"] = access\n',
        "                pass\n",
        "test_both",
    ),
    (
        "both-local-device-expires-sooner",
        "pairing.py",
        'if access == "local" and self.local_max_age_days is not None:',
        "if False:",
        "test_both",
    ),
]


def run_tests(root: Path, module: str, timeout: int = 600) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": str(root / "python")}
    return subprocess.run(
        [sys.executable, "-m", "unittest", module],
        cwd=root / "tests" / "python",
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only")
    args = parser.parse_args()
    failures = 0
    for name, file, old, new, module in MUTATIONS:
        if args.only and name != args.only:
            continue
        scratch = Path(tempfile.mkdtemp(prefix="odm", dir="/tmp"))
        try:
            copy = scratch / "plugin"
            shutil.copytree(PLUGIN, copy, ignore=shutil.ignore_patterns("node_modules", "__pycache__"))
            target = copy / PKG / file
            text = target.read_text()
            if old not in text:
                print(f"BROKEN  {name}: the text to mutate is not in {file} any more; update the table")
                failures += 1
                continue
            baseline = run_tests(copy, module)
            if baseline.returncode != 0:
                print(f"BROKEN  {name}: {module} fails on the unmutated copy\n{baseline.stderr[-800:]}")
                failures += 1
                continue
            target.write_text(text.replace(old, new, 1))
            try:
                mutated = run_tests(copy, module, timeout=240)
            except (
                subprocess.TimeoutExpired
            ):  # a guard that is off can make a server start where it must not: that hangs the test
                print(f"caught   {name} ({module}, by hanging)", flush=True)
                continue
            if mutated.returncode == 0:
                print(f"SURVIVED {name}: {module} still passes with this guard off")
                failures += 1
            else:
                print(f"caught   {name} ({module})", flush=True)
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
    print(f"{failures} problem(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
