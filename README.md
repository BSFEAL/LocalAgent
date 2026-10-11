# LocalAgent

A **provider-agnostic agentic software factory** with a pluggable
**self-improvement slot**. It ships
[MASS — *Recursive Self-Improvement through Multi-Agent Self-Supervision*](https://arxiv.org/abs/2610.12176)
(Lee et al., 2026) as the first strategy in that slot.

It implements the "agentic software factory" loop end to end:

```
 builder ──outcome──▶ coder ──▶ CI: build + test + perf harness ──▶ agentic review
   (you)              ▲  │gets context        │fail                 parallel: data · infra · cloud · security
                      │  ▼ from repo docs,    │                     + risk classification
                      │  git, notes, skills   │                          │
                      └───────────────────────┴──── fix until green ◀────┤ blocking findings
                                                                         ▼
                                     human code review ◀──not low risk── low-risk?
                                             │                           │ yes
                                             ▼                           ▼
                                        agentic deploy: feature-flag rollout 1→5→25→50→100 %,
                                        babysits guardrail signals, auto-rollback, writes a dashboard
                                             │
                                             ▼
                                        production ──monitors──▶ Perf Factory ──proposes fix──▶ coder
                                             └──outage──▶ Sevbot: investigates, proposes/applies mitigations, answers questions

 the paper slot (MASS by default): evolves each stage's multi-agent workflow and,
 optionally, fine-tunes the model on the factory's own trajectories → next generation
```

Python 3.11+ and the standard library only. There are no required dependencies.

## Why "provider-agnostic" and "paper slot"

* **Providers.** Every model call goes through one small interface
  (`localagent.providers.Provider`). Adapters exist for OpenAI, Anthropic, Gemini, and any
  OpenAI-compatible server: vLLM, SGLang, Ollama, LM Studio, llama.cpp, OpenRouter, Together, Groq and others.
  A `ProviderRouter` maps *roles* to models, so you can mix vendors per stage:

  ```toml
  [providers]
  default = "ollama:qwen3:32b"            # coder etc. on a local model
  [providers.routes]
  "review.security" = "anthropic:claude-opus-5-5"
  sevbot = "openai:gpt-5.5"
  mass = "vllm:Qwen/Qwen3.6-27B"          # MASS: one model = executor, optimizer, judge
  ```

  Add your own provider with `register_provider("name", factory)`.

* **Paper slot.** Self-improvement is a strategy behind the `ImprovementStrategy` interface
  (`localagent/improvement/base.py`). The factory gives it a context with five things: providers,
  versioned per-stage workflows, a task bank, recorded trajectories and task executors. The
  strategy can retain new workflow versions, which take effect immediately. It can also return a
  new model spec, which becomes the next generation. MASS is `"mass"` and `"noop"` turns improvement off. To plug in
  another paper, write one class and register it
  ([`examples/custom_strategy.py`](examples/custom_strategy.py)).

## Quick start

```bash
pip install -e .                      # or just: python -m localagent ...
localagent init                       # writes localagent.toml; set providers + CI steps
localagent build "Add rate limiting to /login" \
  -c "429 after 5 attempts/min per IP" -c "tests cover it"
localagent monitor --ticks 10 --interval 60 --build-fixes
localagent sevbot "why did error_rate spike at 14:00?"
localagent improve --stages coder     # one MASS cycle over the task bank
localagent workflows coder            # see the currently retained workflow
localagent info                       # providers + strategies
```

Run everything offline with a scripted model:

```bash
python examples/offline_demo.py
python -m unittest discover -s tests -t .
```

## Python API

```python
from localagent import Factory, Outcome, ProviderRouter, create_strategy
from localagent.factory import CommandCI, CLIApprover, PrometheusMetrics, Signal

factory = Factory(
    ".", ProviderRouter("vllm:Qwen/Qwen3.6-27B", {"review.security": "anthropic:claude-opus-5-5"}),
    ci=CommandCI({"lint": "ruff check .", "test": "pytest -q"}, perf_command="python bench.py",
                 perf_budgets={"p95_ms": 50}),
    approver=CLIApprover(),
    metrics=PrometheusMetrics("http://prom:9090", {"error_rate": "...", "p95_ms": "..."}),
    guardrails=[Signal("error_rate", max=0.01)],
    improvement=create_strategy("mass", iterations=4, candidates=16),
)
factory.run(Outcome("Add CSV export", acceptance_criteria=["streams > 1GB files"]))
factory.improve(["coder", "sevbot"])
```

## How MASS works here

MASS improves workflows and then learns from them. Each **cycle** has six steps:

1. **RHI workflow search** (weights fixed). For a stage, the current model runs the stage's
   workflow on a minibatch of training tasks. The *same* model judges each output pairwise
   against a bare single-agent reference, then proposes a new multi-agent design: roles,
   "output to orchestrator" contracts, and recall/re-delegation protocol. Structural guardrails
   reject bad designs: too many agents, missing contracts, invented numeric caps, oversized designs.
   A champion is retained only if it beats the reference (`comparison="reference"`, as in
   the released code) or the best design so far (`comparison="best"`, the paper's Algorithm 1).
2. **Collect.** The retained workflow solves each training task N times. These runs are the teacher trajectories.
3. **Self-rank.** A connected within-task tournament is judged by the current model, then ranked with Bradley–Terry.
4. **Select.** Ranks 1–15 go to training and rank 16 to validation. Test-split tasks are refused.
5. **Render.** The output is chat-JSONL with only assistant turns weighted. The workflow is stripped from the
   orchestrator prompt, so the student has to internalise it.
6. **Post-train** with a pluggable `Trainer`. `CommandTrainer` shells out to any LoRA/SFT stack
   and returns a new provider spec. That spec becomes `L^(k+1)` and persists across restarts.
   `ExportOnlyTrainer` (the default) only writes the datasets. Hosted models that can't be fine-tuned still get the workflow improvements.

The factory feeds MASS automatically. Every built outcome becomes a coder task, and every stage's
conversations are recorded as trajectories under `.localagent/`.

See [docs/architecture.md](docs/architecture.md) for the module map, how the code maps to the
diagram and to the paper, and where this implementation deviates from the paper.

## Layout

| Path | What |
|---|---|
| `localagent/providers/` | Neutral message types, OpenAI-compat / Anthropic / Gemini / mock adapters, registry, role router |
| `localagent/core/` | Agent tool loop, workspace-confined tools, `Workflow` (evolvable multi-agent design) + runner, trajectories, context sources, event bus |
| `localagent/factory/` | Coder, CI, review panel + risk policy, human gate, agentic deploy + flags, observability, Perf Factory, Sevbot, `Factory` pipeline |
| `localagent/improvement/` | The paper slot (`ImprovementStrategy`, task bank, executors) |
| `localagent/improvement/mass/` | MASS: RHI search, optimizer, guardrails, pairwise judge, tournament + Bradley–Terry, SFT rendering, trainers |
| `examples/` | Offline demo and a custom-strategy template |
| `tests/` | Offline tests (local fake HTTP servers for each vendor wire format, end-to-end factory and MASS cycles) |

## Safety notes

* File and shell tools are confined to the workspace. You can set a shell allowlist with `Workspace(shell_allowlist=[...])`.
* The risk floor is deterministic (`RiskPolicy`). Changes to migrations, auth, secrets, infra or CI
  are always high risk and go to a human, whatever the models say. An unparseable review counts as a rejection.
* `AutoApprover` exists for sandboxes and tests. Use `CLIApprover` or `CallbackApprover` (Slack, a PR review, etc.) in real setups.
* Sevbot only changes flags when you set `auto_mitigate = true`.
