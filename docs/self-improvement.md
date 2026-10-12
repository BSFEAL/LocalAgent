# Self-improvement: the paper slot and MASS

The factory records everything it does. A **self-improvement strategy** uses those records, offline,
to make the factory better. Strategies are pluggable, so a method from a research paper fits into a
fixed slot without touching the rest of the code. LocalAgent ships two:

| Strategy | What it does |
|---|---|
| `mass` (default in `localagent init`) | MASS: *Recursive Self-Improvement through Multi-Agent Self-Supervision*, Lee et al., [arXiv:2610.12176](https://arxiv.org/abs/2610.12176) |
| `noop` | nothing; turns improvement off |

## The idea behind MASS, briefly

A multi-agent team can often do better than one agent, but designing the team by hand is hard.
MASS has a model **design its own team, judge the results itself, and then learn from the team's best
work**. Each cycle has two halves:

1. **Improve the workflow, model fixed.** The model proposes better team designs for a stage. Each
   design is run on real tasks and the model compares the results pairwise. The best design is kept.
2. **Improve the model.** The kept team solves each task many times. The model ranks those runs, and
   the best ones become fine-tuning data. The team design is removed from the prompt, so the
   fine-tuned model has to learn the teamwork. The fine-tuned model is the next generation, and the next
   cycle starts from it.

This needs no human labels and no automatic test of correctness. That is why it suits open-ended
tasks like research, data analysis or writing code against fuzzy goals.

## Running a cycle

```bash
# tasks: every built outcome is already a coder task; add more if you like
localagent task coder "Implement a token-bucket rate limiter with tests" --id rl-1 --template-dir ./fixtures/empty_py
localagent task coder "…held-out task…" --id eval-1 --split test

localagent improve --stages coder
```

```python
factory.improve(["coder"])          # same thing from Python; returns an ImprovementReport
```

Only `train` tasks are used. `test` tasks never enter the training data.

## Step by step

### 1. Workflow search (RHI)

For each stage:

1. **Baseline.** The search tasks (the first `search_tasks` train tasks) are run with a baseline workflow:
   a single agent (`reference = "bare"`) or the workflow in use (`reference = "current"`).
2. **Starting point.** The stage's current workflow (v0 of this search) is run and judged against the baseline.
3. **Propose.** The model plays *workflow optimizer*. It sees the tasks, the current design, the best
   design so far, the original design, evidence from the last runs (outputs, diffs, CI results,
   token use, which agents took how many turns) and the full comparison history with the judge's
   reasons. It returns a complete new design plus a list of what it changed. The prompt pushes it towards
   what single agents can't do: parallel specialists, agents critiquing each other, tighter
   report formats for each agent, and recalling agents with narrower follow-ups.
4. **Check the design.** The proposal is rejected and re-asked, up to twice, if it breaks the rules: too many
   agents, a missing report format, a design too long, a removed required agent, or
   **invented numeric limits** like "at most 3 rounds" (the paper wants agents to stop when the
   acceptance criteria are met, not after a fixed count).
5. **Score.** The proposal is run on the search tasks and judged:
   * `comparison = "reference"` (the released code): it must beat the baseline. If a champion already
     exists, it must then also beat the champion directly to replace it.
   * `comparison = "best"` (the paper's Algorithm 1): it must beat the current champion, or the baseline
     while there is no champion.
6. Repeat `iterations` times. Each proposal builds on the previous one, and both the latest and the best designs are shown.

The **champion** becomes the stage's current workflow straight away. If nothing beat the baseline, the stage
keeps its workflow and is left out of data collection, as the paper does.

### 2. Judging without a correctness test

`PairwiseJudge` shows the model the task and two results, labelled A and B, and asks which is better
and why. To reduce bias towards whichever result comes first, every comparison is asked **in both orders**.
If the two answers disagree, the result is a tie. If both results have automatic checks (for the coder,
whether CI passed) and only one passed, that one wins without asking the model.

### 3. Collect, rank, select

* The champion solves **every** train task `candidates` times, with different seeds.
* Within each task, runs are paired in a seeded tournament where each run meets about `opponents`
  others and every run is connected to the rest. Results are turned into a ranking with the
  Bradley–Terry method.
* Ranks 1…`train_ranks` → training data; the next `val_ranks` → validation data.

### 4. Training data format

`sft_train.jsonl` and `sft_val.jsonl` use the common chat fine-tuning format with tool calls:

```json
{"messages": [
   {"role": "system", "content": "<generic orchestrator preamble: team removed>"},
   {"role": "user", "content": "<task>"},
   {"role": "assistant", "content": "", "tool_calls": [...], "reasoning_content": "...", "weight": 1},
   {"role": "tool", "tool_call_id": "...", "content": "..."},
   ...],
 "tools": [...],
 "metadata": {"role": "orchestrator", "agent_id": "lead", "task_id": "rl-1",
              "trajectory_id": "…", "workflow": "coder@v3"}}
```

* Every conversation in a run becomes one example: the orchestrator's and each worker's.
* Only assistant turns carry `"weight": 1`, so the model is trained only on what it wrote.
* With `strip_workflow`, the orchestrator's system prompt is replaced by the generic orchestration
  preamble, so the student has to *internalise* the team design. Worker instructions stay, as in the paper.
* `metadata.role` lets a trainer copy the paper's mix: `localagent.improvement.mass.sample_mix(path, n)`
  samples tasks evenly, with replacement, and picks orchestrator conversations with probability 1/3.

### 5. Training → the next generation

The trainer is pluggable. LocalAgent never trains models itself:

* `ExportOnlyTrainer` (default) writes the datasets and stops. Workflow improvements still apply. This is
  the right choice for hosted models you can't fine-tune.
* `CommandTrainer` runs any command (axolotl, unsloth, TRL, torchtune, a cloud fine-tuning CLI…)
  and returns a provider spec for the result. That spec becomes the router's `default` model and is
  recorded in `improvement/generations.json`, so the factory starts on the newest generation from then on.

The paper's recipe, for reference: LoRA rank 64, alpha 128, dropout 0.05; AdamW (0.9, 0.95),
learning rate 3e-5 with 5 warmup steps and cosine decay to 10 %; 49k-token windows; keep the
checkpoint with the lowest validation loss.

## Reading the report

`improvement/mass-<run>/report.json`, abridged:

```json
{"strategy": "mass", "stages": {"coder": {
   "searched_on": ["rl-1", "rl-2"],
   "proposals": [4, 5],
   "rejected_proposals": 0,
   "history": [
     {"version": 3, "against": "reference", "wins": 0, "losses": 0, "ties": 2, "rationales": ["..."]},
     {"version": 4, "against": "reference", "wins": 2, "losses": 0, "ties": 0, "changes": ["added critic"]},
     {"version": 5, "against": "reference", "wins": 2, "losses": 0, "ties": 0},
     {"version": 5, "against": "champion v4", "wins": 0, "losses": 0, "ties": 2}],
   "retained": 4,
   "mean_output_tokens": {"coder@v-1": 812, "coder@v3": 790, "coder@v4": 1430, "coder@v5": 1510},
   "collected": 48, "selected": {"train": 30, "val": 6}}},
 "new_model": null, "artifacts": {"train": "...", "val": "...", "report": "..."}}
```

`mean_output_tokens` (`v-1` is the baseline) lets you compare quality gains with their cost.
The paper reports its results as *performance per output token*.

## Which stages can be improved

Any stage whose workflow can be run on a task. The default task runner suits stages that work on a
workspace and produce an output: `coder` out of the box, and anything you add tasks for.
The review panel always runs its specialists in parallel and then aggregates, which the generic runner
doesn't reproduce. To improve `review` faithfully, register a runner for it (see
[Extending → Task executors](extending.md#task-executors)).

## Writing your own strategy (plugging in another paper)

```python
from localagent.improvement import ImprovementStrategy, ImprovementReport, register_strategy

class MyPaper(ImprovementStrategy):
    name = "my_paper"

    def observe(self, trajectory):            # optional: called on every recorded run
        ...

    def run_cycle(self, ctx, stages):
        report = ImprovementReport(self.name)
        for stage in stages:
            tasks = ctx.tasks.get(stage, "train")
            wf = ctx.workflow(stage)                       # current team
            ex = ctx.executor(stage).execute(wf, tasks[0], seed=0)   # run it in a fresh workspace
            ...                                            # your method
            ctx.workflows.save(stage, new_wf, make_current=True)     # adopt a new team
        # optionally: ctx.router.set("default", "vllm:my-new-model")
        return report

register_strategy("my_paper", MyPaper)
```

What `ctx` (`ImprovementContext`) gives you:

| Field | Use |
|---|---|
| `router` | models by role; `set("default", spec)` switches generation |
| `workflows` | versioned workflows: `save`, `load`, `versions`, `next_version` |
| `tasks` | task bank: `get(stage, split)` |
| `trajectories` | every recorded run: `load(stage)`, `save` |
| `executor(stage)` | runs a workflow on a task in a fresh workspace and returns an `Execution` (output, result text for judging, trajectory, workspace, CI result) |
| `run_dir`, `bus` | where to write reports, and the event bus |

The MASS building blocks can be reused on their own: `PairwiseJudge`, `WorkflowOptimizer`,
`StructuralGuardrails`, `tournament`, `bradley_terry`, `write_dataset`, `CommandTrainer`.
[`examples/custom_strategy.py`](../examples/custom_strategy.py) is a complete ~40-line example.
From Python, use it with `factory.improvement = create_strategy("my_paper", **options)`. A config file can say
`strategy = "my_paper"` only if the module that registers it was imported before `build_factory`
runs. The `localagent` CLI does not load plugin modules yet, so for custom strategies drive the
factory from a small Python script.
