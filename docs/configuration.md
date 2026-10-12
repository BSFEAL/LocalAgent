# Configuration reference

The CLI reads `localagent.toml` from the repository root. Use `--config` to point elsewhere, and
`localagent init` to write a commented starter file. Every section is optional.

## `[providers]`

| Key | Type | Default | Meaning |
|---|---|---|---|
| `default` | string | `"ollama:qwen3:32b"` | model used by any role without its own route |
| `routes` | table | `{}` | role → provider spec, e.g. `"review.security" = "anthropic:claude-opus-5-5"` |
| `options.<provider>` | table | `{}` | constructor options for that provider (`base_url`, `api_key`, `timeout`, `temperature`, `max_tokens`, `extra_body`, `extra_headers`…) |

The command-line `--provider SPEC` flag overrides `default` for one run. See [Providers](providers.md).

## `[factory]`

| Key | Type | Default | Meaning |
|---|---|---|---|
| `max_iterations` | int | `5` | max coder → CI → review rounds before the build counts as `failed` |
| `approver` | `"cli"` \| `"auto"` \| `"reject"` | `"cli"` | who decides on changes that are not low risk. `auto` approves everything (sandboxes only), `reject` refuses everything |
| `deploy` | bool | `true` | `false` stops after approval with status `built` |
| `rollout_stages` | list of numbers | `[1, 5, 25, 50, 100]` | flag percentages the deploy steps through |
| `soak_seconds` | number | `0` | wait at each rollout stage before checking signals. Use a real value in production |
| `auto_mitigate` | bool | `false` | let Sevbot set a flag to 0 % on its own when it recommends a rollback |

## `[ci]` and `[ci.steps]`

```toml
[ci.steps]                 # run in order; the first failure stops CI and goes back to the coder
lint = "ruff check ."
types = "mypy src"
test = "python -m pytest -q -x"

[ci]
perf_command = "python bench.py"          # optional
perf_budgets = { p95_ms = 50, rss_mb = 300 }
```

| Key | Meaning |
|---|---|
| `steps` | name → shell command, run in the repo root. Non-zero exit = failure |
| `perf_command` | runs after the steps. Its **last line of output** must be a JSON object of numbers |
| `perf_budgets` | metric → maximum. Going over a budget fails CI like a failing test |

Each command has a 30-minute timeout.

## `[deploy]`

| Key | Meaning |
|---|---|
| `command` | shell command that ships the change. `{flag}` and `{outcome_id}` are filled in |
| `rollback` | shell command run when a guardrail is breached during rollout |

Without `command`, a no-op deployer is used. The flag rollout and signal checks still run,
which is useful against a staging environment that reads the local flag file.

Feature flags are stored in `.localagent/flags.json` by default. To use LaunchDarkly, Unleash or another
flag service, plug in a `FlagStore` (see [Extending](extending.md#feature-flags)).

## `[metrics]`

Where production numbers come from. They are used by the deploy guardrails, Perf Factory and Sevbot.

| `type` | Extra keys | Reads |
|---|---|---|
| `json` (default) | `path` (default `.localagent/metrics.json`) | a JSON object of numbers in a file |
| `command` | `command` | any command that prints a JSON object of numbers (wrap Datadog, CloudWatch, kubectl…) |
| `prometheus` | `url`, `queries = { name = "promql" }` | instant PromQL queries |
| `static` | `values = { name = number }` | fixed values (testing) |

## `[[guardrails]]`

Signals that always apply: during every rollout, and during `monitor`.

```toml
[[guardrails]]
metric = "error_rate"
max = 0.01
kind = "error"           # "error" | "availability" | "latency"

[[guardrails]]
metric = "success_ratio"
min = 0.995
```

If `kind` is omitted, it is guessed from the name: names containing `latency`, `p9` or `_ms` are
`latency`, everything else is `error`.

* **During a rollout**, every signal is checked, latency included. Any breach rolls the change back.
* **During `monitor`**, a breached `error`/`availability` signal opens a Sevbot incident.
  `latency` signals are skipped there: Perf Factory watches latency by comparing each metric with its
  own learned baseline, whether or not it is listed here. The deploy agent adds its own signals for each change.
Those stay active in `monitor` for the 5 most recent deploys that completed their rollout.

## `[risk]`

Overrides the fixed risk rules that sit underneath the reviewers' judgement:

```toml
[risk]
high_risk_paths = ["*migrations/*", "*auth*", "*.tf", "billing/*"]
medium_risk_paths = ["*config*", "*.yaml", "package.json"]
max_low_risk_lines = 200
```

The final risk is the highest of: the classifier's risk, the riskiest specialist's risk, and this rule set.
Any file matching `high_risk_paths` makes the change high risk. Setting a key replaces its default list
(see [Factory → Review](factory.md#5-agentic-review)).

## `[context]`

| Key | Meaning |
|---|---|
| `dirs` | extra folders (relative to the repo) of `.md`/`.txt`/`.rst` files to search: runbooks, ADRs, exported Slack or Notion pages, skill files |

The repository itself (its docs and code comments) and its git history are always included.

## `[improvement]`

| Key | Meaning |
|---|---|
| `strategy` | `"mass"`, `"noop"`, or the name of a strategy you registered |
| `config` | options passed to the strategy (MASS options below) |
| `trainer` | how MASS turns its datasets into a new model (below) |

### `[improvement.config]` for MASS

| Key | Default | Meaning |
|---|---|---|
| `iterations` | `4` | new workflow proposals per stage per cycle |
| `comparison` | `"reference"` | `"reference"`: proposals must beat a single-agent baseline, then the current best (as in the released code). `"best"`: proposals compete directly with the current best (the paper's Algorithm 1) |
| `reference` | `"bare"` | baseline: `"bare"` (one agent with the stage's base instruction) or `"current"` (the workflow in use) |
| `search_tasks` | `3` | training tasks each proposal is scored on |
| `candidates` | `16` | runs collected per training task with the winning workflow. `0` skips data collection |
| `opponents` | `6` | comparisons per run in the ranking tournament |
| `train_ranks` | `15` | top-ranked runs per task that become training data |
| `val_ranks` | `1` | runs after those that become validation data |
| `strip_workflow` | `true` | remove the team description from the orchestrator's prompt in the training data |
| `parallel` | `1` | tasks run concurrently while scoring a design |
| `seed` | `0` | base random seed |
| `role` | `"mass"` | router role prefix for the optimizer (`<role>.optimizer`) and judge (`<role>.judge`) |
| `judge_rubric` | `""` | extra instructions for the judge |
| `guardrails` | `{}` | rules for proposed workflows: `max_agents` (8), `min_agents` (0), `max_design_chars` (24000), `require_output_contracts` (true), `forbid_numeric_caps` (true), `required_agent_ids` ([]) |

Cost grows roughly with `search_tasks × (iterations + 2)` runs for the search, plus
`tasks × candidates` runs for collection, plus judge calls. Start small, e.g. `iterations = 2`,
`search_tasks = 2`, `candidates = 4`.

### `[improvement.trainer]`

| Key | Meaning |
|---|---|
| `type` | `"export"` (default: write datasets only) or `"command"` |
| `command` | shell command that trains a model. Placeholders: `{train}`, `{val}`, `{base_model}`, `{base_name}`, `{out_dir}`, `{generation}` |
| `model_spec` | provider spec of the trained model, e.g. `"vllm:my-org/coder-L{generation}"` |
| `serve_command` | optional command run after training, e.g. to restart vLLM on the new weights |

## A complete example

```toml
[providers]
default = "vllm:Qwen/Qwen3.6-27B"
[providers.routes]
"review.security" = "anthropic:claude-opus-5-5"
[providers.options.vllm]
base_url = "http://gpu-box:8000/v1"

[factory]
max_iterations = 6
approver = "cli"
rollout_stages = [1, 10, 50, 100]
soak_seconds = 300

[ci.steps]
lint = "ruff check ."
test = "python -m pytest -q"
[ci]
perf_command = "python bench.py --json"
perf_budgets = { p95_ms = 80 }

[deploy]
command = "./ops/deploy.sh {flag}"
rollback = "./ops/rollback.sh {flag}"

[metrics]
type = "prometheus"
url = "http://prometheus:9090"
queries = { error_rate = 'sum(rate(http_errors_total[5m]))/sum(rate(http_requests_total[5m]))', p95_ms = 'histogram_quantile(0.95, sum(rate(http_latency_seconds_bucket[5m])) by (le))*1000' }

[[guardrails]]
metric = "error_rate"
max = 0.01

[context]
dirs = ["docs/runbooks", "notes/slack-export"]

[improvement]
strategy = "mass"
[improvement.config]
iterations = 3
candidates = 8
train_ranks = 7
[improvement.trainer]
type = "command"
command = "python train_lora.py --train {train} --val {val} --base {base_name} --out {out_dir}"
model_spec = "vllm:coder-L{generation}"
serve_command = "./ops/restart_vllm.sh {out_dir}"
```
