# The factory, stage by stage

`Factory.build(outcome)` runs stages 1–7. `Factory.monitor_once()` runs 8–10.
`Factory.run(outcome)` does both and feeds Perf Factory fixes back into `build`.

## 1. Outcome intake

The outcome is added to the task bank as a `coder` task, pinned to the current git revision, so
self-improvement can replay it later. An `outcome.received` event is emitted.

## 2. Context

Before coding, the factory searches its context sources with the outcome's title and description.
The best matches (up to about 12k characters) are added to the coder's prompt. Agents can search
further during the run with the `search_context` tool.

Default sources: the repository's own text files (*docs live in the code*) and recent git history.
Add more with `[context] dirs = [...]`, or write a custom `ContextSource` for live systems such as Slack,
Notion, Datadog or Databricks (see [Extending](extending.md#context-sources)).

Retrieval is keyword-based. A failing source never stops a build; its error simply shows up as a context item.

## 3. Coder

The `coder` workflow runs in the repository with full workspace tools. The default is one agent,
`codex`, told to read before editing, make minimal changes, add tests, run the checks, and keep docs in sync.
A multi-agent alternative ships as `coder_team_workflow()` (lead, implementer, tester, critic).
Self-improvement can also discover a team of its own.

All file changes are tracked by comparing content before and after, so this works with or without
git. If the coder changes nothing, it is told so and tries again.

## 4. CI

Each `[ci.steps]` command runs in order. The first failure ends CI, and that step's output (the last
4000 characters) goes back to the coder as feedback. If a perf harness is configured, it runs after
the steps, and a metric over its budget is treated as a failure.

## 5. Agentic review

If CI passes, the **review** workflow runs:

1. Every specialist reviews the outcome and diff **in parallel**, with read-only tools. Each returns
   `{"risk", "approve", "findings": [{"severity", "file", "line", "message"}]}`.
2. The orchestrator, `risk_classifier`, reads all the reviews and gives the overall verdict.
3. The final risk is the **highest** of: the classifier's risk, the riskiest specialist's risk, and the
   fixed rule set (`RiskPolicy`). The default rules:
   * **high** if any file matches migrations, `*.sql`, auth, secrets, security, IAM, Terraform,
     k8s/helm, Dockerfile, CI workflows or deploy paths;
   * **medium** if config, YAML/TOML, requirements, `package.json` or lock files change, or the
     diff is over 200 lines;
   * otherwise **low**.
4. Findings with severity `blocker` or `major` are **blocking**. They go back to the coder, and the
   loop continues from step 3.
5. A review that can't be parsed counts as `high` risk with a `major` finding. A broken reviewer can never wave a change through.

The review workflow is data. You can add a "performance" or "accessibility" specialist by
editing `.localagent/workflows/review/v<N>.json` and pointing `current` at it.

## 6. Gate

* `risk == "low"` **and** the classifier approved → shipped automatically (`approved_by: agentic-review`).
* Anything else → the configured `Approver`:
  * `CLIApprover` shows the reasons and findings, offers the diff, and asks y/N;
  * `CallbackApprover(fn)` connects to anything: a Slack button, a GitHub PR review, a web form;
  * `AutoApprover(True/False)` is for sandboxes and tests.

If the change is rejected, the build stops with status `rejected` and the changes stay in your working tree.

## 7. Agentic deploy

1. **Plan**: the deploy agent reads the change and the currently available metric names. It picks a
   flag name and the signals worth watching (`{"metric", "max"|"min"}`). The configured
   `[[guardrails]]` are always added. If the agent's answer can't be used, the flag defaults to
   `la-<outcome id>` with guardrails only.
2. **Dashboard**: a vendor-neutral spec is written to `.localagent/dashboards/<flag>.json`. It has one
   panel per signal with its thresholds, ready to translate into Grafana or Datadog.
3. **Ship**: the flag is set to 0 %, then the deploy command runs. If it fails, the status is `failed`.
4. **Babysit**: the flag steps through `rollout_stages`. At each stage it waits `soak_seconds`, then reads
   metrics and checks every signal. On any breach: flag back to 0 %, rollback command, status
   `rolled_back`, and a `deploy.rollback` event listing what breached.
5. Every deploy is appended to `deploys.jsonl`, which Sevbot reads later.

## 8. Production monitoring

`monitor_once()` reads metrics once, then:

* hands every latency-like metric to **Perf Factory** (9);
* checks error and availability signals: the configured guardrails plus the signals of the 5 most recent
  completed rollouts. Any breach goes to **Sevbot** (10).

## 9. Perf Factory

* **Detect**: each latency metric (name containing `latency`, `p50`, `p90`, `p95`, `p99`, `_ms` or
  `duration`) has a moving-average baseline. A reading more than 20 % above its baseline is a regression.
  Only healthy readings update the baseline, so a regression can't slowly become the new normal.
* **Filter duplicates**: an open alert on the same metric is not raised again for an hour (`perf.duplicate` event).
  The alert closes once the metric is healthy again.
* **Propose**: the `perf` agent sees the regression and recent deploys, can read the code, and
  writes a fix as a new `Outcome` (`origin: "perf_factory"`). `Factory.run` and `monitor --build-fixes`
  send it to the coder, which closes the loop.

The threshold (`0.2`), baseline smoothing (`0.2`) and duplicate window (`3600` s) are
constructor arguments of `PerfFactory`. To change them, replace `factory.perf` from Python.

## 10. Sevbot

**Incidents**: Sevbot gets the alert text and investigates with `metrics_now`, `recent_deploys`,
`flag_state`, `git_log`, read-only code access and `search_context`. It returns:

```json
{"summary": "...", "suspected_cause": "...", "confidence": 0.8,
 "mitigations": ["turn off flag la-3f2a…", "..."], "rollback": true, "flag": "la-3f2a…"}
```

With `auto_mitigate = true`, Sevbot sets the named flag to 0 % itself when it recommends a rollback
(`sevbot.mitigated` event). Otherwise the report is advice for a human.

**Questions**: `localagent sevbot "…"` / `factory.ask_sevbot("…")` answers with the same tools.

## Running stages yourself

Every stage is also usable on its own:

```python
from localagent.factory import ReviewPanel, ChangeTracker
from localagent.core import Workspace

ws = Workspace("."); tracker = ChangeTracker(ws); tracker.start()
# ... make changes ...
result = ReviewPanel(factory.router).review(factory.workflow("review"), "Outcome text", tracker.collect(),
                                    ws.toolbox(read_only=True))
print(result.risk, result.blocking)
```
