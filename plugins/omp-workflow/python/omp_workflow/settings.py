"""Starter settings and model selection."""

CORE = {
    "defaultThinkingLevel": "auto",
    "steeringMode": "one-at-a-time",
    "plan.defaultOnStartup": True,
    "plan.autosave": False,
    "edit.mode": "hashline",
    "task.eager": "preferred",
    "memory.backend": "mnemopi",
    "mnemopi.scoping": "per-project",
    "autolearn.enabled": True,
    "autolearn.autoContinue": True,
    "contextPromotion.enabled": True,
    "extendedContext": True,
    "branchSummary.enabled": True,
}
ADVISOR = {
    "advisor.enabled": True,
    "advisor.syncBacklog": 3,
    "advisor.continueSeverity": "concern",
    "advisor.reviewMode": "agent-end",
    "tier.advisor": "auto",
}
DISPLAY = {
    "composer.shape": "box",
    "composer.tokenRate": True,
    "statusLine.preset": "full",
    "statusLine.separator": "powerline-thin",
    "statusLine.contextLine": "embedded",
    "statusLine.sessionAccent": True,
    "statusLine.showHookStatus": True,
    "display.subagentLivePreview": True,
    "display.pinnedAgents": "collapsed",
    "display.showTurnTime": True,
    "display.showTokenUsage": True,
    "title.icons": "nf+emoji",
    "title.generator": "tiny",
}
FALLBACKS = {
    "default": ["openai-codex/gpt-6.1-sol:medium"],
    "plan": ["openai-codex/gpt-6.1-sol:high"],
    "smol": ["openai-codex/gpt-6.1-terra:medium"],
    "exec_fast": ["openai-codex/gpt-6.1-terra:medium"],
    "plan_verifier": ["openai-codex/gpt-6-astra:high"],
}
OVERRIDES = {
    "sonic": "@exec_fast",
    "task": "@exec_strong",
    "plan-verifier": "@plan_verifier",
    "code-reviewer": "@code_review",
}


def role_values(profile, available, advisor):
    opus = "anthropic/claude-opus-5-5"
    sonnet = "anthropic/claude-sonnet-5-5"
    roles = {
        "default": opus + ":medium",
        "plan": opus + ":high",
        "smol": sonnet + ":medium",
        "exec_fast": sonnet + ":medium",
        "exec_strong": (sonnet + ":high" if profile == "anthropic" else "openai-codex/gpt-6.1-terra:medium"),
        "plan_verifier": "anthropic/claude-fable-5-2:high",
        "code_review": (opus + ":high" if profile == "anthropic" else "openai-codex/gpt-6.1-sol:high"),
    }
    if advisor:
        roles["advisor"] = sonnet + ":medium" if profile == "anthropic" else "openai-codex/gpt-6.1-sol:medium"
    result = {}
    for role, value in roles.items():
        if value.rsplit(":", 1)[0] in available:
            result[role] = value
        elif role == "plan_verifier" and opus in available:
            result[role] = opus + ":high"
        elif role == "exec_strong" and profile == "anthropic-codex" and "openai-codex/gpt-6.1-sol" in available:
            result[role] = "openai-codex/gpt-6.1-sol:medium"
        else:
            print(f"skipped role {role}: model {value} not available")
    return result


def merge_record(key, current, additions, overwrite):
    if key in current and not isinstance(current[key], dict):
        raise ValueError(f"{key} is not a record")
    merged = dict(current.get(key, {}))
    for name, value in additions.items():
        if name in merged and not overwrite:
            print(f"kept yours: {key}.{name}")
        else:
            merged[name] = value
    return merged
