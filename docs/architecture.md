# Architecture

[← Documentation index](README.md)

## Diagram → code

| Box in the factory diagram | Implementation |
|---|---|
| Software "builder" defines outcome | `factory/outcome.py` `Outcome` (title, description, acceptance criteria, constraints) |
| Codex writes/edits the code | `coder` stage: a `Workflow` (solo by default) run by `core/workflow.py` `WorkflowRunner` with workspace tools (`core/tools.py`) |
| Gets context from internal systems | `core/context.py` `ContextHub`: repo docs (docs-in-code), git history, folders of notes/skills; any `ContextSource` (Slack, Notion, Datadog...) plugs in. Exposed to agents as the `search_context` tool and injected into the coder prompt |
| CI: build + test (+ perf harness) | `factory/ci.py` `CommandCI` (named steps, perf command emitting JSON, budgets) behind the `CIRunner` protocol |
| Agent fixes issues until CI and reviews pass | `Factory.build` loop: CI output and blocking review findings go back to the coder as feedback, up to `max_iterations` |
| Agentic code review (data, infra, cloud, security) + risk classification | `factory/review.py` `ReviewPanel`: specialists run in parallel, a risk classifier aggregates, and a deterministic `RiskPolicy` sets a floor |
| Low-risk change? → Code review by an engineer | `Factory._gate_and_deploy`: low and approved means ship; otherwise an `Approver` (CLI, callback, auto for sandboxes) decides |
| Agentic deploy: babysits until prod, FF rollout, own dashboards | `factory/deploy.py` `AgenticDeploy`: a deploy agent picks signals and a flag, writes a dashboard spec, steps the flag through rollout stages, checks guardrails at each stage, and rolls back on breach. `Deployer` and `FlagStore` are protocols |
| Production: agent watches graphs/signals/alerts | `factory/observability.py` (`MetricsSource`: JSON file, command, Prometheus, static) + `Factory.monitor_once` |
| Perf Factory: detects latency regressions, filters duplicates, proposes fixes | `factory/perf.py` `PerfFactory`: EWMA baselines, fingerprint + cooldown dedupe, and a perf agent that turns a regression into an `Outcome` fed back to the coder (`Factory.run`) |
| Sevbot: investigates, proposes mitigations, answers questions | `factory/sevbot.py` `Sevbot`: tools for metrics, recent deploys, flag state, git log and context. Returns a structured incident report, can switch flags off when allowed, and answers questions via `ask` |

Every stage records trajectories (`core/trajectory.py`) and emits events (`core/events.py`) to
`.localagent/`. The agentic stages are `coder`, `review`, `deploy`, `perf` and `sevbot`. Each is driven
by a versioned `Workflow` in `.localagent/workflows/<stage>/`, which is the object the
improvement slot evolves.

## Provider layer

```
Provider.complete(messages, tools) -> Completion        # neutral types in providers/base.py
  ├─ OpenAICompatProvider   /chat/completions  (openai, vllm, ollama, lmstudio, openrouter, compat)
  ├─ AnthropicProvider      /v1/messages
  ├─ GeminiProvider         :generateContent
  └─ ScriptedProvider       offline tests/demos
ProviderRouter.for_role("review.security") → "review.security" → "review" → "default"
```

JSON output never depends on a vendor's JSON mode. `Provider.complete_json` parses tolerantly and
re-asks when the reply is not valid JSON. Reasoning text ("thinking") is kept on messages so SFT traces include it.

## Paper slot

```python
class ImprovementStrategy(ABC):
    def observe(self, trajectory): ...                       # online hook
    def run_cycle(self, ctx: ImprovementContext, stages) -> ImprovementReport: ...
```

`ImprovementContext` = router, `WorkflowStore`, `TaskBank`, `TrajectoryStore`, per-stage
`TaskExecutor`s (default `WorkspaceExecutor`: fresh isolated workspace from a template dir or a
`git archive` of the outcome's base revision, optional CI as a verifiable check), default
workflows, run dir and event bus.

## Paper → code (MASS, arXiv:2610.12176)

| Paper concept | Here |
|---|---|
| Current model L^(k) as executor, optimizer and evaluator | router role `mass` (sub-roles `mass.optimizer`, `mass.judge` fall back to it) |
| Workflow w (roles + routing) | `core/workflow.py` `Workflow` / `AgentSpec` (output contracts, protocol) |
| RHI workflow search | `improvement/mass/rhi.py` `RecursiveHarnessImprovement` |
| Optimizer prompt (multi-agent advantages, output contracts, recall loops, evidence-grounded) | `improvement/mass/optimizer.py` |
| Structural guardrails | `improvement/mass/guardrails.py` |
| Bare-task reference | `reference="bare"`: a single agent with the stage's base instruction |
| Champion arbitration (released code) / compare with retained best (Algorithm 1) | `comparison="reference"` / `comparison="best"` |
| Self-evaluation, pairwise | `improvement/mass/judge.py` (both presentation orders; disagreement = tie; verifiable checks first) |
| Within-task tournament + Bradley–Terry | `improvement/mass/ranking.py` |
| Ranks 1–15 train / 16 validation; test tasks excluded | `improvement/mass/sft.py` `select` (raises on test-split tasks) |
| Workflow removal from the orchestrator prompt; assistant-only loss | `sft.render_conversation` (generic orchestrator preamble; `weight: 1` on assistant turns only) |
| Orchestrator sampled with p = 1/3, task-uniform, with replacement | `sft.sample_mix` |
| Post-training → L^(k+1) | `improvement/mass/trainer.py` (`CommandTrainer` / `ExportOnlyTrainer`); generations persist in `.localagent/improvement/generations.json` |

### Deviations

* **Stage-level instead of task-level search.** The paper retains one workflow per task. A
  factory stage serves many tasks, so each design is scored on a minibatch of the stage's training
  tasks (`search_tasks`). "Beats" means more pairwise wins than losses. With one task this reduces
  to the paper's per-task search.
* **Runtime.** The paper runs each agent inside a coding-agent CLI (qwen-code). Here agents
  use this framework's own tool loop and `delegate` / `delegate_parallel` tools. A second
  delegation to the same worker *recalls* it with its conversation intact.
* **Workflow removal.** Workers in this runner get their instructions from the spec, not from
  orchestrator-authored prompts. So stripping the workflow replaces the orchestrator's system prompt
  with the generic orchestration preamble, and worker conversations keep their instructions (as in the paper).
* **Training recipe.** LoRA hyperparameters, windowing and checkpoint selection belong to the external trainer.
  The datasets carry the metadata needed to reproduce the paper's sampling.
* **Reporting** with external judges is not built in. Use `PairwiseJudge` with any other provider.
