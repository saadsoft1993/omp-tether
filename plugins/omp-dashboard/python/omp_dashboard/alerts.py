"""Phone alerts through ntfy, off until you set a topic.

ntfy (https://ntfy.sh, a third party; or a server of your own) delivers a message to every device subscribed to a
*topic*. The topic is the only thing that decides who receives it, so it is a bearer secret: it lives in its own 0600 file
in a 0700 directory (never in config.json), is never put in a log, and is not sent to the browser. An alert names the
project folder and the model, and nothing else: no transcript, no session title (omp writes that from your first prompt),
no link to a session. It passes the ntfy server in clear text to that server's operator, which is why alerts are off
until you ask for them.

While no topic is set the hub does not even poll omp for sessions on behalf of alerts.
"""

from __future__ import annotations

import http.client
import json
import re
import ssl
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .util import DashboardError, Deadline, connect_bounded, ensure_private_dir, printable, read_text, write_file

TOPIC_RE = re.compile(
    r"[A-Za-z0-9_-]{16,64}\Z"
)  # a topic is a bearer secret in a public namespace: short ones are guessable
POLL_S = 5.0
MAX_ANSWER_BYTES = 4096


def read_topic(path: Path) -> str | None:
    """The ntfy topic, or None when alerts are off (no file). Raises DashboardError when the file is not safe or not a
    topic: an unusable file is never treated as "off" silently, the hub says so on the Alerts page."""
    try:
        text = read_text(path, limit=256, secret=True).strip()
    except FileNotFoundError:
        return None
    if not TOPIC_RE.match(text):
        raise DashboardError(f"{path}: not a topic (16 to 64 letters, digits, - or _)")
    return text


def write_topic(path: Path, topic: str) -> None:
    if not TOPIC_RE.match(topic):
        raise DashboardError("a topic is 16 to 64 letters, digits, - or _")
    ensure_private_dir(path.parent, refuse_open=True)
    write_file(path, topic + "\n", 0o600)


class AlertTracker:
    """Turns successive session listings into alerts: a long turn finished, or a session waits for you.

    Short turns do not alert: you are probably at the keyboard. Sessions first seen in a listing do not alert for the
    state they were already in.
    """

    def __init__(self, min_turn_ms: int = 60_000):
        self.min_turn_ms = min_turn_ms
        self.state: dict[
            str, dict[str, Any]
        ] = {}  # instanceId -> {"busy": bool|None, "input": bool, "busy_since": ms|None}

    def observe(self, hosts: list[dict[str, Any]], now_ms: int) -> list[tuple[str, dict[str, Any]]]:
        alerts: list[tuple[str, dict[str, Any]]] = []
        seen = set()
        for h in hosts:
            key = h["instanceId"]
            seen.add(key)
            busy, needs = h.get("busy"), bool(h.get("inputRequired"))
            prev = self.state.get(key)
            if prev is None:
                self.state[key] = {"busy": busy, "input": needs, "busy_since": now_ms if busy else None}
                continue
            if needs and not prev["input"]:
                alerts.append(("needs_input", h))
            if prev["busy"] is True and busy is False:
                ran = now_ms - (prev["busy_since"] or now_ms)
                if ran >= self.min_turn_ms and not needs:
                    alerts.append(("finished", h))
            if busy and not prev["busy"]:
                prev["busy_since"] = now_ms
            elif not busy:
                prev["busy_since"] = None
            prev["busy"], prev["input"] = busy, needs
        for gone in set(self.state) - seen:
            del self.state[gone]
        return alerts


def project_name(h: dict[str, Any]) -> str:
    return Path(h.get("cwd") or "").name or "omp session"


def alert_message(kind: str, h: dict[str, Any]) -> tuple[str, str, int]:
    """(title, body, priority). Project folder and state only."""
    model = h.get("model")
    model = model.get("id") if isinstance(model, dict) else None
    suffix = f" · {model}" if isinstance(model, str) and model else ""
    if kind == "needs_input":
        return f"{project_name(h)} needs you", f"Waiting for your answer{suffix}", 4
    return f"{project_name(h)} finished", f"Turn done{suffix}", 3


def ntfy_payload(topic: str, title: str, body: str, priority: int = 3, click: str | None = None) -> dict[str, Any]:
    """ntfy's JSON publish format: UTF-8 safe, unlike its Title header (Latin-1 only)."""
    payload: dict[str, Any] = {"topic": topic, "title": title, "message": body, "priority": priority}
    if click:
        payload["click"] = click
    return payload


def _tls_context() -> ssl.SSLContext:
    """python.org builds on macOS ship without system CA certificates; prefer certifi's bundle when it is installed."""
    try:
        import certifi
    except ImportError:
        return ssl.create_default_context()
    return ssl.create_default_context(cafile=certifi.where())


def ntfy_send(
    server: str, topic: str, title: str, body: str, priority: int = 3, click: str | None = None, timeout: float = 10.0
) -> int:
    """One POST to the ntfy server within `timeout` seconds IN TOTAL (not per read: a server that answers a byte at a time cannot
    hold the watcher). No proxy, no redirect (a 3xx is an error: the message is never re-sent somewhere else), at most
    MAX_ANSWER_BYTES of answer are read, and any status outside 2xx is an OSError."""
    parts = urlsplit(server)
    host, https = parts.hostname or "", parts.scheme == "https"
    port = parts.port or (443 if https else 80)
    conn = (
        http.client.HTTPSConnection(host, port, timeout=timeout)
        if https
        else http.client.HTTPConnection(host, port, timeout=timeout)
    )
    data = json.dumps(ntfy_payload(topic, title, body, priority, click)).encode()
    deadline = Deadline(timeout)
    try:
        connect_bounded(conn, host, port, deadline, _tls_context() if https else None)
        conn.request(
            "POST", (parts.path.rstrip("/") or "") + "/", body=data, headers={"Content-Type": "application/json"}
        )
        response = conn.getresponse()
        response.read(MAX_ANSWER_BYTES)
        deadline.check()
        if not 200 <= response.status < 300:
            raise OSError(f"the ntfy server answered HTTP {response.status}")
        return response.status
    except http.client.HTTPException as exc:
        raise OSError(f"the ntfy server's answer was not usable ({type(exc).__name__})") from None
    finally:
        deadline.cancel()
        conn.close()


def ntfy_send_retrying(
    *args: Any,
    attempts: int = 3,
    backoff_s: tuple[float, ...] = (2, 5),
    sleep: Callable[[float], None] = time.sleep,
    send: Callable[..., int] | None = None,
    **kwargs: Any,
) -> int:
    """Connections to ntfy.sh reset now and then; retry before giving up."""
    send = send or ntfy_send
    for attempt in range(attempts):
        try:
            return send(*args, **kwargs)
        except OSError:  # URLError, HTTPError, connection resets and timeouts are all OSError
            if attempt == attempts - 1:
                raise
            sleep(backoff_s[min(attempt, len(backoff_s) - 1)])
    raise AssertionError("unreachable")


class Alerts:
    """The alert watcher and what the Alerts page shows."""

    def __init__(
        self,
        server: str,
        topic_file: Path,
        min_turn_seconds: int,
        list_hosts: Callable[[], list[dict[str, Any]]],
        click_url: Callable[[], str | None],
        send: Callable[..., int] = ntfy_send_retrying,
    ):
        self.server = server
        self.topic_file = topic_file
        self.tracker = AlertTracker(min_turn_seconds * 1000)
        self.list_hosts = list_hosts
        self.click_url = click_url
        self.send = send
        self.lock = threading.Lock()
        self.counters: dict[str, Any] = {"sent": 0, "last_sent_at": None, "last_error": None}
        self._enabled = False

    def _topic(self) -> str | None:
        try:
            topic = read_topic(self.topic_file)
        except DashboardError as exc:
            with self.lock:
                self.counters["last_error"] = f"the topic file cannot be used: {printable(str(exc), 200)}"
            return None
        self._enabled = topic is not None
        return topic

    def status(self) -> dict[str, Any]:
        """What the page shows: never the topic itself."""
        try:
            topic = read_topic(self.topic_file)
            problem = None
        except DashboardError as exc:
            topic, problem = None, f"the topic file cannot be used: {printable(str(exc), 200)}"
        with self.lock:
            counters = dict(self.counters)
        if problem:
            counters["last_error"] = problem
        return {"enabled": topic is not None, "server": self.server, **counters}

    def poll_once(self, now_ms: int | None = None) -> None:
        """One watcher round. Does nothing, and asks omp nothing, while no topic is set."""
        topic = self._topic()
        if topic is None:
            if self.tracker.state:
                self.tracker.state.clear()
            return
        try:
            pending = self.tracker.observe(self.list_hosts(), int(time.time() * 1000) if now_ms is None else now_ms)
        except (DashboardError, OSError, ValueError) as exc:
            with self.lock:
                self.counters["last_error"] = f"watching sessions: {type(exc).__name__}"
            return
        for kind, host in pending:
            title, body, priority = alert_message(kind, host)
            try:
                self.send(self.server, topic, title, body, priority, click=self.click_url())
            except OSError as exc:  # stays on the page until a later alert goes through
                with self.lock:
                    self.counters["last_error"] = f"sending alert: {type(exc).__name__}"
                continue
            with self.lock:
                self.counters.update(
                    sent=self.counters["sent"] + 1, last_sent_at=int(time.time() * 1000), last_error=None
                )

    def send_test(self) -> None:
        topic = read_topic(self.topic_file)
        if topic is None:
            raise DashboardError("no ntfy topic is set: run `omp-dashboard config set-ntfy-topic`")
        self.send(self.server, topic, "omp dashboard", "Test alert: phone alerts work.", click=self.click_url())

    def run_forever(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                self.poll_once()
            except Exception as exc:  # never let the watcher thread die silently
                with self.lock:
                    self.counters["last_error"] = f"watcher: {type(exc).__name__}"
            stop.wait(POLL_S)
