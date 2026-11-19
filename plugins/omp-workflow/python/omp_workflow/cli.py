"""Command line for installing and removing workflow settings."""

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import files, settings


def run_omp(*args, check=True):
    result = subprocess.run(["omp", *args], capture_output=True, text=True)
    if check and result.returncode:
        raise RuntimeError(f"omp {' '.join(args)}: {result.stderr.strip()}")
    return result


def global_values():
    raw = json.loads(run_omp("config", "list", "--scope", "global", "--json").stdout)
    return {key: entry["value"] for key, entry in raw.items() if "value" in entry}


def value_arg(value):
    return value if isinstance(value, str) else json.dumps(value, separators=(",", ":"))


def state_path():
    root = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(root) / "omp-workflow" / "state.json"


def load_state(agent):
    path = state_path()
    if not path.exists():
        return {"agent_dir": str(agent), "keys": {}, "files": {}, "backups": []}
    state = json.loads(path.read_text())
    if state["agent_dir"] != str(agent):
        raise ValueError("state belongs to a different omp config directory")
    return state


def save_state(state):
    path = state_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    temp = path.with_name("state.json.tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(state, handle, indent=2)
        handle.write("\n")
    temp.chmod(0o600)
    temp.replace(path)


def backup_config(agent):
    config = agent / "config.yml"
    if not config.exists():
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = agent / f"config.yml.bak-omp-workflow-{stamp}"
    suffix = 0
    while True:
        candidate = path if not suffix else path.with_name(f"{path.name}-{suffix}")
        try:
            with candidate.open("xb") as target, config.open("rb") as source:
                shutil.copyfileobj(source, target)
            shutil.copymode(config, candidate)
            return str(candidate)
        except FileExistsError:
            suffix += 1


def confirm(yes):
    if yes:
        return True
    try:
        return input("Apply? [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def prepare(args, current):
    desired = dict(settings.CORE)
    if not args.no_advisor:
        desired.update(settings.ADVISOR)
    if args.display:
        desired.update(settings.DISPLAY)
    if args.models != "none":
        data = json.loads(run_omp("models", "--json").stdout)
        available = {item.get("selector", f"{item['provider']}/{item['id']}") for item in data["models"]}
        roles = settings.role_values(args.models, available, not args.no_advisor)
        merged = settings.merge_record("modelRoles", current, roles, args.overwrite)
        if roles:
            desired["modelRoles"] = merged
        if args.models == "anthropic-codex":
            desired.update({"retry.fallbackOn": "usage-limit", "retry.usageReservePolicy": "auto"})
            fallbacks = {
                role: [model for model in chain if model.rsplit(":", 1)[0] in available]
                for role, chain in settings.FALLBACKS.items()
            }
            fallbacks = {role: chain for role, chain in fallbacks.items() if chain}
            if fallbacks:
                desired["retry.fallbackChains"] = settings.merge_record(
                    "retry.fallbackChains", current, fallbacks, args.overwrite
                )
        overrides = {key: value for key, value in settings.OVERRIDES.items() if value[1:] in merged}
        if overrides:
            desired["task.agentModelOverrides"] = settings.merge_record(
                "task.agentModelOverrides", current, overrides, args.overwrite
            )
    records = {"modelRoles", "retry.fallbackChains", "task.agentModelOverrides"}
    plan = []
    for key, value in desired.items():
        if key in current and key not in records and not args.overwrite:
            print(f"kept yours: {key}")
        elif key not in current or current[key] != value:
            plan.append((key, value))
    return plan


def init(args, agent, state):
    current = global_values()
    plan = prepare(args, current)
    operations = files.plan_files(agent, not args.no_advisor, state["files"])
    for key, value in plan:
        print(f"set {key}: {json.dumps(value, sort_keys=True)}")
    for rel, _, kind, _ in operations:
        print(f"write {rel}" + (" (managed block)" if kind == "block" else ""))
    if args.dry_run:
        print("dry run: no changes made")
        return 0
    if not confirm(args.yes):
        print("not confirmed; no changes made")
        return 1
    if not plan and not operations:
        return 0
    backup = backup_config(agent)
    if backup:
        state.setdefault("backups", []).append(backup)
    save_state(state)
    for key, value in plan:
        if key == "task.agentModelOverrides":
            roles = current.get("modelRoles", {})
            additions = {name: role for name, role in settings.OVERRIDES.items() if role[1:] in roles}
            value = settings.merge_record(key, current, additions, args.overwrite)
            if (key not in current and not value) or current.get(key) == value:
                continue
        result = run_omp("config", "set", key, value_arg(value), check=False)
        if result.returncode:
            if "Unknown setting" in result.stderr:
                print(f"skipped {key}: not known to this omp")
                continue
            raise RuntimeError(f"failed to set {key}: {result.stderr.strip()}")
        if key not in state["keys"]:
            state["keys"][key] = {"was_present": key in current, "prior": current.get(key)}
        state["keys"][key]["written"] = value
        save_state(state)
        current = global_values()
        if key not in current:
            raise RuntimeError(f"setting {key} is missing after omp reported success")
        state["keys"][key]["written"] = current[key]
        save_state(state)
    for rel, content, kind, managed in operations:
        path = agent / rel
        previous = state["files"].get(rel, {})
        separator = previous.get("separator", False)
        if kind == "block" and path.exists():
            original = path.read_bytes()
            if files.block_bounds(original) is None:
                separator = bool(original and not original.endswith(b"\n"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        state["files"][rel] = {"kind": kind, "sha256": files.digest(managed)}
        if kind == "block":
            state["files"][rel]["separator"] = separator
        save_state(state)
    return 0


def status(agent, state):
    current = global_values()
    for key, record in state["keys"].items():
        outcome = (
            "missing"
            if key not in current
            else "unchanged"
            if current[key] == record["written"]
            else "changed since init"
        )
        print(f"{key}: {outcome}")
    for rel, record in state["files"].items():
        content = files.current_content(agent / rel, record["kind"])
        outcome = (
            "missing"
            if content is None
            else "unchanged"
            if files.digest(content) == record["sha256"]
            else "changed since init"
        )
        print(f"{rel}: {outcome}")
    return 0


def uninstall(args, agent, state):
    if not confirm(args.yes):
        print("not confirmed; no changes made")
        return 1
    current = global_values()
    for key, record in list(state["keys"].items()):
        if key not in current or current[key] != record["written"]:
            print(f"changed since init, left: {key}")
        else:
            command = ("set", key, value_arg(record["prior"])) if record["was_present"] else ("reset", key)
            run_omp("config", *command)
            print(f"restored {key}")
        del state["keys"][key]
        save_state(state)
    for rel, record in list(state["files"].items()):
        path = agent / rel
        content = files.current_content(path, record["kind"])
        if content is None or files.digest(content) != record["sha256"]:
            print(f"changed since init, left: {rel}")
        elif record["kind"] == "block":
            data = path.read_bytes()
            start, end = files.block_bounds(data)
            if record.get("separator") and data[max(0, start - 1) : start] == b"\n":
                start -= 1
            path.write_bytes(data[:start] + data[end:])
            print(f"removed block: {rel}")
        else:
            path.unlink()
            print(f"removed {rel}")
            parent = path.parent
            while parent != agent and parent != agent.parent:
                try:
                    parent.rmdir()
                except OSError:
                    break
                parent = parent.parent
        del state["files"][rel]
        save_state(state)
    state_path().unlink(missing_ok=True)
    return 0


def main():
    parser = argparse.ArgumentParser(prog="omp-workflow")
    sub = parser.add_subparsers(dest="command", required=True)
    setup = sub.add_parser("init")
    setup.add_argument("--models", choices=("anthropic", "anthropic-codex", "none"), default="none")
    for flag in ("display", "no-advisor", "overwrite", "yes", "dry-run"):
        setup.add_argument("--" + flag, action="store_true")
    sub.add_parser("status")
    sub.add_parser("uninstall").add_argument("--yes", action="store_true")
    args = parser.parse_args()
    if shutil.which("omp") is None:
        print("omp-workflow: omp not found on PATH", file=sys.stderr)
        return 2
    try:
        agent = Path(run_omp("config", "path").stdout.strip()).expanduser().resolve()
        state = load_state(agent)
        if args.command == "init":
            return init(args, agent, state)
        if args.command == "status":
            return status(agent, state)
        return uninstall(args, agent, state)
    except (OSError, ValueError, KeyError, RuntimeError) as exc:
        print(f"omp-workflow: {exc}", file=sys.stderr)
        return 1
