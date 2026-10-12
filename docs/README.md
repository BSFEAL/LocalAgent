# LocalAgent documentation

LocalAgent is an agentic software factory. You describe an **outcome**. Agents write the code,
get it through CI and an AI code review, deploy it behind a feature flag, and watch it in
production. A pluggable **self-improvement strategy** then makes the factory better over
time, learning from the factory's own history. MASS is the default strategy.

It runs on any model provider, hosted or local, and needs nothing beyond the Python standard library.

## Start here

| If you want to… | Read |
|---|---|
| Install it and run your first outcome | [Getting started](getting-started.md) |
| Learn the vocabulary (outcome, workflow, stage, trajectory…) | [Concepts](concepts.md) |
| Connect OpenAI, Anthropic, Gemini, Ollama, vLLM… | [Providers](providers.md) |
| See every `localagent.toml` option | [Configuration reference](configuration.md) |
| See every command and flag | [CLI reference](cli.md) |
| Understand each factory stage and how to tune it | [The factory, stage by stage](factory.md) |
| Understand MASS and the paper slot | [Self-improvement](self-improvement.md) |
| Plug in your own CI, deployer, metrics, approver, provider, strategy | [Extending LocalAgent](extending.md) |
| See how the pieces map to the diagram and the paper | [Architecture](architecture.md) |
| Fix something that isn't working | [Troubleshooting & FAQ](troubleshooting.md) |

## The loop in one picture

```
 builder ──outcome──▶ coder ──▶ CI ──▶ review (data · infra · cloud · security + risk)
                        ▲       │fail      │blocking findings
                        └───────┴──────────┘
                                          │ approved
                     low risk ────────────┼──────────── otherwise → human approver
                                          ▼
                   deploy: flag 1→5→25→50→100 %, guardrail checks, auto-rollback
                                          ▼
                   production ──latency regression──▶ Perf Factory ──new outcome──▶ coder
                              └──errors/outage──────▶ Sevbot (investigate, mitigate, answer)

   self-improvement strategy, run offline: improves each stage's agent team (and optionally the model)
```
