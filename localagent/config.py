"""Build a Factory from ``localagent.toml``. Every external system is chosen here."""
from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from .core.context import ContextHub, DirectorySource, GitSource
from .factory.ci import CommandCI
from .factory.deploy import CommandDeployer, NoopDeployer
from .factory.gates import AutoApprover, CLIApprover
from .factory.observability import CommandMetrics, JsonFileMetrics, PrometheusMetrics, Signal, StaticMetrics
from .factory.pipeline import Factory
from .factory.review import RiskPolicy
from .improvement.base import create_strategy
from .improvement.mass.trainer import CommandTrainer, ExportOnlyTrainer
from .providers.registry import ProviderRouter

SAMPLE = '''# LocalAgent factory configuration.
# Any provider works: openai:, anthropic:, gemini:, vllm:, ollama:, lmstudio:, openrouter:, compat:, mock:

[providers]
default = "ollama:qwen3:32b"

[providers.routes]            # optional per-role overrides (dotted roles fall back to their prefix)
# "review.security" = "anthropic:claude-opus-5-5"
# sevbot = "openai:gpt-5.5"
# mass = "vllm:Qwen/Qwen3.6-27B"   # MASS: one model = executor, optimizer and judge

# [providers.options.vllm]
# base_url = "http://127.0.0.1:8000/v1"

[factory]
max_iterations = 5            # coder <-> CI/review fix loop
approver = "cli"              # cli | auto (sandbox only) | reject
deploy = true
rollout_stages = [1, 5, 25, 50, 100]
soak_seconds = 0
auto_mitigate = false         # let Sevbot flip flags off on its own

[ci.steps]                    # run in order; first failure goes back to the coder
# lint = "ruff check ."
# test = "python -m pytest -q"

[ci]
# perf_command = "python bench.py"     # must print a JSON object of metrics as its last line
# perf_budgets = { p95_ms = 50 }

[deploy]
# command = "./scripts/deploy.sh {flag}"
# rollback = "./scripts/rollback.sh {flag}"

[metrics]
type = "json"                 # json | command | prometheus | static
path = ".localagent/metrics.json"

# [[guardrails]]
# metric = "error_rate"
# max = 0.01
# kind = "error"

[context]
dirs = []                     # extra folders of notes/runbooks/skills (e.g. Slack/Notion exports)

[improvement]
strategy = "mass"             # the paper slot: mass | noop | your registered strategy

[improvement.config]
iterations = 4
comparison = "reference"      # "reference" (released code) | "best" (paper Algorithm 1)
search_tasks = 3
candidates = 16
train_ranks = 15

[improvement.trainer]
type = "export"               # export | command
# command = "axolotl train cfg.yml --train {train} --val {val} --base {base_name} --out {out_dir}"
# model_spec = "vllm:my-org/model-L{generation}"
'''


def load_config(path: str | Path) -> dict[str, Any]:
    return tomllib.loads(Path(path).read_text())


def _metrics(cfg: dict[str, Any], repo: Path):
    t = cfg.get("type", "json")
    if t == "json":
        return JsonFileMetrics(repo / cfg.get("path", ".localagent/metrics.json"))
    if t == "command":
        return CommandMetrics(cfg["command"])
    if t == "prometheus":
        return PrometheusMetrics(cfg["url"], cfg.get("queries", {}))
    return StaticMetrics(cfg.get("values", {}))


def build_factory(cfg: dict[str, Any], repo: str | Path = ".", echo: bool = True) -> Factory:
    repo = Path(repo).resolve()
    p = cfg.get("providers", {})
    router = ProviderRouter(p.get("default", "ollama:qwen3:32b"), p.get("routes", {}), p.get("options", {}))
    f = cfg.get("factory", {})
    ci = cfg.get("ci", {})
    d = cfg.get("deploy", {})
    approver = {"auto": AutoApprover(True), "reject": AutoApprover(False)}.get(f.get("approver", "cli"), CLIApprover())
    ctx = ContextHub([DirectorySource(repo, "repo-docs"), GitSource(repo)])
    for extra in cfg.get("context", {}).get("dirs", []):
        ctx.add(DirectorySource(repo / extra, Path(extra).name))
    imp = cfg.get("improvement", {})
    tcfg = imp.get("trainer", {})
    trainer = (CommandTrainer(tcfg["command"], tcfg["model_spec"], serve_command=tcfg.get("serve_command"))
               if tcfg.get("type") == "command" else ExportOnlyTrainer())
    name = imp.get("strategy", "noop")
    strategy = create_strategy(name, **({"trainer": trainer} if name == "mass" else {}), **imp.get("config", {}))
    risk = cfg.get("risk", {})
    return Factory(
        repo, router,
        ci=CommandCI(ci.get("steps", {}), ci.get("perf_command"), ci.get("perf_budgets", {})),
        approver=approver,
        deployer=CommandDeployer(d["command"], d.get("rollback", "")) if d.get("command") else NoopDeployer(),
        metrics=_metrics(cfg.get("metrics", {}), repo), context=ctx, improvement=strategy,
        risk_policy=RiskPolicy(**risk) if risk else None,
        guardrails=[Signal.from_dict(g) for g in cfg.get("guardrails", [])],
        max_iterations=f.get("max_iterations", 5), rollout_stages=tuple(f.get("rollout_stages", (1, 5, 25, 50, 100))),
        soak_seconds=f.get("soak_seconds", 0), auto_mitigate=f.get("auto_mitigate", False),
        deploy_enabled=f.get("deploy", True), echo=echo)
