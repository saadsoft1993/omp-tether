"""Projects and the Start backends: roots empty by default, path validation, herdr and tmux driven through stand-in commands that record
their argv, the one fixed text that is ever typed, and a real tmux server."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from omp_dashboard.hostplat import Platform
from omp_dashboard.projects import MAX_PROJECTS, discover_projects, expand_root, session_dir_name
from omp_dashboard.start import Herdr, StartError, Tmux, build_backend
from support import PLUGIN, Scratch, ScratchTestCase, host_entry  # noqa: F401


class Projects(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="odj", dir="/tmp")).resolve()
        self.addCleanup(shutil.rmtree, self.root, True)
        self.home = self.root / "home"
        self.home.mkdir()

    def repo(self, *parts: str) -> Path:
        p = self.home.joinpath(*parts)
        (p / ".git").mkdir(parents=True)
        return p

    def test_no_roots_means_no_projects(self) -> None:
        self.assertEqual(discover_projects([], self.home), [])

    def test_a_root_and_the_repositories_below_it_are_offered_to_the_depth_given(self) -> None:
        self.repo("code", "a")
        self.repo("code", "group", "b")
        self.repo("code", "x", "y", "z", "deep")
        found = {p["label"] for p in discover_projects(["~/code"], self.home, max_depth=3)}
        self.assertEqual(found, {"~/code", "~/code/a", "~/code/group/b"})
        self.assertIn("~/code/x/y/z/deep", {p["label"] for p in discover_projects(["~/code"], self.home, max_depth=4)})

    def test_links_hidden_and_vendored_folders_are_never_entered(self) -> None:
        self.repo("code", "real")
        self.repo("code", ".hidden")
        self.repo("code", "node_modules", "pkg")
        outside = self.root / "outside" / "evil"
        (outside / ".git").mkdir(parents=True)
        (self.home / "code" / "link").symlink_to(self.root / "outside")
        labels = {p["label"] for p in discover_projects(["~/code"], self.home)}
        self.assertEqual(labels, {"~/code", "~/code/real"})

    def test_a_missing_or_file_root_is_ignored(self) -> None:
        (self.home / "f").write_text("x")
        self.assertEqual(discover_projects(["~/nope", "~/f", "/nonexistent-root"], self.home), [])

    def test_absolute_roots_outside_home_keep_their_path(self) -> None:
        other = self.root / "elsewhere"
        other.mkdir()
        (p,) = discover_projects([str(other)], self.home)
        self.assertEqual(p["label"], str(other))
        self.assertEqual(expand_root("~/x", self.home), self.home / "x")

    def test_the_search_is_bounded(self) -> None:
        for i in range(MAX_PROJECTS + 40):
            (self.home / "many" / f"r{i:04d}" / ".git").mkdir(parents=True)
        self.assertLessEqual(len(discover_projects(["~/many"], self.home)), MAX_PROJECTS + 1)

    def test_ranking_reads_only_names_and_times_and_only_when_asked(self) -> None:
        a, b = self.repo("code", "a"), self.repo("code", "b")
        sessions = self.root / "sessions"
        for repo, age in ((a, 1000), (b, 10)):
            d = sessions / session_dir_name(repo, self.home)
            d.mkdir(parents=True)
            (d / "secret-transcript.jsonl").write_text("never read")
            os.utime(d, (time.time() - age, time.time() - age))
        plain = [p["name"] for p in discover_projects(["~/code"], self.home)]
        self.assertEqual(plain, ["code", "a", "b"] if "code" in plain else ["a", "b"])
        ranked = discover_projects(["~/code"], self.home, sessions_dir=sessions)
        names = [p["name"] for p in ranked if p["last_used"]]
        self.assertEqual(names, ["b", "a"])
        self.assertEqual(session_dir_name(self.home / "x" / "y", self.home), "-x-y")

    def test_the_hub_default_offers_nothing_and_ranking_is_off(self) -> None:
        s = Scratch()
        self.addCleanup(s.close)
        c = s.config()
        self.assertEqual(
            (c.get("start.backend"), c.get("start.roots"), c.get("projects.rankBySessions")), ("off", [], False)
        )
        self.assertIsNone(build_backend(c, Platform(), s.env))


FAKE_CLI = """#!{python}
import json, os, sys
here = os.path.dirname(os.path.abspath(__file__))
name = os.path.basename(__file__)
with open(os.path.join(here, name + ".argv"), "a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\\n")
state = json.load(open(os.path.join(here, name + ".json")))
key = " ".join(sys.argv[1:3]) if sys.argv[1:2] in (["workspace"], ["tab"], ["pane"]) else sys.argv[1]
if key in state.get("fail", []):
    sys.exit(1)
out = state["answers"].get(key)
if out is not None:
    print(out if isinstance(out, str) else json.dumps(out))
"""


class FakeCli:
    def __init__(self, directory: Path, name: str, answers: dict, **state) -> None:
        self.path = directory / name
        self.name = name
        self.path.write_text(FAKE_CLI.format(python=sys.executable))
        os.chmod(self.path, 0o700)
        self.state = {"answers": answers, **state}
        (directory / f"{name}.json").write_text(json.dumps(self.state))
        self.log = directory / f"{name}.argv"

    def calls(self) -> list[list[str]]:
        return [json.loads(x) for x in self.log.read_text().splitlines()] if self.log.exists() else []


class HostSequence:
    """list_hosts that shows a new session from the Nth call on."""

    def __init__(self, new_after: int, cwd: str) -> None:
        self.calls, self.new_after, self.cwd = 0, new_after, cwd

    def __call__(self) -> list[dict]:
        self.calls += 1
        return [host_entry(1)] + ([host_entry(9, cwd=self.cwd)] if self.calls > self.new_after else [])


class Validation(unittest.TestCase):
    def test_a_path_that_is_not_a_plain_absolute_path_is_refused_before_anything_runs(self) -> None:
        d = Path(tempfile.mkdtemp(prefix="odv", dir="/tmp"))
        self.addCleanup(shutil.rmtree, d, True)
        herdr = FakeCli(d, "herdr", {})
        tmux = FakeCli(d, "tmux", {})
        backends = (
            Herdr(lambda: str(herdr.path), {}, wait_s=1, sleep=lambda s: None),
            Tmux(lambda: str(tmux.path), "omp", Platform(), {}, wait_s=1, sleep=lambda s: None),
        )
        for backend in backends:
            for bad in ("relative/dir", "", None, 5, "/ok/-dashed", "/a\nb", "/a\x00b", ["/x"]):
                with self.assertRaises(StartError, msg=repr(bad)) as ctx:
                    backend.start(bad, lambda: [])  # type: ignore[arg-type]
                self.assertEqual(ctx.exception.status, 400)
        self.assertEqual((herdr.calls(), tmux.calls()), ([], []))


class HerdrBackend(unittest.TestCase):
    def setUp(self) -> None:
        self.d = Path(tempfile.mkdtemp(prefix="odh", dir="/tmp")).resolve()
        self.addCleanup(shutil.rmtree, self.d, True)
        (self.d / "proj" / "app").mkdir(parents=True)
        self.project = str(self.d / "proj" / "app")

    def herdr(self, **answers) -> tuple[Herdr, FakeCli]:
        base = {
            "workspace list": {"result": {"workspaces": []}},
            "pane list": {"result": {"panes": []}},
            "tab list": {"result": {"tabs": []}},
            "workspace create": {
                "result": {
                    "workspace": {"workspace_id": "w1", "label": "app"},
                    "tab": {"tab_id": "t1", "label": "1"},
                    "root_pane": {"pane_id": "p1"},
                }
            },
            "tab create": {"result": {"tab": {"tab_id": "t2", "label": "omp"}, "root_pane": {"pane_id": "p2"}}},
            "tab rename": {"result": {}},
        }
        base.update(answers)
        cli = FakeCli(self.d, "herdr", base)
        return Herdr(lambda: str(cli.path), {}, wait_s=3, sleep=lambda s: None), cli

    def test_a_new_workspace_is_made_the_tab_renamed_and_only_omp_is_typed(self) -> None:
        herdr, cli = self.herdr()
        seq = HostSequence(1, self.project)
        started = herdr.start(self.project, seq)
        self.assertEqual(
            (started["backend"], started["workspace"], started["tab"], started["pane"]), ("herdr", "app", "omp", "p1")
        )
        self.assertEqual(started["instanceId"], "inst0009")
        calls = cli.calls()
        self.assertIn(["workspace", "create", "--cwd", self.project, "--label", "app", "--no-focus"], calls)
        self.assertIn(["tab", "rename", "t1", "omp"], calls)
        typed = [c for c in calls if c[:2] == ["pane", "run"]]
        self.assertEqual(typed, [["pane", "run", "p1", "omp"]])

    def test_an_existing_workspace_with_a_pane_in_exactly_this_folder_gets_a_tab(self) -> None:
        herdr, cli = self.herdr(
            **{
                "workspace list": {"result": {"workspaces": [{"workspace_id": "wX", "label": "whatever"}]}},
                "pane list": {"result": {"panes": [{"workspace_id": "wX", "cwd": self.project}]}},
            }
        )
        started = herdr.start(self.project, HostSequence(0, self.project))
        self.assertEqual(started["workspace"], "whatever")
        self.assertEqual(
            [c for c in cli.calls() if c[:2] == ["tab", "create"]],
            [["tab", "create", "--workspace", "wX", "--cwd", self.project, "--label", "omp", "--no-focus"]],
        )
        self.assertEqual([c for c in cli.calls() if c[:2] == ["pane", "run"]], [["pane", "run", "p2", "omp"]])

    def test_two_projects_with_the_same_name_do_not_share_a_workspace(self) -> None:
        herdr, cli = self.herdr(
            **{
                "workspace list": {"result": {"workspaces": [{"workspace_id": "wY", "label": "app"}]}},
                "pane list": {"result": {"panes": [{"workspace_id": "wY", "cwd": "/somewhere/else/entirely"}]}},
            }
        )
        herdr.start(self.project, HostSequence(0, self.project))
        self.assertTrue(any(c[:2] == ["workspace", "create"] for c in cli.calls()))

    def test_it_says_when_the_new_session_did_not_show_up_and_when_herdr_is_missing_or_fails(self) -> None:
        herdr, _ = self.herdr()
        herdr.wait_s = 0.2
        self.assertIsNone(herdr.start(self.project, lambda: [host_entry(1)])["instanceId"])
        missing = Herdr(lambda: None, {})
        with self.assertRaises(StartError) as ctx:
            missing.start(self.project, lambda: [])
        self.assertEqual(ctx.exception.status, 503)
        broken, cli = self.herdr()
        cli.state["fail"] = ["workspace list"]
        (self.d / "herdr.json").write_text(json.dumps(cli.state))
        with self.assertRaises(StartError):
            broken.start(self.project, lambda: [])
        garbled, _ = self.herdr(**{"workspace list": "not json"})
        with self.assertRaises(StartError):
            garbled.start(self.project, lambda: [])

    def test_a_listing_failure_before_starting_means_nothing_is_started(self) -> None:
        herdr, cli = self.herdr()

        def failing():
            from omp_dashboard.omp import OmpError

            raise OmpError("down")

        with self.assertRaises(StartError):
            herdr.start(self.project, failing)
        self.assertEqual(cli.calls(), [])

    def test_locations_come_from_the_session_file_name_or_id(self) -> None:
        sid = "01a1b65b-93f3-7000-8000-000000000001"
        herdr, _ = self.herdr(
            **{
                "workspace list": {"result": {"workspaces": [{"workspace_id": "w1", "label": "sms"}]}},
                "tab list": {"result": {"tabs": [{"tab_id": "t1", "label": "omp"}]}},
                "pane list": {
                    "result": {
                        "panes": [
                            {
                                "workspace_id": "w1",
                                "tab_id": "t1",
                                "pane_id": "p9",
                                "agent_session": {"agent": "omp", "kind": "path", "value": f"/x/y/2026_{sid}.jsonl"},
                            },
                            {
                                "workspace_id": "w1",
                                "tab_id": "t1",
                                "pane_id": "p8",
                                "agent_session": {"agent": "other", "kind": "id", "value": "zzz"},
                            },
                        ]
                    }
                },
            }
        )
        self.assertEqual(
            herdr.locations([]), {sid: {"backend": "herdr", "workspace": "sms", "tab": "omp", "pane": "p9"}}
        )
        self.assertEqual(Herdr(lambda: None, {}).locations([]), {})


class TmuxBackend(unittest.TestCase):
    def setUp(self) -> None:
        self.d = Path(tempfile.mkdtemp(prefix="odt", dir="/tmp")).resolve()
        self.addCleanup(shutil.rmtree, self.d, True)
        (self.d / "work" / "my app").mkdir(parents=True)
        self.project = str(self.d / "work" / "my app")

    def tmux(self, exists: bool, **answers) -> tuple[Tmux, FakeCli]:
        base = {"new-window": "%7", "new-session": "%7", "list-panes": "", "send-keys": ""}
        base.update(answers)
        cli = FakeCli(self.d, "tmux", base, fail=[] if exists else ["has-session"])
        return Tmux(
            lambda: str(cli.path),
            "omp",
            Platform(),
            {"TMUX": "/tmp/tmux-1/default,1,0", "TMUX_PANE": "%1", "PATH": "/usr/bin"},
            wait_s=3,
            sleep=lambda s: None,
        ), cli

    def test_an_existing_session_gets_a_window_and_only_omp_is_typed_literally(self) -> None:
        tmux, cli = self.tmux(True)
        started = tmux.start(self.project, HostSequence(1, self.project))
        self.assertEqual(
            (started["backend"], started["workspace"], started["tab"], started["pane"], started["instanceId"]),
            ("tmux", "omp", "my app", "%7", "inst0009"),
        )
        calls = cli.calls()
        self.assertEqual(calls[0], ["has-session", "-t", "=omp"])
        self.assertEqual(
            calls[1], ["new-window", "-d", "-t", "=omp:", "-n", "my app", "-c", self.project, "-P", "-F", "#{pane_id}"]
        )
        self.assertEqual(calls[2:], [["send-keys", "-t", "%7", "-l", "--", "omp"], ["send-keys", "-t", "%7", "Enter"]])

    def test_a_missing_session_is_created_detached(self) -> None:
        tmux, cli = self.tmux(False)
        tmux.start(self.project, HostSequence(0, self.project))
        self.assertEqual(
            cli.calls()[1],
            ["new-session", "-d", "-s", "omp", "-n", "my app", "-c", self.project, "-P", "-F", "#{pane_id}"],
        )

    def test_the_hubs_own_tmux_variables_are_not_passed_on(self) -> None:
        tmux, _ = self.tmux(True)
        self.assertNotIn("TMUX", tmux._env)
        self.assertNotIn("TMUX_PANE", tmux._env)

    def test_an_odd_answer_or_a_failing_tmux_is_an_error_and_nothing_more_is_typed(self) -> None:
        tmux, cli = self.tmux(True, **{"new-window": "not a pane id"})
        with self.assertRaises(StartError):
            tmux.start(self.project, lambda: [])
        self.assertFalse(any(c[0] == "send-keys" for c in cli.calls()))
        with self.assertRaises(StartError) as ctx:
            Tmux(lambda: None, "omp", Platform(), {}).start(self.project, lambda: [])
        self.assertEqual(ctx.exception.status, 503)

    def test_a_folder_name_with_control_characters_is_cleaned_for_the_window_label(self) -> None:
        weird = self.d / "wei\x1brd"
        weird.mkdir()
        tmux, cli = self.tmux(True)
        tmux.start(str(weird), HostSequence(0, str(weird)))
        label = cli.calls()[1][cli.calls()[1].index("-n") + 1]
        self.assertNotRegex(label, r"[\x00-\x1f]")


@unittest.skipUnless(shutil.which("tmux"), "tmux is not installed here")
class RealTmux(unittest.TestCase):
    """A real tmux server in a private TMUX_TMPDIR, with a stand-in `omp` that records that it ran and where."""

    def setUp(self) -> None:
        self.d = Path(tempfile.mkdtemp(prefix="odT", dir="/tmp")).resolve()
        self.addCleanup(shutil.rmtree, self.d, True)
        os.chmod(self.d, 0o700)
        self.stub = self.d / "stub"
        self.stub.mkdir()
        omp = self.stub / "omp"
        omp.write_text(f'#!/bin/sh\necho $$ > "{self.d}/pid"\npwd -P > "{self.d}/cwd"\nexec sleep 60\n')
        os.chmod(omp, 0o700)
        self.project = self.d / "proj" / "app one"
        self.project.mkdir(parents=True)
        (self.d / "tmp").mkdir()
        self.env = {
            "PATH": f"{self.stub}:/usr/bin:/bin:/opt/homebrew/bin:/usr/local/bin",
            "TMUX_TMPDIR": str(self.d / "tmp"),
            "SHELL": "/bin/sh",
            "HOME": str(self.d),
            "LANG": "C.UTF-8",
        }
        self.tmux_bin = shutil.which("tmux") or ""
        self.addCleanup(lambda: subprocess.run([self.tmux_bin, "kill-server"], env=self.env, capture_output=True))

    def test_start_runs_omp_in_the_folder_and_the_session_is_located_by_ancestry(self) -> None:
        platform = Platform()
        backend = Tmux(lambda: self.tmux_bin, "odtest", platform, self.env, wait_s=15)
        state: dict = {}

        def list_hosts() -> list[dict]:
            pid_file = self.d / "pid"
            if pid_file.exists() and pid_file.read_text().strip():
                state["pid"] = int(pid_file.read_text())
                return [host_entry(5, pid=state["pid"], cwd=str(self.project), instanceId="realinst1")]
            return []

        started = backend.start(str(self.project), list_hosts)
        self.assertEqual(started["instanceId"], "realinst1")
        self.assertEqual(started["workspace"], "odtest")
        self.assertEqual(os.path.realpath((self.d / "cwd").read_text().strip()), os.path.realpath(self.project))
        hosts = list_hosts()
        place = backend.locations(hosts)[hosts[0]["sessionId"]]
        self.assertEqual(
            (place["backend"], place["workspace"], place["tab"], place["pane"]),
            ("tmux", "odtest", "app one", started["pane"]),
        )
        # a second start adds a window to the same session
        again = backend.start(str(self.project), lambda: [])
        self.assertEqual(again["workspace"], "odtest")
        windows = subprocess.run(
            [self.tmux_bin, "list-windows", "-t", "=odtest", "-F", "#{window_name}"],
            env=self.env,
            capture_output=True,
            text=True,
        ).stdout.split("\n")
        self.assertEqual(windows.count("app one"), 2)


class ThroughTheHub(ScratchTestCase):
    def test_start_only_accepts_a_path_the_hub_itself_listed(self) -> None:
        s = self.make()
        proj = s.root / "code" / "app"
        (proj / ".git").mkdir(parents=True)
        tmux = FakeCli(s.bin, "tmux", {"new-window": "%3", "new-session": "%3"}, fail=["has-session"])
        hub = self.hub(
            s, {"start": {"backend": "tmux", "roots": [str(s.root / "code")], "tmux": {"bin": str(tmux.path)}}}
        )
        hub.app.backend.wait_s = 0.2  # type: ignore[union-attr]
        hub.pair()
        c = hub.client
        listed = c.get("/api/hub/projects").json()
        self.assertEqual(listed["backend"], "tmux")
        self.assertEqual({p["name"] for p in listed["projects"]}, {"code", "app"})
        for evil in (
            str(s.root),
            "/etc",
            str(proj) + "/../..",
            "relative",
            str(s.root / "code" / "app" / "x"),
            5,
            None,
        ):
            r = c.post("/api/hub/start", {"path": evil})
            self.assertEqual(r.status, 400, evil)
        self.assertEqual(tmux.calls(), [])
        ok = c.post("/api/hub/start", {"path": str(proj)})
        self.assertEqual(ok.status, 200)
        self.assertEqual(next(x for x in tmux.calls() if x[0] == "send-keys")[-1], "omp")


if __name__ == "__main__":
    unittest.main()
