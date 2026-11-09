#!/usr/bin/env python3
"""A scratch environment to check the PACKAGED web client in a real browser (nothing outside a scratch HOME changes).

It starts an omp session hosting through a relay on random ports (scratch HOME, the shipped archive installed by
`init --asset-file`), prints the pairing link and the session link, and then takes commands from a control file so
that a person (or a browser-driving tool) can follow the release checklist in docs/release-evidence.md:

    echo restart     >> CONTROL     restart the relay (the grace period and the browsers' reconnect)
    echo revoke-all  >> CONTROL     unpair every browser (open pages must end with the 4401 text)
    echo pair        >> CONTROL     print a fresh pairing link
    echo stop        >> CONTROL     stop everything and remove the scratch HOME

    scripts/browser-check.py --asset omp-private-relay-web-18.18.tar [--adapter token]

Needs: omp on PATH (or OMP_BIN), the shipped web archive, and the repository's tools/testkit.
Prints one line "READY {json}" when the links are ready.
"""

from __future__ import annotations

import argparse
import json
import socket
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PLUGIN = HERE.parent
sys.path.insert(0, str(PLUGIN.parent.parent / "tools" / "testkit"))
import omp_scratch as kit


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--asset", required=True, help="the web archive that will be released")
    ap.add_argument("--control", help="control file (default: a file in the scratch directory)")
    args = ap.parse_args()
    omp = kit.find_omp()
    if not omp:
        print("omp not found (put it on PATH or set OMP_BIN)", file=sys.stderr)
        return 2
    host, guest = free_port(), free_port()
    sh = kit.ScratchHome(relay_url=f"ws://127.0.0.1:{host}", prefix="relaycheck")
    config = sh.omp_config_dir / "config.yml"
    config.write_text(config.read_text() + f"  webUrl: http://127.0.0.1:{guest}\n")
    env = {**sh.env, "OMP_BIN": omp}
    cli = str(PLUGIN / "bin" / "omp-private-relay")

    def relay_cli(*a: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([cli, *a], env=env, capture_output=True, text=True, timeout=120)

    init = relay_cli("init", "--yes", "--asset-file", args.asset, "--host-port", str(host), "--guest-port", str(guest))
    if init.returncode != 0:
        print(init.stdout, init.stderr, file=sys.stderr)
        sh.cleanup()
        return 1

    def start_relay() -> subprocess.Popen[bytes]:
        proc = subprocess.Popen(
            [cli, "run"], env=env, stdout=open(sh.root / "relay.log", "ab"), stderr=subprocess.STDOUT
        )
        kit.wait_for(
            lambda: subprocess.run(["curl", "-s", f"http://127.0.0.1:{host}/healthz"], capture_output=True).stdout
            == b"ok",
            20,
            what="relay",
        )
        return proc

    relay = start_relay()
    session = kit.PtyOmp(sh)
    control = Path(args.control) if args.control else sh.root / "control"
    control.write_text("")
    out = sh.root / "control.out"
    try:
        kit.wait_for(lambda: session.collab_hosts(), 60, 1, "omp hosting")
        link = sh.cli("collab", "link", session.collab_hosts()[0]["instanceId"], timeout=30).stdout.strip()
        pair = [ln.strip() for ln in relay_cli("pair").stdout.splitlines() if "#token=" in ln]
        print(
            "READY "
            + json.dumps(
                {
                    "root": str(sh.root),
                    "control": str(control),
                    "out": str(out),
                    "pair": pair[0] if pair else None,
                    "link": link,
                }
            ),
            flush=True,
        )
        seen = 0
        while True:
            lines = control.read_text().splitlines()
            for cmd in lines[seen:]:
                seen += 1
                if cmd == "stop":
                    return 0
                if cmd == "restart":
                    relay.terminate()
                    relay.wait(10)
                    relay = start_relay()
                    result = "relay restarted"
                elif cmd == "revoke-all":
                    result = relay_cli("revoke", "all").stdout.strip()
                elif cmd == "pair":
                    result = " ".join(ln.strip() for ln in relay_cli("pair").stdout.splitlines() if "#token=" in ln)
                else:
                    result = f"unknown command {cmd!r}"
                with out.open("a") as f:
                    f.write(f"{cmd}: {result}\n")
            time.sleep(0.5)
    finally:
        session.stop()
        relay.terminate()
        try:
            relay.wait(10)
        except subprocess.TimeoutExpired:
            relay.kill()
        sh.cleanup()
        print("CLEANED", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
