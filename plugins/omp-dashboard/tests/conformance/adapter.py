#!/usr/bin/env python3
"""The command-line adapter that lets plugins/omp-private-relay/tests/conformance/pairing-store-suite.ts drive this
plugin's reader and writer (python/omp_dashboard/pairing.py). It is test support, not part of the plugin's command
line. See the header of that suite for the contract.

    adapter.py check <dir> <cookie-name> <cookie-header>
    adapter.py create-token <dir>
    adapter.py redeem <dir> <token> <label>
    adapter.py devices <dir>
    adapter.py revoke <dir> <prefix|all>
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "python"))

from omp_dashboard.pairing import PairingError, PairingReader, PairingWriter


def out(value: object) -> None:
    print(json.dumps(value), flush=True)


def main(argv: list[str]) -> int:
    try:
        if len(argv) < 2:
            raise ValueError("usage: adapter.py <command> <dir> ...")
        command, directory, rest = argv[0], argv[1], argv[2:]
        if command == "check":
            cookie_name, header = [*rest, "", ""][:2]
            reader = PairingReader(directory)
            view = reader.read()
            device = None if view.refusal else reader.match(header, cookie_name)
            out({"paired": device is not None, "device": device, "refused": view.refusal})
            return 0
        writer = PairingWriter(directory)
        if command == "create-token":
            out({"token": writer.create_token()})
        elif command == "redeem":
            out({"session": writer.redeem(rest[0] if rest else None, rest[1] if len(rest) > 1 else None)})
        elif command == "devices":
            out({"devices": [{"id": d.id, "label": d.label, "pairedAt": d.paired_at} for d in writer.devices()]})
        elif command == "revoke":
            out({"revoked": writer.revoke(None if rest and rest[0] == "all" else (rest[0] if rest else ""))})
        else:
            raise ValueError(f"unknown command {command}")
        return 0
    except PairingError as err:
        out({"error": str(err)})
        return 1
    except Exception as err:
        out({"error": f"{type(err).__name__}: {err}"})
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
