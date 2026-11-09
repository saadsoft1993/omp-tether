"""pairing-store/1 as this plugin implements it. The protocol's own conformance suite (which lives with omp-private-relay and is run
by `make test-conformance`) checks the reader and the writer against the specification; these tests check what the suite cannot:
the plugin's extra rule about the directories above the store, the cookie matching used by the hub, and, with the relay's own TypeScript
implementation, that each side reads what the other wrote."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

from omp_dashboard.pairing import (
    PAIR_TTL_S,
    PairingError,
    PairingReader,
    PairingStore,
    PairingWriter,
    clean_label,
    cookie_hash,
    cookie_values,
)
from support import PLUGIN, shared_group

REPO = PLUGIN.parents[1]
RELAY_ADAPTER = REPO / "plugins" / "omp-private-relay" / "tests" / "conformance" / "adapter.ts"


def find_bun() -> str:
    for candidate in (os.environ.get("BUN_BIN"), shutil.which("bun")):
        if candidate and os.access(candidate, os.X_OK):
            return candidate
    raise AssertionError("Bun 1.4.4 or newer is required for the cross-implementation tests (set BUN_BIN)")


class Scratch(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="odp", dir="/tmp")).resolve()
        os.chmod(self.root, 0o700)
        self.addCleanup(shutil.rmtree, self.root, True)
        self.dir = self.root / "store"


class Cookies(unittest.TestCase):
    def test_every_cookie_of_that_name_is_tried_and_odd_values_are_ignored(self) -> None:
        header = (
            "a=1; omp_dashboard=; omp_dashboard=first; other=x; omp_dashboard="
            + "v" * 513
            + "; omp_dashboard = second "
        )
        self.assertEqual(cookie_values(header, "omp_dashboard"), ["first", "second"])
        self.assertEqual(cookie_values(None, "x"), [])
        self.assertEqual(cookie_values("omp_dashboardx=1; xomp_dashboard=2", "omp_dashboard"), [])

    def test_the_hash_is_sha256_of_the_utf8_value_exactly_as_sent(self) -> None:
        self.assertEqual(cookie_hash("abc"), "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad")
        self.assertEqual(len(cookie_hash("é")), 64)

    def test_labels_lose_controls_and_invisible_characters_and_are_cut(self) -> None:
        self.assertEqual(clean_label("Fire\x1b[31mfox\u202e\u200b"), "Fire [31mfox")
        self.assertEqual(clean_label("\x07\x07"), "browser")
        self.assertEqual(clean_label(None), "browser")
        self.assertEqual(len(clean_label("x" * 200)), 80)


class WriterAndReader(Scratch):
    def test_pair_then_match_then_revoke(self) -> None:
        writer = PairingWriter(self.dir)
        token = writer.create_token()
        session = writer.redeem(token, "Firefox on Linux")
        self.assertIsNotNone(session)
        reader = PairingReader(self.dir)
        header = f"omp_dashboard={session}"
        self.assertEqual(reader.match(header, "omp_dashboard"), cookie_hash(session))
        self.assertIsNone(reader.match("omp_dashboard=wrong", "omp_dashboard"))
        (device,) = writer.devices()
        self.assertEqual((device.label, device.id), ("Firefox on Linux", cookie_hash(session)[:8]))
        self.assertEqual(writer.revoke(device.id), 1)
        self.assertIsNone(reader.match(header, "omp_dashboard"), "revocation is seen on the next read")

    def test_a_token_works_once_and_an_expired_one_not_at_all(self) -> None:
        clock = [1_000_000.0]
        writer = PairingWriter(self.dir, now=lambda: clock[0])
        first = writer.create_token()
        self.assertIsNotNone(writer.redeem(first, "x"))
        self.assertIsNone(writer.redeem(first, "x"))
        late = writer.create_token()
        clock[0] += PAIR_TTL_S + 1
        self.assertIsNone(writer.redeem(late, "x"))
        self.assertEqual(len(writer.devices()), 1)

    def test_only_hashes_are_stored(self) -> None:
        writer = PairingWriter(self.dir)
        token = writer.create_token()
        session = writer.redeem(token, "x")
        for name in ("pending.json", "devices.json"):
            text = (self.dir / name).read_text()
            self.assertNotIn(token, text)
            self.assertNotIn(session, text)
        self.assertEqual(json.loads((self.dir / "devices.json").read_text()).keys(), {cookie_hash(session)})

    def test_junk_tokens_are_refused_without_touching_the_store(self) -> None:
        writer = PairingWriter(self.dir)
        writer.create_token()
        before = (self.dir / "pending.json").read_bytes()
        for junk in (None, "", 5, ["x"], "x" * 513, {"a": 1}):
            self.assertIsNone(writer.redeem(junk, "x"))
        self.assertEqual((self.dir / "pending.json").read_bytes(), before)
        self.assertFalse((self.dir / "devices.json").exists())

    def test_modes_of_everything_written(self) -> None:
        writer = PairingWriter(self.dir)
        writer.redeem(writer.create_token(), "x")
        self.assertEqual(stat.S_IMODE(os.stat(self.dir).st_mode), 0o700)
        for name in ("pending.json", "devices.json", ".lock"):
            self.assertEqual(stat.S_IMODE(os.stat(self.dir / name).st_mode), 0o600, name)
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), [".lock", "devices.json", "pending.json"])

    def test_eight_simultaneous_redemptions_all_land(self) -> None:
        writer = PairingWriter(self.dir)
        tokens = [writer.create_token() for _ in range(8)]
        sessions: list[str | None] = [None] * 8

        def run(i: int) -> None:
            sessions[i] = PairingWriter(self.dir).redeem(tokens[i], f"b{i}")

        threads = [threading.Thread(target=run, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertTrue(all(sessions))
        self.assertEqual(len(writer.devices()), 8)

    def test_a_corrupt_store_is_never_overwritten(self) -> None:
        writer = PairingWriter(self.dir)
        writer.redeem(writer.create_token(), "x")
        (self.dir / "devices.json").write_text("{not json")
        for call in (lambda: writer.redeem(writer.create_token(), "y"), lambda: writer.revoke(None)):
            with self.assertRaises(PairingError) as ctx:
                call()
            self.assertEqual(ctx.exception.code, "corrupt")
        self.assertEqual((self.dir / "devices.json").read_text(), "{not json")

    def test_a_prefix_that_is_too_short_or_ambiguous_removes_nothing(self) -> None:
        writer = PairingWriter(self.dir)
        for _ in range(2):
            writer.redeem(writer.create_token(), "x")
        for bad in ("", "abc", "zzzz", "ABCD", "../.."):
            with self.assertRaises(PairingError):
                writer.revoke(bad)
        self.assertEqual(len(writer.devices()), 2)
        self.assertEqual(writer.revoke(None), 2)

    def test_a_reader_never_changes_a_store_it_refuses(self) -> None:
        writer = PairingWriter(self.dir)
        writer.redeem(writer.create_token(), "x")
        os.chmod(self.dir / "devices.json", 0o644)
        view = PairingReader(self.dir).read()
        self.assertEqual(view.refusal, "store-open-to-others")
        self.assertEqual(stat.S_IMODE(os.stat(self.dir / "devices.json").st_mode), 0o644)
        self.assertEqual(view.hashes, frozenset())

    def test_the_reader_reports_a_refusal_once_not_on_every_request(self) -> None:
        seen: list[str] = []
        reader = PairingReader(self.dir, on_refusal=seen.append)
        for _ in range(5):
            reader.read()
        self.assertEqual(seen, ["store-missing"])

    def test_the_reader_notices_a_changed_file_even_with_the_same_size(self) -> None:
        writer = PairingWriter(self.dir)
        session = writer.redeem(writer.create_token(), "x")
        reader = PairingReader(self.dir)
        self.assertTrue(reader.match(f"c={session}", "c"))
        time.sleep(0.01)
        writer.revoke(None)
        self.assertIsNone(reader.match(f"c={session}", "c"))


class StoreAncestry(Scratch):
    """The plugin's own rule on top of the protocol: a directory above the store that another account could replace means nobody is paired."""

    def test_a_clean_chain_pairs_browsers(self) -> None:
        store = PairingStore(self.dir, "c")
        session = store.writer.redeem(store.writer.create_token(), "x")
        self.assertEqual(store.paired_device(f"c={session}"), cookie_hash(session)[:8])
        self.assertIsNone(store.ancestry_problem())

    def test_a_group_writable_ancestor_means_nobody_is_paired_and_says_why(self) -> None:
        loose = self.root / "loose"
        loose.mkdir(mode=0o700)
        store = PairingStore(loose / "inner" / "store", "c")
        session = store.writer.redeem(store.writer.create_token(), "x")
        self.assertTrue(store.paired_device(f"c={session}"))
        shared_group(self)  # a group with other members: the case these checks are about
        os.chmod(loose, 0o770)
        store2 = PairingStore(loose / "inner" / "store", "c")  # a fresh walk: the answer is cached for a few seconds
        self.assertIsNone(store2.paired_device(f"c={session}"))
        self.assertEqual(store2.view().refusal, "store-untrusted-path")
        self.assertIn("writable by group or other", store2.ancestry_problem() or "")

    def test_the_walk_is_cached_briefly(self) -> None:
        calls = []
        store = PairingStore(self.dir, "c")
        import omp_dashboard.pairing as module

        original = module.check_path
        module.check_path = lambda p, **k: calls.append(p)
        try:
            for _ in range(5):
                store.ancestry_problem()
        finally:
            module.check_path = original
        self.assertEqual(len(calls), 1)


class WithTheRelaysImplementation(Scratch):
    """Each side reads what the other wrote: omp-private-relay's TypeScript writer and reader, run on Bun."""

    def ts(self, *args: str) -> dict:
        proc = subprocess.run(
            [find_bun(), "--no-install", str(RELAY_ADAPTER), *args],
            capture_output=True,
            text=True,
            timeout=60,
            env={**os.environ, "HOME": str(self.root)},
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return json.loads(proc.stdout.strip().splitlines()[-1])

    def test_a_browser_paired_by_this_plugin_is_paired_for_the_relays_reader(self) -> None:
        writer = PairingWriter(self.dir)
        session = writer.redeem(writer.create_token(), "Firefox")
        answer = self.ts("check", str(self.dir), "omp_dashboard", f"a=1; omp_dashboard={session}")
        self.assertEqual(answer, {"paired": True, "device": cookie_hash(session), "refused": None})
        self.assertFalse(self.ts("check", str(self.dir), "omp_dashboard", "omp_dashboard=nope")["paired"])
        writer.revoke(None)
        self.assertFalse(self.ts("check", str(self.dir), "omp_dashboard", f"omp_dashboard={session}")["paired"])

    def test_a_browser_paired_by_the_relay_is_paired_for_this_plugins_reader(self) -> None:
        token = self.ts("create-token", str(self.dir))["token"]
        session = self.ts("redeem", str(self.dir), token, "Safari")["session"]
        self.assertTrue(session)
        reader = PairingReader(self.dir)
        self.assertEqual(reader.match(f"omp_relay={session}", "omp_relay"), cookie_hash(session))
        (device,) = PairingWriter(self.dir).devices()
        self.assertEqual(device.label, "Safari")
        # and this plugin's writer can revoke what the relay's wrote
        self.assertEqual(PairingWriter(self.dir).revoke(device.id), 1)
        self.assertIsNone(reader.match(f"omp_relay={session}", "omp_relay"))

    def test_tokens_made_by_one_side_are_redeemed_by_the_other(self) -> None:
        token = PairingWriter(self.dir).create_token()
        session = self.ts("redeem", str(self.dir), token, "x")["session"]
        self.assertTrue(session)
        self.assertIsNone(PairingWriter(self.dir).redeem(token, "again"))
        token2 = self.ts("create-token", str(self.dir))["token"]
        self.assertTrue(PairingWriter(self.dir).redeem(token2, "y"))


if __name__ == "__main__":
    unittest.main()
