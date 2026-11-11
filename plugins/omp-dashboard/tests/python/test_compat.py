"""The versions of omp this dashboard says it was tested with are stated in four places: `compat.py` (what `doctor` compares with),
`compat.json` and `docs/compat.md` at the repository root, and the README. One of them is the source (`compat.json`; the installed
plugin does not carry the repository root, so the code keeps its own copy) and these tests fail when a copy drifts."""

from __future__ import annotations

import json
import re
import unittest

from omp_dashboard.compat import MIN_OMP, TESTED_OMP
from support import PLUGIN

REPO = PLUGIN.parents[1]
COMPAT = json.loads((REPO / "compat.json").read_text())["plugins"]["omp-dashboard"]


def _versions(text: str) -> list[str]:
    return re.findall(r"18\.\d+\.\d+", text)


class CompatSources(unittest.TestCase):
    def test_the_code_lists_the_versions_compat_json_lists(self) -> None:
        self.assertEqual(list(TESTED_OMP), COMPAT["ompTested"])

    def test_the_minimum_is_the_one_in_compat_json(self) -> None:
        top = json.loads((REPO / "compat.json").read_text())["omp"]["minimum"]
        self.assertEqual(".".join(map(str, MIN_OMP)), top)

    def test_the_readme_names_exactly_those_versions(self) -> None:
        line = next(
            line
            for line in (PLUGIN / "README.md").read_text().splitlines()
            if line.startswith("Supported: macOS and Linux")
        )
        tested = re.search(r"\(tested on ([^)]*)\)", line)
        assert tested is not None, line
        self.assertEqual(_versions(tested.group(1)), COMPAT["ompTested"])

    def test_the_compat_page_names_exactly_those_versions(self) -> None:
        page = (REPO / "docs" / "compat.md").read_text()
        section = page.split("### `omp-dashboard`")[1].split("###")[0]
        tested = re.search(r"Tested with omp ((?:\d+\.\d+\.\d+(?: and )?)+)", section)
        assert tested is not None, section
        self.assertEqual(_versions(tested.group(0)), COMPAT["ompTested"])


if __name__ == "__main__":
    unittest.main()
