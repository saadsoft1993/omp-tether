"""Every byte omp-memory prints passes through one place, `omp_memory/sink.py`, which withholds what carries a secret.

This test reads every file of the plugin that is executable code (the package, the Python entry point, the maintainer's
scripts and the shell launcher) and fails when one prints, writes to a standard stream, lets a child process or argparse
write to the terminal, or reports an uncaught error in any other way. A new `print(` in a command module must be written
as a call of the sink instead; the bootstrap files, which run before the sink can be loaded, may only print fixed text."""

from __future__ import annotations

import ast
import re
import unittest

from support import LAUNCHER, PLUGIN, PYTHON_DIR

PACKAGE = PYTHON_DIR / "omp_memory"
ENTRY = PYTHON_DIR / "omp_memory_main.py"
SCRIPTS = PLUGIN / "scripts"
SINK = "sink.py"

# Names whose use would send bytes to the user's terminal without the sink.
STREAM_ATTRS = {"stdout", "stderr", "__stdout__", "__stderr__", "excepthook"}
TRACEBACK_CALLS = {"print_exc", "print_exception", "print_stack", "print_tb", "print_last"}
EXEC_CALLS = {
    "system",
    "execv",
    "execve",
    "execvp",
    "execvpe",
    "execl",
    "execle",
    "execlp",
    "execlpe",
    "popen",
    "write",
}
SUBPROCESS_CALLS = {"run", "Popen", "call", "check_call", "check_output"}
# Narrow allow-list: (file, reason). Nothing is listed on purpose: a case needs a comment saying why it cannot use the sink.
ALLOWED: dict[str, str] = {}


def is_str_like(node: ast.AST | None) -> bool:
    return isinstance(node, ast.JoinedStr) or (isinstance(node, ast.Constant) and isinstance(node.value, str))


def keyword_names(call: ast.Call) -> set[str]:
    return {kw.arg for kw in call.keywords if kw.arg}


def is_constant_text(node: ast.AST | None) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, str)


# Names a maintainer script may interpolate into a message: a counter, and a manifest-relative file name it loops over.
SCRIPT_NAMES = {"rel", "line"}


def is_static_text(node: ast.AST) -> bool:
    """Fixed text, or an f-string whose only placeholders are `len(...)` counts and the names in SCRIPT_NAMES."""
    if is_constant_text(node):
        return True
    if isinstance(node, ast.JoinedStr):
        for part in node.values:
            if isinstance(part, ast.FormattedValue):
                value = part.value
                counted = isinstance(value, ast.Call) and isinstance(value.func, ast.Name) and value.func.id == "len"
                named = isinstance(value, ast.Name) and value.id in SCRIPT_NAMES
                if not (counted or named):
                    return False
        return True
    return isinstance(node, ast.Name) and node.id in SCRIPT_NAMES


def captured(call: ast.Call) -> bool:
    """Both stdout and stderr of a child are taken by this process (the values, not just the keywords): capture_output=True, or
    each of stdout and stderr set to subprocess.PIPE or subprocess.DEVNULL (stderr may also be subprocess.STDOUT)."""
    values = {kw.arg: kw.value for kw in call.keywords if kw.arg}
    flag = values.get("capture_output")
    if isinstance(flag, ast.Constant) and flag.value is True:
        return True

    def named(node: ast.AST | None, allowed: set[str]) -> bool:
        return (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "subprocess"
            and node.attr in allowed
        )

    return named(values.get("stdout"), {"PIPE", "DEVNULL"}) and named(
        values.get("stderr"), {"PIPE", "DEVNULL", "STDOUT"}
    )


def problems_in(path, mode: str = "package") -> list[str]:
    """What in `path` would print or run a child without the sink. mode: `package` (nothing may print), `entry` (the bootstrap:
    only `sys.stderr.write` of a fixed string and a numeric exit), `script` (a maintainer tool: print of fixed text or counts)."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[str] = []

    def add(node: ast.AST, what: str) -> None:
        found.append(f"{path.name}:{node.lineno}: {what}")

    stderr_writes = {
        id(node.func.value)
        for node in ast.walk(tree)
        if mode == "entry"
        and isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "write"
        and isinstance(node.func.value, ast.Attribute)
        and node.func.value.attr == "stderr"
        and len(node.args) == 1
        and is_constant_text(node.args[0])
    }
    printed_to = {
        id(kw.value)
        for node in ast.walk(tree)
        if mode == "script" and isinstance(node, ast.Call) and getattr(node.func, "id", None) == "print"
        for kw in node.keywords
        if kw.arg == "file"
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Import | ast.ImportFrom):
            names = [alias.name for alias in node.names]
            module = node.module if isinstance(node, ast.ImportFrom) else None
            if "logging" in names or (module or "").split(".")[0] == "logging":
                add(node, "imports logging")
            if module == "sys" and STREAM_ATTRS & set(names):
                add(node, "imports a standard stream from sys")
            if module == "traceback":
                add(node, "imports from traceback")
        elif isinstance(node, ast.Attribute):
            if (
                isinstance(node.value, ast.Name)
                and node.value.id == "sys"
                and node.attr in STREAM_ATTRS
                and id(node) not in stderr_writes
                and id(node) not in printed_to
            ):
                add(node, f"uses sys.{node.attr}")
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id == "print" and mode == "script":
                if not all(is_static_text(arg) for arg in node.args):
                    add(node, "print() of something other than fixed text, a count or a manifest file name")
            elif isinstance(func, ast.Name) and func.id in ("print", "input", "exit", "quit", "breakpoint"):
                add(node, f"calls {func.id}()")
            if isinstance(func, ast.Name) and func.id == "SystemExit" and node.args and is_str_like(node.args[0]):
                add(node, "SystemExit with a message (it is printed to stderr as written)")
            if isinstance(func, ast.Attribute):
                base = func.value.id if isinstance(func.value, ast.Name) else None
                if base == "sys" and func.attr == "exit" and node.args and is_str_like(node.args[0]):
                    add(node, "sys.exit with a message (it is printed to stderr as written)")
                if base == "traceback" and func.attr in TRACEBACK_CALLS:
                    add(node, f"traceback.{func.attr}")
                if base == "os" and func.attr in EXEC_CALLS:
                    add(node, f"os.{func.attr}")
                if base == "argparse" and func.attr == "ArgumentParser" and mode != "script":
                    add(node, "argparse.ArgumentParser prints usage and errors itself; use the sink's ArgumentParser")
                if base == "logging":
                    add(node, "uses logging")
                if base == "subprocess" and func.attr in SUBPROCESS_CALLS and not captured(node):
                    add(
                        node,
                        f"subprocess.{func.attr} without stdout and stderr both captured (capture_output=True, or PIPE or DEVNULL for each): "
                        "the child would write to the terminal",
                    )
                if mode == "entry" and func.attr == "write" and id(func.value) not in stderr_writes:
                    add(node, "a write that is not sys.stderr.write of a fixed string")
    return found


ECHO = re.compile(r"(?:^|[\s;{(&|])(?:echo|printf)\b(?P<rest>.*)$")
HEREDOC = re.compile(r"<<-?\s*(?P<quote>['\"]?)(?P<tag>\w+)(?P=quote)")


def shell_problems(text: str) -> list[str]:
    """In a shell launcher: an echo or printf whose text expands a variable or a command (it could print a path or a value
    the user gave), and an unquoted here-document that expands anything."""
    found = []
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        number = index + 1
        index += 1
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        for match in ECHO.finditer(line):
            first = re.split(r";|\|\||&&|\}", match.group("rest"))[0]
            if "$" in first or "`" in first:
                found.append(f"{number}: echo or printf expands something: {stripped}")
        heredoc = HEREDOC.search(line)
        if heredoc:
            body = []
            while index < len(lines) and lines[index].strip() != heredoc.group("tag"):
                body.append(lines[index])
                index += 1
            index += 1
            if not heredoc.group("quote") and any("$" in row or "`" in row for row in body):
                found.append(f"{number}: an unquoted here-document expands something")
    return found


class OutputSinkLintTest(unittest.TestCase):
    def test_the_sink_module_exists(self):
        self.assertTrue((PACKAGE / SINK).is_file(), "omp_memory/sink.py is the one place that prints")

    def test_no_module_but_the_sink_prints_or_lets_anything_write_to_the_terminal(self):
        problems: list[str] = []
        for path in sorted(PACKAGE.glob("*.py")):
            if path.name == SINK or path.name in ALLOWED:
                continue
            problems += problems_in(path)
        self.assertEqual(problems, [], "\n" + "\n".join(problems))

    def test_the_python_entry_point_only_writes_fixed_text_to_stderr(self):
        problems = problems_in(ENTRY, "entry")
        self.assertEqual(problems, [], "\n" + "\n".join(problems))

    def test_the_maintainer_scripts_print_only_fixed_text_counts_and_file_names(self):
        scripts = sorted(SCRIPTS.glob("*.py"))
        self.assertTrue(scripts)
        problems: list[str] = []
        for path in scripts:
            problems += problems_in(path, "script")
        self.assertEqual(problems, [], "\n" + "\n".join(problems))

    def test_every_python_file_the_plugin_ships_is_scanned(self):
        shipped = {
            p.name
            for p in PLUGIN.rglob("*.py")
            if "vendor" not in p.relative_to(PLUGIN).parts and "tests" not in p.relative_to(PLUGIN).parts
        }
        scanned = {p.name for p in PACKAGE.glob("*.py")} | {ENTRY.name} | {p.name for p in SCRIPTS.glob("*.py")}
        self.assertEqual(shipped - scanned, set())

    def test_the_shell_launcher_never_echoes_a_variable_or_a_command_result(self):
        problems = shell_problems(LAUNCHER.read_text(encoding="utf-8"))
        self.assertEqual(problems, [], "\n" + "\n".join(problems))

    def test_the_shell_lint_catches_what_it_is_for(self):
        leaky = (
            "#!/bin/sh\n"
            'echo "found $main" >&2\n'
            "printf '%s' \"$OMP_MEMORY_PYTHON\" >&2\n"
            '[ -f x ] || { echo "cannot find $(pwd)" >&2; exit 2; }\n'
            "cat >&2 <<EOF\nthe path is $here\nEOF\n"
        )
        self.assertEqual(len(shell_problems(leaky)), 4)
        fine = (
            'echo "fixed text" >&2; exit "$code"\n'
            "cat >&2 <<'EOF'\nneeds $nothing expanded\nEOF\n"
            "# echo $in_a_comment\n"
        )
        self.assertEqual(shell_problems(fine), [])

    def test_the_subprocess_check_looks_at_the_values(self):
        import tempfile
        from pathlib import Path

        cases = {
            "subprocess.run(['x'], capture_output=True)": 0,
            "subprocess.Popen(['x'], stdout=subprocess.PIPE, stderr=subprocess.PIPE)": 0,
            "subprocess.run(['x'], stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)": 0,
            "subprocess.run(['x'], capture_output=False)": 1,
            "subprocess.run(['x'], stdout=None, stderr=None)": 1,
            "subprocess.run(['x'], stdout=sys_out, stderr=subprocess.PIPE)": 1,
            "subprocess.run(['x'], stdout=subprocess.PIPE)": 1,
        }
        with tempfile.TemporaryDirectory() as tmp:
            for call, expected in cases.items():
                path = Path(tmp) / "case.py"
                path.write_text(f"import subprocess\n{call}\n")
                self.assertEqual(len(problems_in(path)), expected, call)

    def test_the_lint_itself_catches_each_kind_of_leak(self):
        import tempfile
        from pathlib import Path

        leaky = (
            "import sys, logging, traceback, subprocess, argparse, os\n"
            "print('x')\n"
            "sys.stderr.write('x')\n"
            "traceback.print_exc()\n"
            "subprocess.run(['x'])\n"
            "argparse.ArgumentParser()\n"
            "sys.exit('message')\n"
            "os.system('x')\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "leaky.py"
            path.write_text(leaky)
            found = " ".join(problems_in(path))
        for needle in (
            "logging",
            "print()",
            "sys.stderr",
            "traceback.print_exc",
            "subprocess.run",
            "ArgumentParser",
            "sys.exit",
            "os.system",
        ):
            self.assertIn(needle, found)


if __name__ == "__main__":
    unittest.main()
