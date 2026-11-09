"""The relay's directory walker (omp-private-relay/src/trustwalk.ts) and this plugin's walker must reach the same verdict on the same real macOS ACL cases."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from omp_dashboard.trustwalk import TrustError, check_path
from support import PLUGIN

RELAY_WALKER = PLUGIN.parents[1] / "plugins" / "omp-private-relay" / "src" / "trustwalk.ts"
BUN = os.environ.get("BUN_BIN") or shutil.which("bun")


def relay_verdict(directory: Path) -> bool:
    """True when the relay's walker finds a problem with `directory`."""
    code = f'import {{ trustWalk }} from "{RELAY_WALKER}"; console.log(JSON.stringify(trustWalk({json.dumps(str(directory))}).problem));'
    out = subprocess.run([BUN or "bun", "-e", code], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1]) is not None


def dashboard_verdict(directory: Path) -> bool:
    try:
        check_path(directory)
    except TrustError:
        return True
    return False


@unittest.skipUnless(
    sys.platform == "darwin" and BUN and RELAY_WALKER.exists(), "needs macOS ACLs, bun and the relay's sources"
)
class SameVerdicts(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="odw", dir="/tmp")).resolve()
        os.chmod(self.root, 0o700)
        self.addCleanup(shutil.rmtree, self.root, True)

    def chmod(self, *args: str) -> None:
        subprocess.run(["/bin/chmod", *args], check=True)

    def inherited_copy(self, entry: str) -> Path:
        """A directory that carries an `inherited` copy of `entry` after the parent's own entry was removed."""
        parent = self.root / "p"
        parent.mkdir(mode=0o700)
        self.chmod("+a", f"group:everyone allow {entry},file_inherit,directory_inherit", str(parent))
        child = parent / "served"
        child.mkdir(mode=0o700)
        self.chmod("-a#", "0", str(parent))
        return child

    def test_both_refuse_an_inherited_write_entry_that_outlived_its_source(self) -> None:
        d = self.inherited_copy("write,add_file,add_subdirectory,delete_child")
        listing = subprocess.run(["/bin/ls", "-lde", str(d)], capture_output=True, text=True).stdout
        self.assertIn("inherited", listing)
        self.assertTrue(dashboard_verdict(d))
        self.assertTrue(relay_verdict(d))

    def test_both_accept_an_inherited_read_entry(self) -> None:
        d = self.inherited_copy("read")
        self.assertFalse(dashboard_verdict(d))
        self.assertFalse(relay_verdict(d))

    def test_both_refuse_a_direct_write_entry_and_accept_a_plain_directory(self) -> None:
        plain = self.root / "plain"
        plain.mkdir(mode=0o700)
        self.assertEqual((dashboard_verdict(plain), relay_verdict(plain)), (False, False))
        direct = self.root / "direct"
        direct.mkdir(mode=0o700)
        self.chmod("+a", "group:everyone allow write,add_file", str(direct))
        self.assertEqual((dashboard_verdict(direct), relay_verdict(direct)), (True, True))


if __name__ == "__main__":
    unittest.main()
