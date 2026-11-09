"""trustwalk.check_path: the same cases the relay's TypeScript walk is tested with (omp-private-relay/tests/unit/trustwalk.test.ts),
for the Python implementation, on real directories in a scratch tree. Ownership is simulated by patching `_owner_ok`, because a
test cannot create a directory that belongs to another user."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from omp_dashboard import trustwalk
from omp_dashboard.trustwalk import TrustError, check_path
from support import PLUGIN, shared_group  # noqa: F401  (sets up sys.path)


class TrustWalkCase(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="odt", dir="/tmp")).resolve()
        os.chmod(self.root, 0o700)
        self.addCleanup(shutil.rmtree, self.root, True)

    def mkdir(self, *parts: str, mode: int = 0o700) -> Path:
        path = self.root.joinpath(*parts)
        path.mkdir(parents=True, exist_ok=True)
        os.chmod(path, mode)
        return path

    def foreign(self, *paths: Path):
        """Treat these paths as belonging to another account."""
        names = {str(p) for p in paths}
        real = trustwalk._owner_ok
        original_lstat = os.lstat

        def owner_ok(info: os.stat_result) -> bool:
            return real(info) and not any(
                info.st_ino == original_lstat(n).st_ino and info.st_dev == original_lstat(n).st_dev for n in names
            )

        return mock.patch.object(trustwalk, "_owner_ok", owner_ok)


class Directories(TrustWalkCase):
    def test_a_tree_only_this_user_can_write_is_trusted(self) -> None:
        check_path(self.mkdir("a", "b"))

    def test_a_missing_leaf_is_fine_but_its_existing_ancestors_are_still_checked(self) -> None:
        check_path(self.root / "not" / "there" / "yet")
        loose = self.mkdir("loose", mode=0o777)
        with self.assertRaises(TrustError):
            check_path(loose / "not" / "there")

    def test_group_or_other_writable_is_refused_unless_sticky(self) -> None:
        shared_group(self)  # a group with other members: the case these checks are about
        for mode in (0o770, 0o707, 0o777):
            self.mkdir("w", mode=mode)
            with self.assertRaises(TrustError, msg=oct(mode)):
                check_path(self.root / "w" / "inner")
        self.mkdir("sticky", mode=0o1777)
        check_path(self.root / "sticky" / "inner")  # /tmp itself is the model

    def test_a_directory_of_another_account_is_refused(self) -> None:
        theirs = self.mkdir("theirs")
        with self.foreign(theirs), self.assertRaisesRegex(TrustError, "another user"):
            check_path(theirs / "x")

    def test_root_owned_directories_are_trusted(self) -> None:
        check_path("/")
        check_path("/tmp")  # root's, sticky (a link to /private/tmp on macOS)
        check_path("/usr/bin")

    def test_a_readable_but_not_writable_directory_is_fine(self) -> None:
        check_path(self.mkdir("readable", mode=0o755))


class Links(TrustWalkCase):
    def test_a_link_into_a_clean_tree_is_followed(self) -> None:
        target = self.mkdir("real", "inner")
        (self.root / "link").symlink_to(target)
        check_path(self.root / "link" / "file")

    def test_a_link_that_looks_safe_but_leads_into_a_group_writable_tree_is_refused(self) -> None:
        shared_group(self)  # a group with other members: the case these checks are about
        bad = self.mkdir("bad", mode=0o770)
        self.mkdir("bad", "private", mode=0o700)
        (self.root / "link").symlink_to(bad / "private")
        with self.assertRaises(TrustError):
            check_path(self.root / "link" / "x")

    def test_a_link_of_another_account_is_refused(self) -> None:
        self.mkdir("real")
        link = self.root / "link"
        link.symlink_to(self.root / "real")
        real_lstat = os.lstat

        def owner_ok(info: os.stat_result) -> bool:
            return info.st_ino != real_lstat(link).st_ino

        with mock.patch.object(trustwalk, "_owner_ok", owner_ok), self.assertRaisesRegex(TrustError, "symbolic link"):
            check_path(link / "x")

    def test_a_relative_link_with_dots_is_resolved_from_the_directory_that_holds_it(self) -> None:
        self.mkdir("a", "b")
        self.mkdir("c")
        (self.root / "a" / "b" / "link").symlink_to("../../c")
        check_path(self.root / "a" / "b" / "link" / "x")
        loose = self.mkdir("d", mode=0o777)
        (self.root / "a" / "b" / "link2").symlink_to(os.path.relpath(loose, self.root / "a" / "b"))
        with self.assertRaises(TrustError):
            check_path(self.root / "a" / "b" / "link2" / "x")

    def test_dot_dot_after_a_link_is_taken_from_the_real_directory_the_link_reached(self) -> None:
        loose = self.mkdir("loose", mode=0o777)
        inner = self.mkdir("loose", "inner")
        self.mkdir("ok")
        (self.root / "ok" / "link").symlink_to(inner)
        # ok/link/../x is loose/x, not ok/x: the parent of the REAL directory, which is group/other writable
        with self.assertRaises(TrustError):
            check_path(self.root / "ok" / "link" / ".." / "x")
        self.assertTrue(loose.exists())

    def test_loops_and_long_chains_end_in_a_refusal_not_a_hang(self) -> None:
        (self.root / "l1").symlink_to(self.root / "l2")
        (self.root / "l2").symlink_to(self.root / "l1")
        with self.assertRaisesRegex(TrustError, "too many"):
            check_path(self.root / "l1" / "x")
        previous = self.mkdir("end")
        for i in range(60):
            link = self.root / f"c{i}"
            link.symlink_to(previous)
            previous = link
        with self.assertRaisesRegex(TrustError, "too many"):
            check_path(previous / "x")

    def test_trailing_slashes_dot_components_and_doubled_slashes_change_nothing(self) -> None:
        d = self.mkdir("plain")
        for spelled in (f"{d}/", f"{d}//", f"{self.root}/./plain", f"{self.root}//plain/."):
            check_path(spelled)

    def test_a_file_where_a_directory_is_needed_is_refused(self) -> None:
        (self.root / "file").write_text("x")
        with self.assertRaisesRegex(TrustError, "not a directory"):
            check_path(self.root / "file" / "x")

    def test_a_target_ending_in_a_newline_is_not_validated_as_a_different_directory(self) -> None:
        loose = self.mkdir("loose", mode=0o777)
        odd = self.mkdir("nl\n")  # a real directory whose name ends in a newline
        (self.root / "link").symlink_to(self.root / "nl\n")
        if sys.platform == "darwin":  # `ls -le` output cannot be matched to such a name, so it is not trusted
            with self.assertRaisesRegex(TrustError, "newline"):
                check_path(self.root / "link" / "x")
        else:
            check_path(
                self.root / "link" / "x"
            )  # the newline-named directory is what the link reaches, and it is clean
        with self.assertRaises(TrustError):
            check_path(loose / "x")
        self.assertTrue(odd.exists())

    def test_the_system_own_links_are_followed(self) -> None:
        check_path(tempfile.gettempdir())  # /var/folders/... on macOS, /tmp on Linux


class Leaves(TrustWalkCase):
    def test_a_file_must_be_ours_and_not_writable_by_others(self) -> None:
        shared_group(self)  # a group with other members: the case these checks are about
        f = self.root / "f"
        f.write_text("x")
        os.chmod(f, 0o644)
        check_path(f)
        os.chmod(f, 0o664)
        with self.assertRaisesRegex(TrustError, "writable by group or other"):
            check_path(f)

    def test_a_secret_file_must_not_be_readable_by_others_either(self) -> None:
        f = self.root / "secret"
        f.write_text("x")
        os.chmod(f, 0o644)
        with self.assertRaisesRegex(TrustError, "accessible to group or other"):
            check_path(f, secret=True)
        os.chmod(f, 0o600)
        check_path(f, secret=True)


@unittest.skipUnless(sys.platform == "darwin", "macOS ACLs")
class MacAcls(TrustWalkCase):
    def acl(self, path: Path, entry: str) -> None:
        subprocess.run(["/bin/chmod", "+a", entry, str(path)], check=True)

    def test_an_allow_entry_for_another_principal_that_can_write_is_refused(self) -> None:
        d = self.mkdir("acl")
        self.acl(d, "group:everyone allow add_file,delete_child")
        with self.assertRaisesRegex(TrustError, "ACL entry"):
            check_path(d / "x")

    def test_deny_entries_and_read_only_grants_are_harmless(self) -> None:
        d = self.mkdir("acl2")
        self.acl(d, "group:everyone deny delete")
        self.acl(d, "group:everyone allow list,search,readattr")
        check_path(d / "x")

    def test_a_read_grant_on_a_secret_file_is_refused_but_not_on_a_plain_one(self) -> None:
        f = self.root / "f"
        f.write_text("x")
        os.chmod(f, 0o600)
        self.acl(f, "group:everyone allow read")
        check_path(f)
        with self.assertRaisesRegex(TrustError, "ACL entry"):
            check_path(f, secret=True)

    def test_an_acl_that_cannot_be_inspected_is_never_trusted(self) -> None:
        d = self.mkdir("noinspect")
        with (
            mock.patch.object(trustwalk, "_run", side_effect=OSError("no ls")),
            self.assertRaisesRegex(TrustError, "cannot inspect ACLs"),
        ):
            check_path(d / "x")

    def test_unexpected_ls_output_is_not_trusted(self) -> None:
        d = self.mkdir("weird")
        fake = subprocess.CompletedProcess([], 0, stdout="garbage\n", stderr="")
        with mock.patch.object(trustwalk, "_run", return_value=fake), self.assertRaises(TrustError):
            check_path(d / "x")


class LinuxAcls(TrustWalkCase):
    """The POSIX ACL extended attribute, parsed from bytes (a test cannot rely on the scratch file system having ACLs)."""

    @staticmethod
    def acl_bytes(*entries: tuple[int, int, int]) -> bytes:
        out = (2).to_bytes(4, "little")
        for tag, perm, who in entries:
            out += tag.to_bytes(2, "little") + perm.to_bytes(2, "little") + who.to_bytes(4, "little")
        return out

    def run_linux(self, raw: bytes, secret: bool = False) -> str | None:
        d = str(self.mkdir("lin"))
        with (
            mock.patch.object(os, "listxattr", create=True, return_value=["system.posix_acl_access"]),
            mock.patch.object(os, "getxattr", create=True, return_value=raw),
        ):
            return trustwalk._acl_linux(d, secret)

    def test_a_named_user_or_group_that_can_write_is_refused(self) -> None:
        for tag in (0x02, 0x08):
            self.assertIn("write", self.run_linux(self.acl_bytes((tag, 0x02, 1234))) or "")

    def test_read_only_entries_and_the_owners_own_entry_are_fine_unless_the_file_is_a_secret(self) -> None:
        raw = self.acl_bytes((0x02, 0x04, 1234))
        self.assertIsNone(self.run_linux(raw))
        self.assertIn("read", self.run_linux(raw, secret=True) or "")
        owner = os.lstat(self.root).st_uid
        self.assertIsNone(self.run_linux(self.acl_bytes((0x02, 0x06, owner))))

    def test_no_acl_attribute_or_no_acl_support_means_no_problem_but_a_failure_to_read_it_is_not_trusted(self) -> None:
        d = str(self.mkdir("plain-lin"))
        with mock.patch.object(os, "listxattr", create=True, return_value=[]):
            self.assertIsNone(trustwalk._acl_linux(d, False))
        unsupported = OSError(95, "Operation not supported")
        with mock.patch.object(os, "listxattr", create=True, side_effect=unsupported):
            self.assertIsNone(trustwalk._acl_linux(d, False))
        denied = OSError(13, "Permission denied")
        with mock.patch.object(os, "listxattr", create=True, side_effect=denied), self.assertRaises(TrustError):
            trustwalk._acl_linux(d, False)


if __name__ == "__main__":
    unittest.main()
