"""Plan and identify managed files and the RULES block."""

import hashlib
from pathlib import Path

BEGIN = b"<!-- omp-workflow:begin -->"
END = b"<!-- omp-workflow:end -->"
TEMPLATES = Path(__file__).resolve().parents[2] / "templates"


def digest(data):
    return hashlib.sha256(data).hexdigest()


def block_bounds(data):
    if BEGIN not in data and END not in data:
        return None
    if data.count(BEGIN) != 1 or data.count(END) != 1:
        raise ValueError("RULES.md has incomplete or duplicate workflow markers")
    start = data.index(BEGIN)
    end = data.index(END) + len(END)
    if end <= start:
        raise ValueError("RULES.md has reversed workflow markers")
    if data[end : end + 1] == b"\n":
        end += 1
    return start, end


def rules_block(template):
    return BEGIN + b"\n" + template.rstrip(b"\n") + b"\n" + END + b"\n"


def plan_files(agent, advisor, old_files):
    operations = []
    rules = agent / "RULES.md"
    template = (TEMPLATES / "RULES.md").read_bytes()
    if not rules.exists():
        operations.append(("RULES.md", template, "file", template))
    else:
        data = rules.read_bytes()
        bounds = block_bounds(data)
        old = old_files.get("RULES.md", {})
        if old.get("kind") == "file":
            if old.get("sha256") == digest(data):
                print("unchanged: RULES.md")
            else:
                print("kept yours: RULES.md")
        else:
            block = rules_block(template)
            if bounds:
                content = data[: bounds[0]] + block + data[bounds[1] :]
            else:
                separator = b"" if not data or data.endswith(b"\n") else b"\n"
                content = data + separator + block
            if content == data:
                print("unchanged: RULES.md")
            else:
                operations.append(("RULES.md", content, "block", block))
    if advisor:
        watchdog = agent / "WATCHDOG.yml"
        if watchdog.exists():
            print("kept yours: WATCHDOG.yml")
        else:
            data = (TEMPLATES / "WATCHDOG.yml").read_bytes()
            operations.append(("WATCHDOG.yml", data, "file", data))
    for category in ("agents", "skills"):
        root = TEMPLATES / category
        if not root.is_dir():
            raise ValueError(f"missing templates: {root}")
        for entry in sorted(root.iterdir()):
            destination = agent / category / entry.name
            if destination.exists():
                print(f"kept yours: {category}/{entry.name}")
                continue
            sources = [entry] if entry.is_file() else sorted(p for p in entry.rglob("*") if p.is_file())
            for source in sources:
                rel = source.relative_to(TEMPLATES).as_posix()
                data = source.read_bytes()
                operations.append((rel, data, "file", data))
    return operations


def current_content(path, kind):
    if not path.is_file():
        return None
    data = path.read_bytes()
    if kind == "file":
        return data
    try:
        bounds = block_bounds(data)
    except ValueError:
        return data
    return None if bounds is None else data[bounds[0] : bounds[1]]
