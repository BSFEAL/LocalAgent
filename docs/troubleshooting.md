# Troubleshooting & FAQ

## Errors

**`ProviderError: cannot reach http://127.0.0.1:11434/v1/...`**
The local model server isn't running, or it's on another address. Start it (`ollama serve`,
`vllm serve <model>`) or set `OLLAMA_BASE_URL` / `VLLM_BASE_URL` / `[providers.options.<name>] base_url`.
Each failing request is retried 4 times with exponential backoff before this error appears.

**`ProviderError: HTTP 401/403 ...`**
The API key is missing or wrong. Check `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`,
`OPENROUTER_API_KEY`, or the `api_key` option.

**`ValueError: provider spec must look like 'provider:model'`**
Write the full spec, e.g. `ollama:llama3.3`, not `llama3.3`.

**`ValueError: unknown improvement strategy 'x'`**
The module that registers the strategy wasn't imported. See
[Self-improvement → Writing your own strategy](self-improvement.md#writing-your-own-strategy-plugging-in-another-paper).

**Build ends `no_changes`**
The model answered without calling `write_file`/`edit_file`. Usually the model is too weak at tool
calling, or the provider's tool-calling support is off. Ollama needs a model with tool support;
vLLM needs `--enable-auto-tool-choice --tool-call-parser <parser>`.

**Build ends `failed` after `max_iterations`**
Check the `ci.failed` events in `.localagent/events.jsonl` and the last coder trajectory in
`.localagent/trajectories/coder.jsonl`. Common causes are a CI command that fails before any change
(fix CI first), or a flaky test.

**Everything is "high" risk**
Look at the `reasons` of the `review.done` event in `.localagent/events.jsonl`. Either a sensitive path matched
`RiskPolicy.high_risk_paths` (e.g. any path containing `auth` or `deploy`), or a reviewer's answer could
not be parsed (weak models sometimes return prose). Narrow the patterns under `[risk]`, or route `review`
to a stronger model.

**Every rollout is rolled back**
A guardrail is breached even before the change: compare `localagent monitor` output with your
thresholds. Set `soak_seconds` so metrics have time to reflect the new flag percentage.

**Perf Factory never fires**
It needs a baseline first (the first reading of each metric). The metric name must contain one of `latency`,
`p50`, `p90`, `p95`, `p99`, `_ms` or `duration`. And the rise must be more than 20 %.

## FAQ

**Does it commit or push my code?**
No. Changes stay in your working tree. Committing, pushing and opening PRs are up to you, or to
your deploy command.

**Can agents touch files outside the repository?**
File tools refuse any path that leaves the workspace. `run_command` runs a real shell with the repo as
working directory, so use a container or VM for untrusted setups, and/or
`Workspace(shell_allowlist=["pytest", "ruff"])`.

**Can I mix providers?**
Yes. That is what [routing](providers.md#routing) is for. Example: coding on a local model,
security review on a hosted frontier model.

**Do I need a GPU or fine-tuning for self-improvement?**
No. With the default export-only trainer, MASS still improves each stage's agent team using your
current model, and writes datasets you can train on later.

**How much does a MASS cycle cost?**
Roughly `search_tasks × (iterations + 2)` stage runs, plus `train_tasks × candidates` runs, plus two
judge calls per comparison. Start with `iterations = 2`, `search_tasks = 2`, `candidates = 4`.

**How do I go back to an earlier workflow?**
Write the version number into `.localagent/workflows/<stage>/current`. All versions are kept.

**How do I go back to an earlier model generation?**
Remove the last entry from `.localagent/improvement/generations.json`. The newest remaining entry
is loaded at start-up; with none left, the configured `default` is used.

**Is this an exact reproduction of the MASS paper?**
No. It is a faithful adaptation to a software factory. The differences are listed in
[Architecture → Deviations](architecture.md#deviations).
