---
name: model-routing-lifecycle
description: "Set up or verify OMP plan/execute/verify model routing (planning, execution, verifier, reviewer and advisor roles) via modelRoles, task.agentModelOverrides and a sticky RULES.md; includes how to prove which model each subagent ran on."
---

# OMP model routing lifecycle

Use when the user wants automatic plan -> execute -> verify model switching in OMP.

## Facts
- OMP has no per-prompt auto router. Routing = roles + subagent overrides + a sticky rule that tells the main model to delegate.
- `prewalk.enabled: true` hands off to `prewalk.into` (default `@smol`) at the first edit/write, one-shot, unconditional. `prewalk.from` only affects `/prewalk restart`; sessions plan on `@default`.
- `prewalk.auto: suggest|on` needs a NATIVE judge backend (typesafe jev, OpenRouter/OpenAI Decisions, Workers AI, LM Studio). Chat subscriptions do not count; without one it silently does nothing.
- `plan.defaultOnStartup` is ignored in `-p` mode; `--plan-yolo` auto-approves the plan and switches to `--plan-yolo-into` (default smol).
- Subagent model order: `task.agentModelOverrides[agent]` > agent frontmatter `model` > parent model. Overrides accept `@role` aliases defined in `modelRoles` (custom role names allowed).
- `~/.omp/agent/RULES.md` is a sticky rule: sent on every request AND reaches subagents (AGENTS.md does not reach subagents).
- Advisor: `modelRoles.advisor` or `WATCHDOG.yml` advisors[].model; `advisor.enabled: true`; reviews each turn on its own model.
- List available models with `omp models`; choose models available to the user.

## Config pattern (~/.omp/agent/config.yml)
```yaml
modelRoles:
  default: <planning-model>
  plan: <planning-model>
  exec_fast: <economical-execution-model>
  exec_strong: <execution-model>
  plan_verifier: <plan-verification-model>
  code_review: <independent-review-model>
  advisor: <advisor-model>
plan: { defaultOnStartup: true }
task:
  eager: preferred
  agentModelOverrides: { sonic: "@exec_fast", task: "@exec_strong", plan-verifier: "@plan_verifier", code-reviewer: "@code_review" }
```
RULES.md: plan (planning role) -> plan-verifier -> user approves -> sonic (economical execution role) for specified edits, `task` (execution role) when judgment is needed or the economical worker failed its check -> code-reviewer MANDATORY for every code change (small-fix path skips only the plan check) -> report with test evidence.
Back up config.yml before editing. Test overlays with `omp -p --config "$TMPDIR/x.yml"`.

## Verify (do not trust agent self-reports; an agent mislabeled its own model)
Session logs: `~/.omp/agent/sessions/<cwd-slug>/<ts>.jsonl`; subagents in sibling dir `<ts>/<Name>.jsonl`, advisor in `__advisor.default.jsonl`. Parse `model_change` events and assistant `message.provider/model` per file. Test on a scratch git repo with a multi-file task; in `-p` mode the main session stops at the plan gate, so send a follow-up `omp -p --continue "Plan approved. Proceed"`.

## Pitfalls
- Trivial fixes may skip the plan check but never independent code review (small-fix rule).
- Trust prompts for new folders block CLI workers in tmux.
