# Concepts

## Outcome

An `Outcome` is what the builder hands the factory: a goal, not a list of steps.

```python
Outcome(
    title="Add CSV export",
    description="Users can download any report as CSV",
    acceptance_criteria=["streams files > 1 GB", "UTF-8 with header row"],
    constraints=["no new dependencies"],
    origin="builder",            # "builder" | "perf_factory" | "sevbot"
)
```

Each outcome has a stable `id`, a hash of its title and description. That id names its
feature flag (`la-<id>` by default) and its task in the task bank.

## Stage

A stage is one step of the factory. The agentic stages are:

| Stage | Job | Default team |
|---|---|---|
| `coder` | implement the outcome | one agent, `codex` |
| `review` | review the diff and assign a risk level | 4 specialists + `risk_classifier` |
| `deploy` | pick a flag and the signals to watch during rollout | `deploy_agent` |
| `perf` | turn a latency regression into a fix outcome | `perf_engineer` |
| `sevbot` | investigate incidents, answer questions | `sevbot` |

CI, the approval gate and the rollout mechanics are deterministic code, not agents.

## Agent

An `Agent` is one model conversation with a system prompt, tools and a step budget
(`max_steps`, 40 by default). It loops: call the model, run any tool calls it made, send back the
results, and repeat until the model answers without calling a tool. If the budget runs out, it is asked
for a final answer with no tools.

An agent is **stateful**. Sending it another message continues the same conversation. That is how
an orchestrator *recalls* a worker for a follow-up.

## Workflow

A `Workflow` is a stage's **agent team, written as data**:

```json
{
  "name": "coder", "version": 3, "parent": 1,
  "orchestrator": {"id": "lead", "role": "tech lead", "instruction": "...", "tools": null},
  "agents": [
    {"id": "implementer", "role": "software engineer", "instruction": "...",
     "output_contract": "List of files changed, rationale, open questions.", "tools": null},
    {"id": "critic", "role": "code reviewer", "instruction": "...",
     "output_contract": "Numbered defects with file:line, or 'LGTM'.",
     "tools": ["read_file", "search"]}
  ],
  "protocol": "Implementer first; critic reviews; recall implementer on defects.",
  "notes": ["added critic with recall loop"]
}
```

* **No `agents`**: a single agent runs the task. This is the "solo" or "bare" form.
* **With `agents`**: the orchestrator gets two extra tools. `delegate(agent_id, task)` hands work to one
  agent; `delegate_parallel(assignments)` runs several at once. Each agent replies in the format its
  `output_contract` asks for. Delegating to the same agent again **recalls** it with its earlier
  conversation intact.
* `tools: null` means every tool the stage offers. A list restricts the agent to those tools.
* `route` (optional) sends that agent to a specific model role. See [Providers](providers.md#routing).

Workflows are versioned on disk. The factory always uses the stage's **current** version.
Self-improvement adds new versions and moves `current` forward.

## Tools

Agents only change the world through tools, and file and shell tools can't leave the workspace:

| Tool | Available to |
|---|---|
| `read_file`, `list_files`, `search` | every stage |
| `write_file`, `edit_file`, `run_command` | coder only (reviewers, deploy, perf and Sevbot are read-only) |
| `search_context` | every stage: searches docs, git history and notes |
| `delegate`, `delegate_parallel` | orchestrators of multi-agent workflows |
| `metrics_now`, `recent_deploys`, `flag_state`, `git_log` | Sevbot |

## Trajectory

A `Trajectory` is the complete record of one stage run: every conversation, with every message, tool
call and result, plus token usage, the final output and metadata (CI result, review risk…).
Trajectories are the audit trail and the raw material for self-improvement.

## Task and task bank

A `Task` is a reusable problem for self-improvement: a prompt, a stage and a split (`train` or
`test`). It also says how to build a fresh workspace for it: a template folder to copy, or a git
repo and revision to export. Every outcome you build is added automatically as a `coder` task, pinned to
the repo revision it started from. You can add your own with `localagent task`.

Tasks marked `test` are only used for evaluation and never become training data.

## Events

Every stage emits events, such as `ci.failed`, `review.done`, `deploy.rollback` or
`perf.regression`. They are written to `events.jsonl` and can be subscribed to in Python:

```python
factory.bus.subscribe("deploy.", lambda ev: notify_slack(ev.kind, ev.data))
```

Full list: `outcome.received`, `coder.done`, `ci.passed`, `ci.failed`, `review.done`,
`review.human_requested`, `gate.decision`, `build.failed`, `deploy.dashboard`, `deploy.stage`,
`deploy.done`, `deploy.rollback`, `deploy.failed`, `deploy.plan_fallback`, `perf.regression`,
`perf.duplicate`, `perf.proposal`, `sevbot.incident`, `sevbot.report`, `sevbot.mitigated`,
`improve.start`, `improve.log`, `improve.retained`, `improve.new_model`, `improve.done`.

## State directory

Everything lives in `<repo>/.localagent/`. You can move it with `Factory(state_dir=...)`.

| Path | Contents |
|---|---|
| `events.jsonl` | every event, one JSON object per line |
| `workflows/<stage>/v<N>.json` | every workflow version ever proposed or used |
| `workflows/<stage>/current` | the version number in use |
| `trajectories/<stage>.jsonl` | every recorded run of that stage |
| `tasks.jsonl` | the task bank (later lines override earlier ones with the same stage and id) |
| `flags.json` | feature flag percentages (local flag store) |
| `deploys.jsonl` | deploy history: flag, files, status, signals, dashboard |
| `dashboards/<flag>.json` | dashboard spec written by the deploy agent |
| `perf_state.json` | Perf Factory latency baselines and open alerts |
| `improvement/workspaces/` | isolated workspaces used during self-improvement |
| `improvement/mass-<run>/` | `report.json`, `sft_train.jsonl`, `sft_val.jsonl` |
| `improvement/generations.json` | model generations produced by training; the newest one is loaded at start-up |
