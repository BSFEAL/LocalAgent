# Getting started

## Requirements

* Python **3.11+**. There are no third-party dependencies.
* `git` is recommended. It is used for context and for exporting a clean copy of your repo during self-improvement.
* Access to at least one model. That can be a local server (Ollama, vLLM, LM Studio) or a hosted API key
  (OpenAI, Anthropic, Gemini, OpenRouter…).

## Install

```bash
git clone <this repo> && cd LocalAgent
pip install -e .            # installs the `localagent` command
# or run without installing:
python -m localagent info
```

`localagent info` lists the available providers and improvement strategies:

```
providers:  anthropic, compat, echo, gemini, lmstudio, mock, ollama, openai, openrouter, vllm
strategies: mass, noop
```

## 1. See it work offline (no model needed)

```bash
python examples/offline_demo.py
```

The demo creates a throwaway repo and runs the whole loop with a scripted fake model:

1. builds and deploys a small feature (`status: deployed`);
2. simulates a latency spike and an error spike. Perf Factory proposes a fix, and Sevbot diagnoses the error spike and switches the flag off;
3. runs one MASS cycle. The single-agent coder loses to a proposed two-agent team, which is kept.

The test suite also runs offline:

```bash
python -m unittest discover -s tests -t .
```

## 2. Point it at your repository

From the root of the repository you want agents to work on:

```bash
localagent init          # writes ./localagent.toml
```

Then edit three sections of `localagent.toml`:

```toml
[providers]
default = "ollama:qwen3:32b"           # or "anthropic:claude-opus-5-5", "openai:gpt-5.5", ...

[ci.steps]                             # what "green" means for your project, in order
lint = "ruff check ."
test = "python -m pytest -q"

[factory]
approver = "cli"                       # a human confirms anything that is not low risk
deploy = false                         # start with build-only; turn on once [deploy] is set up
```

If you use a hosted provider, export its key (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`,
`GEMINI_API_KEY`, `OPENROUTER_API_KEY`). See [Providers](providers.md).

## 3. Build your first outcome

```bash
localagent build "Add a /health endpoint" \
  -d "Return JSON {status: ok, version} from the web app" \
  -c "GET /health returns 200 with status=ok" \
  -c "a test covers it"
```

What happens:

1. **Coder** reads your code and docs, edits files and runs your checks.
2. **CI** runs each `[ci.steps]` command. A failure goes back to the coder along with its output.
3. **Review**: four specialist reviewers read the diff in parallel, and a classifier assigns a risk level.
4. **Gate**: if the change is low risk and approved it continues automatically. Otherwise you are
   asked in the terminal, can view the diff, and approve or reject.
5. **Deploy** runs only when deploying is enabled. With `deploy = false` the run ends with `status: built`.

The command prints a summary and exits with `0` on `deployed` or `built`, and `2` otherwise:

```json
[{"outcome": "Add a /health endpoint", "status": "built", "iterations": 2,
  "changes": "2 files, +41/-0", "ci_passed": true, "risk": "low",
  "approved_by": "agentic-review", "deploy": null}]
```

The changes stay in your working tree, uncommitted. Review them with `git diff` and commit them as usual.

## 4. Turn on deploys and monitoring

Once you have a deploy script and a metrics source, configure them:

```toml
[factory]
deploy = true

[deploy]
command  = "./scripts/deploy.sh {flag}"
rollback = "./scripts/rollback.sh {flag}"

[metrics]
type = "command"
command = "./scripts/metrics.sh"       # prints {"error_rate": 0.002, "p95_ms": 41}

[[guardrails]]
metric = "error_rate"
max = 0.01
```

Then watch production and send performance fixes back to the coder:

```bash
localagent monitor --ticks 60 --interval 60 --build-fixes
localagent sevbot "what changed before the 14:00 error spike?"
```

## 5. Let the factory improve itself

Every outcome you build is saved as a training task. When you have a few, run:

```bash
localagent improve --stages coder
localagent workflows coder        # shows the agent team now in use
```

With the default settings this improves the **coder's agent team**, using your configured model,
and writes fine-tuning datasets. To also produce a fine-tuned model, configure a trainer.
See [Self-improvement](self-improvement.md).

## Where things are stored

Everything the factory records lives in `<repo>/.localagent/`. Add it to `.gitignore`.
[Concepts → State directory](concepts.md#state-directory) explains each file.
