"""Exercise the launcher with an isolated fake omp process."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[2]
LAUNCHER = PLUGIN / "bin" / "omp-workflow"
MODELS = [
    "anthropic/claude-opus-5-5",
    "anthropic/claude-sonnet-5-5",
    "anthropic/claude-fable-5-2",
    "openai-codex/gpt-6.1-terra",
    "openai-codex/gpt-6.1-sol",
    "openai-codex/gpt-6-astra",
]
KNOWN = [
    "defaultThinkingLevel",
    "steeringMode",
    "plan.defaultOnStartup",
    "plan.autosave",
    "edit.mode",
    "task.eager",
    "memory.backend",
    "mnemopi.scoping",
    "autolearn.enabled",
    "autolearn.autoContinue",
    "contextPromotion.enabled",
    "extendedContext",
    "branchSummary.enabled",
    "advisor.enabled",
    "advisor.syncBacklog",
    "advisor.continueSeverity",
    "advisor.reviewMode",
    "tier.advisor",
    "modelRoles",
    "task.agentModelOverrides",
    "retry.fallbackOn",
    "retry.usageReservePolicy",
    "retry.fallbackChains",
    "composer.shape",
    "composer.tokenRate",
    "statusLine.preset",
    "statusLine.separator",
    "statusLine.contextLine",
    "statusLine.sessionAccent",
    "statusLine.showHookStatus",
    "display.subagentLivePreview",
    "display.pinnedAgents",
    "display.showTurnTime",
    "display.showTokenUsage",
    "title.icons",
    "title.generator",
]
FAKE = r"""#!PYTHON
import json, os, sys
from pathlib import Path
root = Path(os.environ["FAKE_ROOT"])
config = root / "values.json"
values = json.loads(config.read_text()) if config.exists() else {}
args = sys.argv[1:]
if args == ["models", "--json"]:
    models = json.loads((root / "models.json").read_text())
    entries = [{"provider": s.split("/", 1)[0], "id": s.split("/", 1)[1], "selector": s} for s in models]
    print(json.dumps({"models": entries}))
elif args == ["config", "path"]:
    print(root / "agent")
elif args == ["config", "list", "--scope", "global", "--json"]:
    print(json.dumps({k: {"value": v, "type": "test", "description": ""} for k, v in values.items()}))
elif len(args) >= 3 and args[:2] == ["config", "get"]:
    key = args[2]
    if key not in json.loads((root / "known.json").read_text()):
        print("Unknown setting: " + key, file=sys.stderr); sys.exit(1)
    print(json.dumps({"key": key, "value": values.get(key)}))
elif len(args) >= 3 and args[0] == "config" and args[1] in ("set", "reset"):
    key = args[2]
    unknown = json.loads((root / "unknown.json").read_text())
    if key in unknown or key not in json.loads((root / "known.json").read_text()):
        print("Unknown setting: " + key, file=sys.stderr); sys.exit(1)
    failures = json.loads((root / "failures.json").read_text())
    if key in failures:
        print("deliberate error", file=sys.stderr); sys.exit(1)
    if args[1] == "reset":
        values.pop(key, None)
    else:
        try: values[key] = json.loads(args[3])
        except json.JSONDecodeError: values[key] = args[3]
        if key == "advisor.syncBacklog":
            values[key] = str(values[key])
    config.write_text(json.dumps(values))
else:
    print("unexpected command: " + repr(args), file=sys.stderr); sys.exit(1)
"""


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.agent = self.root / "agent"
        self.agent.mkdir()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        omp = self.bin / "omp"
        omp.write_text(FAKE.replace("#!PYTHON", "#!" + sys.executable))
        omp.chmod(0o755)
        self.write_json("models.json", MODELS)
        self.write_json("known.json", KNOWN)
        self.write_json("unknown.json", [])
        self.write_json("failures.json", [])
        self.env = {
            **os.environ,
            "HOME": str(self.root),
            "XDG_STATE_HOME": str(self.root / "state"),
            "FAKE_ROOT": str(self.root),
            "PATH": str(self.bin) + os.pathsep + os.environ.get("PATH", ""),
            "OMP_WORKFLOW_PYTHON": sys.executable,
        }

    def write_json(self, name, value):
        (self.root / name).write_text(json.dumps(value))

    def values(self):
        path = self.root / "values.json"
        return json.loads(path.read_text()) if path.exists() else {}

    def run_cli(self, *args, expected=0):
        result = subprocess.run([str(LAUNCHER), *args], env=self.env, text=True, capture_output=True)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return result

    def test_empty_init_status_and_permissions(self):
        (self.agent / "config.yml").write_text("original\n")
        self.run_cli("init", "--models", "anthropic", "--yes")
        self.assertEqual(self.values()["plan.defaultOnStartup"], True)
        self.assertEqual(self.values()["modelRoles"]["default"], "anthropic/claude-opus-5-5:medium")
        self.assertTrue((self.agent / "RULES.md").exists())
        self.assertTrue((self.agent / "WATCHDOG.yml").exists())
        self.assertEqual(len(list(self.agent.glob("config.yml.bak-omp-workflow-*"))), 1)
        state = self.root / "state" / "omp-workflow" / "state.json"
        self.assertEqual(state.stat().st_mode & 0o777, 0o600)
        self.assertEqual(state.parent.stat().st_mode & 0o777, 0o700)
        self.assertIn("RULES.md: unchanged", self.run_cli("status").stdout)

    def test_normalized_setting_is_recorded_and_removed(self):
        self.run_cli("init", "--yes")
        self.assertEqual(self.values()["advisor.syncBacklog"], "3")
        self.assertIn("advisor.syncBacklog: unchanged", self.run_cli("status").stdout)
        self.run_cli("uninstall", "--yes")
        self.assertNotIn("advisor.syncBacklog", self.values())

    def test_scalar_kept_overwritten_and_restored_across_init(self):
        self.write_json("values.json", {"steeringMode": "user"})
        self.assertIn("kept yours: steeringMode", self.run_cli("init", "--yes").stdout)
        self.assertEqual(self.values()["steeringMode"], "user")
        self.run_cli("init", "--overwrite", "--yes")
        self.assertEqual(self.values()["steeringMode"], "one-at-a-time")
        self.run_cli("uninstall", "--yes")
        self.assertEqual(self.values(), {"steeringMode": "user"})

    def test_roles_merge_and_missing_overrides(self):
        self.write_json("values.json", {"modelRoles": {"default": "custom/model", "mine": "custom/other"}})
        self.write_json("models.json", ["anthropic/claude-opus-5-5"])
        result = self.run_cli("init", "--models", "anthropic", "--yes")
        self.assertIn("skipped role exec_fast", result.stdout)
        roles = self.values()["modelRoles"]
        self.assertEqual(roles["default"], "custom/model")
        self.assertEqual(roles["mine"], "custom/other")
        self.assertEqual(roles["plan_verifier"], "anthropic/claude-opus-5-5:high")
        self.assertEqual(
            self.values()["task.agentModelOverrides"],
            {"plan-verifier": "@plan_verifier", "code-reviewer": "@code_review"},
        )

    def test_codex_model_candidates_and_filtered_fallbacks(self):
        self.write_json("models.json", ["openai-codex/gpt-6.1-sol"])
        self.write_json("values.json", {"retry.fallbackChains": {"smol": ["user/model"], "default": ["user/default"]}})
        self.run_cli("init", "--models", "anthropic-codex", "--yes")
        self.assertEqual(self.values()["modelRoles"]["exec_strong"], "openai-codex/gpt-6.1-sol:medium")
        self.assertEqual(
            self.values()["retry.fallbackChains"],
            {"smol": ["user/model"], "default": ["user/default"], "plan": ["openai-codex/gpt-6.1-sol:high"]},
        )
        self.run_cli("init", "--models", "anthropic-codex", "--overwrite", "--yes")
        self.assertEqual(
            self.values()["retry.fallbackChains"],
            {
                "smol": ["user/model"],
                "default": ["openai-codex/gpt-6.1-sol:medium"],
                "plan": ["openai-codex/gpt-6.1-sol:high"],
            },
        )

    def test_codex_prefers_terra_and_omits_empty_fallbacks(self):
        self.write_json("models.json", ["openai-codex/gpt-6.1-terra", "openai-codex/gpt-6.1-sol"])
        self.run_cli("init", "--models", "anthropic-codex", "--yes")
        self.assertEqual(self.values()["modelRoles"]["exec_strong"], "openai-codex/gpt-6.1-terra:medium")
        self.assertNotIn("plan_verifier", self.values()["retry.fallbackChains"])

    def test_codex_with_no_available_fallbacks_does_not_write_record(self):
        self.write_json("models.json", ["anthropic/claude-opus-5-5"])
        result = self.run_cli("init", "--models", "anthropic-codex", "--yes")
        self.assertIn("skipped role exec_strong", result.stdout)
        self.assertNotIn("retry.fallbackChains", self.values())

    def test_unknown_key_and_non_unknown_failure_records_prior_writes(self):
        self.write_json("unknown.json", ["plan.autosave"])
        self.write_json("failures.json", ["edit.mode"])
        result = self.run_cli("init", "--yes", expected=1)
        self.assertIn("skipped plan.autosave: not known to this omp", result.stdout)
        self.assertIn("failed to set edit.mode: deliberate error", result.stderr)
        self.assertIn("defaultThinkingLevel", self.values())
        self.write_json("failures.json", [])
        self.run_cli("uninstall", "--yes")
        self.assertEqual(self.values(), {})

    def test_unknown_model_roles_never_writes_dangling_overrides(self):
        self.write_json("unknown.json", ["modelRoles"])
        self.run_cli("init", "--models", "anthropic", "--yes")
        self.assertNotIn("task.agentModelOverrides", self.values())

    def test_rules_append_replace_and_uninstall(self):
        rules = self.agent / "RULES.md"
        rules.write_text("personal\n")
        self.run_cli("init", "--yes")
        self.assertEqual(rules.read_text().count("<!-- omp-workflow:begin -->"), 1)
        rules.write_text(rules.read_text().replace("personal", "edited personal"))
        self.run_cli("init", "--yes")
        self.assertEqual(rules.read_text().count("<!-- omp-workflow:begin -->"), 1)
        self.run_cli("uninstall", "--yes")
        self.assertEqual(rules.read_text(), "edited personal\n")

    def test_uninstall_leaves_changed_items(self):
        self.run_cli("init", "--yes")
        values = self.values()
        values["steeringMode"] = "changed"
        self.write_json("values.json", values)
        rules = self.agent / "RULES.md"
        rules.write_text("changed")
        result = self.run_cli("uninstall", "--yes")
        self.assertIn("changed since init, left: steeringMode", result.stdout)
        self.assertIn("changed since init, left: RULES.md", result.stdout)
        self.assertEqual(self.values(), {"steeringMode": "changed"})
        self.assertEqual(rules.read_text(), "changed")
        self.assertFalse((self.root / "state" / "omp-workflow" / "state.json").exists())

    def test_dry_run_writes_nothing(self):
        (self.agent / "config.yml").write_text("untouched")
        result = self.run_cli("init", "--models", "anthropic-codex", "--display", "--dry-run")
        self.assertIn("dry run", result.stdout)
        self.assertEqual(list(self.agent.iterdir()), [self.agent / "config.yml"])
        self.assertFalse((self.root / "state").exists())
        self.assertFalse((self.root / "values.json").exists())

    def test_no_advisor_and_no_model_listing(self):
        (self.root / "models.json").unlink()
        self.run_cli("init", "--no-advisor", "--yes")
        self.assertNotIn("advisor.enabled", self.values())
        self.assertFalse((self.agent / "WATCHDOG.yml").exists())

    def test_rules_without_trailing_newline_restored_exactly(self):
        rules = self.agent / "RULES.md"
        rules.write_text("personal")
        self.run_cli("init", "--yes")
        self.run_cli("init", "--yes")
        self.run_cli("uninstall", "--yes")
        self.assertEqual(rules.read_text(), "personal")

    def test_created_rules_remain_owned_after_repeated_init(self):
        self.run_cli("init", "--yes")
        self.run_cli("init", "--yes")
        self.run_cli("uninstall", "--yes")
        self.assertFalse((self.agent / "RULES.md").exists())

    def test_overwrite_merges_and_restores_records(self):
        prior = {"modelRoles": {"default": "custom/model", "mine": "custom/other"}}
        self.write_json("values.json", prior)
        self.run_cli("init", "--models", "anthropic", "--overwrite", "--yes")
        roles = self.values()["modelRoles"]
        self.assertEqual(roles["mine"], "custom/other")
        self.assertEqual(roles["default"], "anthropic/claude-opus-5-5:medium")
        self.assertEqual(roles["plan_verifier"], "anthropic/claude-fable-5-2:high")
        self.run_cli("uninstall", "--yes")
        self.assertEqual(self.values(), prior)

    def test_status_reports_changed_and_missing(self):
        self.run_cli("init", "--yes")
        values = self.values()
        values["steeringMode"] = "changed"
        del values["edit.mode"]
        self.write_json("values.json", values)
        (self.agent / "RULES.md").unlink()
        result = self.run_cli("status")
        self.assertIn("steeringMode: changed since init", result.stdout)
        self.assertIn("edit.mode: missing", result.stdout)
        self.assertIn("RULES.md: missing", result.stdout)


if __name__ == "__main__":
    unittest.main()
