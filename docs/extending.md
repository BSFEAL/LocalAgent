# Extending LocalAgent

Every external system is behind a small protocol (an interface with one or two methods). Implement it,
pass it to `Factory(...)`, and nothing else changes. The CLI covers the built-in implementations. For your
own, drive the factory from a short Python script:

```python
from localagent import Factory, Outcome, ProviderRouter
factory = Factory(".", ProviderRouter("ollama:qwen3:32b"), ci=..., deployer=..., flags=..., metrics=...,
                  approver=..., context=..., improvement=..., executors=...)
factory.run(Outcome("..."))
```

| Extension point | Protocol | Built-ins |
|---|---|---|
| Model provider | `Provider.complete(...)` | see [Providers](providers.md#adding-a-provider) |
| CI | `run(ws) -> CIResult` | `CommandCI` |
| Approver | `request(outcome, changes, review) -> Approval` | `CLIApprover`, `CallbackApprover`, `AutoApprover` |
| Deployer | `deploy(ws, record)`, `rollback(ws, record)` → `(ok, output)` | `CommandDeployer`, `NoopDeployer` |
| Feature flags | `set(flag, percent)`, `get(flag)` | `LocalFlagStore` |
| Metrics | `read() -> dict[str, float]` | `JsonFileMetrics`, `CommandMetrics`, `PrometheusMetrics`, `StaticMetrics` |
| Context | `search(query, limit) -> list[ContextItem]` | `DirectorySource`, `GitSource` |
| Task executor | `execute(workflow, task, seed=) -> Execution` | `WorkspaceExecutor` |
| Trainer | `train(train, val, base_model, out_dir, generation) -> TrainResult` | `ExportOnlyTrainer`, `CommandTrainer` |
| Improvement strategy | `run_cycle(ctx, stages) -> ImprovementReport` | `MassStrategy`, `NoopStrategy` |
| Workflows | data (`Workflow`) | `localagent/factory/defaults.py` |

## CI

```python
from localagent.factory.ci import CIResult, CIStep

class GitHubActionsCI:
    def run(self, ws):
        run_id = trigger_workflow(ws.root)              # your code
        ok, log = wait_for(run_id)
        return CIResult([CIStep("gha", f"run {run_id}", 0 if ok else 1, log, 0.0)])
```

`CIResult.feedback()` is what the coder sees on failure, so put the useful part of the log in `output`.

## Approvers

```python
from localagent.factory import CallbackApprover
from localagent.factory.gates import Approval

def ask_on_slack(outcome, changes, review):
    msg = post_for_approval(f"{outcome.title} ({review.risk}): {changes.summary()}", changes.diff)
    decision = wait_for_button(msg)                      # your code
    return Approval(decision.ok, decision.user, decision.comment)

factory = Factory(..., approver=CallbackApprover(ask_on_slack))
```

## Deployers

```python
class ArgoDeployer:
    def deploy(self, ws, record):       # record.flag, record.outcome_id, record.files
        return run_argo_sync(record.flag)          # -> (ok: bool, output: str)
    def rollback(self, ws, record):
        return run_argo_rollback(record.flag)
```

For anything driven by a shell command, `CommandDeployer("./deploy.sh {flag}", "./rollback.sh {flag}")` is enough.

## Feature flags

```python
class LaunchDarklyFlags:
    def set(self, flag, percent): ld_api.patch_rollout(flag, percent)
    def get(self, flag): return ld_api.get_rollout(flag)
```

Your service should read the same flag, so that a rollout percentage actually changes traffic.

## Metrics

```python
class DatadogMetrics:
    def read(self):
        return {"error_rate": dd_query("sum:errors{svc:api}.as_rate()") / dd_query("sum:requests{svc:api}.as_rate()"),
                "p95_ms": dd_query("p95:trace.http.request{svc:api}") * 1000}
```

Name latency metrics with `latency`, `p95`, `_ms`, etc., so Perf Factory recognises them.

## Context sources

```python
from localagent.core import ContextItem

class NotionSource:
    name = "notion"
    def search(self, query, limit=5):
        return [ContextItem("notion", page.title, page.text[:4000], page.score)
                for page in notion_search(query)[:limit]]

factory.context.add(NotionSource())
```

Items from all sources are merged by `score`. Make scores roughly comparable to the built-in keyword
score, or put very relevant items first with a high score.

## Workflows

Change a stage's team without writing code: edit `.localagent/workflows/<stage>/v<N>.json`, save it
as a new version, and set `current`. From Python:

```python
from localagent.core import Workflow, AgentSpec
from localagent.factory.defaults import coder_team_workflow

wf = coder_team_workflow()
wf.version = factory.workflows.next_version("coder")
factory.workflows.save("coder", wf, make_current=True)

# or pass different defaults for a fresh state directory:
Factory(..., workflows={"coder": coder_team_workflow()})
```

Rules for the review workflow: each agent must answer with the
`{"risk", "approve", "findings"}` JSON. Give any new specialist the same `output_contract`
as the built-in ones (`FINDINGS_CONTRACT` in `factory/defaults.py`).

## Task executors

The improvement slot runs workflows on tasks through executors. By default `coder` uses a
`WorkspaceExecutor` that also runs CI, so the judge can use the CI result, and every other stage uses
the generic one. Register your own for a stage that needs special handling:

```python
from localagent.improvement import Execution

class ReviewExecutor:
    def execute(self, workflow, task, *, seed=None):
        ...   # run ReviewPanel on the task's diff, return Execution(task, workflow, output, deliverable, trajectory, workspace)

factory = Factory(..., executors={"review": ReviewExecutor()})
```

`Execution.deliverable` is the text the judge compares, so include everything that shows quality.

## Events

```python
factory.bus.subscribe("", lambda ev: print(ev.kind, ev.data))           # everything
factory.bus.subscribe("deploy.rollback", lambda ev: page_oncall(ev.data))
```
