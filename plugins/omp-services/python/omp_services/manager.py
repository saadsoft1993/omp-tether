"""Talking to the service manager: launchd on macOS, `systemd --user` on Linux.

Every call is a bounded child process without a shell. The binaries are found at their fixed system locations; the
environment variables OMP_SERVICES_LAUNCHCTL, OMP_SERVICES_SYSTEMCTL and OMP_SERVICES_LOGINCTL point at other
binaries (the tests use scripts that only record what they were asked to do).

All methods act on one `label`, the identifier shared by the unit file name and the manager. The caller has already
checked that the label carries this tool's prefix; the managers do not touch anything else.
"""

from __future__ import annotations

import os
import shutil
import time
from collections.abc import Callable, Mapping
from pathlib import Path

from .util import RunResult, ServicesError, one_line, run_bounded, safe_env

CALL_TIMEOUT = 30.0


def _tool(env: Mapping[str, str], var: str, name: str, candidates: tuple[str, ...]) -> str | None:
    forced = env.get(var)
    if forced:
        return forced
    for candidate in candidates:
        if os.access(candidate, os.X_OK):
            return candidate
    return shutil.which(name, path="/usr/bin:/bin:/usr/sbin:/sbin")


class ServiceManager:
    kind = ""
    suffix = ""

    def __init__(self, env: Mapping[str, str] | None = None, sleep: Callable[[float], None] = time.sleep):
        self.env = os.environ if env is None else env
        self.sleep = sleep
        self.deadline: float | None = None  # absolute (monotonic): while set, no call may run past it

    def _limit(self, timeout: float) -> float:
        """The time a call may take: its own limit, cut to what is left of the deadline. 0 (or less): none is left."""
        if self.deadline is None:
            return timeout
        return min(timeout, self.deadline - time.monotonic())

    def _expired(self, limit: float) -> RunResult | None:
        return RunResult(None, "", "", error="deadline") if limit <= 0 else None

    def transitional(self, label: str) -> bool:
        """True while the manager reports the unit switching state (activating, deactivating, reloading): such a unit is
        neither stopped nor running, so nothing is stopped, unloaded or restarted until it settles."""
        return False

    def unit_file(self, unit_dir: Path, label: str) -> Path:
        return unit_dir / f"{label}{self.suffix}"

    def problem(self) -> str | None:
        """Why this manager cannot be used here, or None."""
        raise NotImplementedError

    def is_loaded(self, label: str) -> bool:
        raise NotImplementedError

    def is_running(self, label: str) -> bool:
        raise NotImplementedError

    def load(self, label: str, unit_path: Path) -> None:
        raise NotImplementedError

    def unload(self, label: str, unit_path: Path) -> None:
        raise NotImplementedError

    def restart(self, label: str) -> None:
        raise NotImplementedError

    def linger_note(self) -> str | None:
        return None


class Launchd(ServiceManager):
    kind = "launchd"
    suffix = ".plist"

    def __init__(self, env: Mapping[str, str] | None = None, sleep: Callable[[float], None] = time.sleep):
        super().__init__(env, sleep)
        self.launchctl = _tool(self.env, "OMP_SERVICES_LAUNCHCTL", "launchctl", ("/bin/launchctl",))
        self.domain = f"gui/{os.geteuid()}"

    def _run(self, *args: str, timeout: float = CALL_TIMEOUT) -> RunResult:
        if not self.launchctl:
            return RunResult(None, "", "", error="launchctl not found")
        limit = self._limit(timeout)
        return self._expired(limit) or run_bounded([self.launchctl, *args], limit, env=safe_env())

    def problem(self) -> str | None:
        if not self.launchctl:
            return "launchctl not found"
        result = self._run("print", self.domain)
        if not result.ok:
            return f"the {self.domain} launchd domain is not available (is a GUI session logged in for this user?)"
        return None

    def _print(self, label: str) -> RunResult:
        result = self._run("print", f"{self.domain}/{label}")
        if result.error or result.timed_out:  # "could not ask" is not "no such job"
            raise ServicesError(f"cannot ask launchd about {label}: {result.error or 'timed out'}")
        # Only two answers are understood: a description of the job, or "no such job" (exit 113). Any other failure (a
        # domain that cannot be reached, a bad request) is not an answer, and nothing destructive may follow from it.
        if not result.ok and result.returncode != 113 and "Could not find service" not in result.stderr:
            raise ServicesError(f"launchd could not answer about {label}: {one_line(result.stderr or 'no message')}")
        return result

    def is_loaded(self, label: str) -> bool:
        return self._print(label).ok

    def is_running(self, label: str) -> bool:
        result = self._print(label)
        return result.ok and any(line.strip() == "state = running" for line in result.stdout.splitlines())

    def load(self, label: str, unit_path: Path) -> None:
        last = ""
        for attempt in range(5):
            result = self._run("bootstrap", self.domain, str(unit_path))
            if result.ok:
                return
            last = one_line(result.stderr or result.stdout or result.error or "no answer")
            if attempt < 4:
                self.sleep(1.0)
        raise ServicesError(f"could not load {label}: {last}")

    def unload(self, label: str, unit_path: Path) -> None:
        if not self.is_loaded(label):
            return
        self._run("bootout", f"{self.domain}/{label}")
        for _ in range(20):
            if not self.is_loaded(label):
                return
            self.sleep(0.5)
        raise ServicesError(f"{label} is still loaded after bootout")

    def restart(self, label: str) -> None:
        result = self._run("kickstart", "-k", f"{self.domain}/{label}")
        if not result.ok:
            raise ServicesError(f"could not restart {label}: {one_line(result.stderr or result.error or 'no answer')}")


class Systemd(ServiceManager):
    kind = "systemd"
    suffix = ".service"

    def __init__(self, env: Mapping[str, str] | None = None, sleep: Callable[[float], None] = time.sleep):
        super().__init__(env, sleep)
        self.systemctl = _tool(
            self.env, "OMP_SERVICES_SYSTEMCTL", "systemctl", ("/usr/bin/systemctl", "/bin/systemctl")
        )
        self.loginctl = _tool(self.env, "OMP_SERVICES_LOGINCTL", "loginctl", ("/usr/bin/loginctl", "/bin/loginctl"))

    def _env(self) -> dict[str, str]:
        extra = {k: os.environ[k] for k in ("XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS") if k in os.environ}
        return safe_env(extra)

    def _run(self, *args: str, timeout: float = CALL_TIMEOUT) -> RunResult:
        if not self.systemctl:
            return RunResult(None, "", "", error="systemctl not found")
        limit = self._limit(timeout)
        return self._expired(limit) or run_bounded([self.systemctl, "--user", *args], limit, env=self._env())

    def problem(self) -> str | None:
        if not self.systemctl:
            return "systemctl not found (is this a systemd system?)"
        result = self._run("show-environment")
        if not result.ok:
            return "`systemctl --user` does not answer (no user session bus: log in normally, or set XDG_RUNTIME_DIR)"
        return None

    def _unit(self, label: str) -> str:
        return f"{label}{self.suffix}"

    ENABLED_WORDS = frozenset({"enabled", "enabled-runtime", "alias"})
    DISABLED_WORDS = frozenset(
        {"disabled", "masked", "masked-runtime", "linked", "linked-runtime", "static", "indirect"}
    )
    # What is-active may say. "inactive" and "failed" are the only words that mean the unit is not running; "active" is
    # running; the rest are a unit switching state, which is neither stopped nor safely running.
    DEAD_WORDS = frozenset({"inactive", "failed"})
    TRANSITIONAL_WORDS = frozenset({"activating", "deactivating", "reloading", "maintenance"})
    ACTIVE_WORDS = DEAD_WORDS | TRANSITIONAL_WORDS | {"active"}

    def _ask(self, verb: str, label: str) -> RunResult:
        result = self._run(verb, self._unit(label))
        if result.error or result.timed_out:  # "could not ask" is not "no"
            raise ServicesError(f"cannot ask systemd about {label}: {result.error or 'timed out'}")
        return result

    def is_enabled(self, label: str) -> bool:
        """True or False only for an answer systemd really gave (a state word, or "no such unit file"); a command that
        failed for another reason (no bus, no session) is an error, not "disabled"."""
        result = self._ask("is-enabled", label)
        word = result.stdout.strip()
        if word in self.ENABLED_WORDS:
            return True
        if word in self.DISABLED_WORDS or (not word and "No such file" in result.stderr):
            return False
        raise ServicesError(
            f"systemd could not answer about {label}: {one_line(result.stderr or word or 'no message')}"
        )

    def _active_word(self, label: str) -> str:
        result = self._ask("is-active", label)
        word = result.stdout.strip()
        if word not in self.ACTIVE_WORDS:
            raise ServicesError(
                f"systemd could not answer about {label}: {one_line(result.stderr or word or 'no message')}"
            )
        return word

    def is_running(self, label: str) -> bool:
        """True unless systemd says the unit is inactive or failed: a unit that is activating, reloading or deactivating
        is live, and is never treated as stopped."""
        return self._active_word(label) not in self.DEAD_WORDS

    def transitional(self, label: str) -> bool:
        return self._active_word(label) in self.TRANSITIONAL_WORDS

    def is_loaded(self, label: str) -> bool:
        """Known to the manager and in effect: enabled, or active (a unit can be one without the other)."""
        return self.is_enabled(label) or self.is_running(label)

    def load(self, label: str, unit_path: Path) -> None:
        for args in (("daemon-reload",), ("enable", "--now", self._unit(label))):
            result = self._run(*args)
            if not result.ok:
                raise ServicesError(
                    f"could not load {label} (systemctl {args[0]}): {one_line(result.stderr or result.error or 'no answer')}"
                )

    def unload(self, label: str, unit_path: Path) -> None:
        if self.transitional(label):  # not while systemd is switching its state: stop, then check, then delete
            raise ServicesError(f"{label} is changing state (activating, reloading or deactivating): not unloaded")
        if self.is_enabled(label):
            result = self._run("disable", "--now", self._unit(label))
            if not result.ok:
                raise ServicesError(f"could not stop {label}: {one_line(result.stderr or result.error or 'no answer')}")
        if self.is_running(label):
            result = self._run("stop", self._unit(label))
            if not result.ok:
                raise ServicesError(f"could not stop {label}: {one_line(result.stderr or result.error or 'no answer')}")
        if self.is_running(label):
            raise ServicesError(f"{label} is still running after it was told to stop")

    def restart(self, label: str) -> None:
        result = self._run("restart", self._unit(label))
        if not result.ok:
            raise ServicesError(f"could not restart {label}: {one_line(result.stderr or result.error or 'no answer')}")

    def refresh(self) -> None:
        """Tell systemd that unit files were removed or changed on disk."""
        self._run("daemon-reload")

    def lingering(self) -> bool | None:
        """True or False when loginctl says whether this user's services outlive the login; None when unknown."""
        if not self.loginctl:
            return None
        result = run_bounded(
            [self.loginctl, "show-user", str(os.geteuid()), "-p", "Linger", "--value"], 10.0, env=self._env()
        )
        if not result.ok:
            return None
        return result.stdout.strip() == "yes"

    def linger_note(self) -> str | None:
        if self.lingering() is True:
            return None
        return (
            "systemd --user services stop when you log out and start again at the next login, unless lingering is on. "
            "To keep them running while you are logged out, run once: loginctl enable-linger "
            "(omp-services never changes this itself)."
        )


def make_manager(
    kind: str, env: Mapping[str, str] | None = None, sleep: Callable[[float], None] = time.sleep
) -> ServiceManager:
    if kind == "launchd":
        return Launchd(env, sleep)
    if kind == "systemd":
        return Systemd(env, sleep)
    raise ServicesError(f"unknown service manager {kind!r}")
