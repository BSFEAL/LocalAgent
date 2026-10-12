# CLI reference

```
localagent [--repo PATH] [--config FILE] [--provider SPEC] [-q] <command> ...
```

| Global option | Default | Meaning |
|---|---|---|
| `--repo` | `.` | repository the factory works on (state goes in `<repo>/.localagent/`) |
| `--config` | `localagent.toml` | config file, relative to `--repo` |
| `--provider` | from config | override the default provider for this run, e.g. `--provider anthropic:claude-opus-5-5` |
| `-q`, `--quiet` | off | don't print events as they happen |

`python -m localagent …` works the same without installing.

## `init`

Write a commented starter `localagent.toml`. It won't overwrite an existing file.

## `info`

List registered providers and improvement strategies.

## `build`

```
localagent build TITLE [-d DESCRIPTION] [-c CRITERION]... [--no-deploy] [--follow N]
```

Runs one outcome through coder → CI → review → gate → deploy.

| Option | Meaning |
|---|---|
| `-d`, `--description` | longer description of the outcome |
| `-c`, `--criterion` | acceptance criterion; repeat for several |
| `--no-deploy` | stop after approval (status `built`) |
| `--follow N` | after a successful deploy, check production once and build up to N follow-up fixes proposed by Perf Factory |

Prints a JSON list of build summaries. Exit code `0` if the (first) outcome ends `deployed` or
`built`, otherwise `2`.

Possible statuses:

| Status | Meaning |
|---|---|
| `deployed` | rolled out to 100 % with no guardrail breach |
| `built` | approved; deploy disabled |
| `rolled_back` | a guardrail was breached during rollout; flag set to 0 % and `rollback` run |
| `rejected` | the human approver (or `approver = "reject"`) said no |
| `failed` | still red (CI or blocking review findings) after `max_iterations`, or the deploy command failed |
| `no_changes` | the coder never changed a file |

## `monitor`

```
localagent monitor [--ticks N] [--interval SECONDS] [--build-fixes]
```

Reads metrics `N` times, `SECONDS` apart (defaults 1 and 60). On each tick:

* latency metrics are compared with their baselines. New regressions go to Perf Factory, which proposes a fix outcome;
* breached error/availability signals go to Sevbot, which writes an incident report.

`--build-fixes` builds each proposed fix straight away.

## `sevbot`

```
localagent sevbot "QUESTION"
```

Ask about production. Sevbot can read metrics, recent deploys, flag states, git history, the code and your docs.

## `improve`

```
localagent improve [--stages STAGE ...]
```

Runs one cycle of the configured improvement strategy over the given stages (default `coder`),
using the `train` tasks in the task bank. Prints the strategy's report as JSON.

## `task`

```
localagent task STAGE PROMPT --id ID [--split train|test] [--template-dir DIR]
```

Adds a task to the task bank. `--template-dir` is copied into a fresh workspace for every run of
the task. Without it the workspace starts empty, unless the task came from `build`, which pins the repo revision.
Adding a task with an existing `STAGE`/`ID` replaces it.

## `workflows`

```
localagent workflows [STAGE]
```

Prints the current workflow (agent team) of one stage or of all stages.
