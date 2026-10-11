"""``localagent`` command line."""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from .config import SAMPLE, build_factory, load_config
from .factory.outcome import Outcome
from .improvement.base import Task, available_strategies
from .providers.registry import available_providers


def _factory(args):
    cfg_path = Path(args.repo) / args.config
    cfg = load_config(cfg_path) if cfg_path.exists() else {}
    if args.provider:
        cfg.setdefault("providers", {})["default"] = args.provider
    return build_factory(cfg, args.repo, echo=not args.quiet)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser("localagent", description="Provider-agnostic agentic software factory")
    ap.add_argument("--repo", default=".", help="repository the factory works on")
    ap.add_argument("--config", default="localagent.toml")
    ap.add_argument("--provider", help="override default provider, e.g. ollama:qwen3:32b")
    ap.add_argument("-q", "--quiet", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="write a sample localagent.toml")
    b = sub.add_parser("build", help="builder defines an outcome -> code, CI, review, gate, deploy")
    b.add_argument("title")
    b.add_argument("-d", "--description", default="")
    b.add_argument("-c", "--criterion", action="append", default=[], help="acceptance criterion (repeatable)")
    b.add_argument("--no-deploy", action="store_true")
    b.add_argument("--follow", type=int, default=0, help="rounds of monitor -> perf-fix follow-ups")
    m = sub.add_parser("monitor", help="watch production signals (Perf Factory + Sevbot)")
    m.add_argument("--ticks", type=int, default=1)
    m.add_argument("--interval", type=float, default=60)
    m.add_argument("--build-fixes", action="store_true", help="send Perf Factory proposals to the coder")
    s = sub.add_parser("sevbot", help="ask Sevbot a question about production")
    s.add_argument("question")
    i = sub.add_parser("improve", help="run one cycle of the configured self-improvement strategy")
    i.add_argument("--stages", nargs="*", default=["coder"])
    t = sub.add_parser("task", help="add a task to the improvement task bank")
    t.add_argument("stage")
    t.add_argument("prompt")
    t.add_argument("--id", required=True)
    t.add_argument("--split", default="train", choices=["train", "test"])
    t.add_argument("--template-dir")
    w = sub.add_parser("workflows", help="show the current workflow of each stage")
    w.add_argument("stage", nargs="?")
    sub.add_parser("info", help="list providers and improvement strategies")
    args = ap.parse_args(argv)

    if args.cmd == "init":
        p = Path(args.repo) / args.config
        if p.exists():
            print(f"{p} exists; not overwriting")
            return 1
        p.write_text(SAMPLE)
        print(f"wrote {p}")
        return 0
    if args.cmd == "info":
        print("providers:  " + ", ".join(available_providers()))
        print("strategies: " + ", ".join(available_strategies()))
        return 0

    fac = _factory(args)
    if args.cmd == "build":
        outcome = Outcome(args.title, args.description, args.criterion)
        if args.follow:
            reports = fac.run(outcome, follow_up_rounds=args.follow)
        else:
            reports = [fac.build(outcome, deploy=False if args.no_deploy else None)]
        print(json.dumps([r.summary() for r in reports], indent=2))
        return 0 if reports[0].status in ("deployed", "built") else 2
    if args.cmd == "monitor":
        for k in range(args.ticks):
            rep = fac.monitor_once()
            print(json.dumps({"values": rep.values, "breaches": rep.breaches,
                              "proposals": [o.title for o in rep.proposals],
                              "incidents": [r.summary for r in rep.incidents]}, indent=2))
            if args.build_fixes:
                for o in rep.proposals:
                    print(json.dumps(fac.build(o).summary(), indent=2))
            if k + 1 < args.ticks:
                time.sleep(args.interval)
        return 0
    if args.cmd == "sevbot":
        print(fac.ask_sevbot(args.question))
        return 0
    if args.cmd == "improve":
        rep = fac.improve(args.stages)
        print(json.dumps(rep.to_dict(), indent=2, default=str))
        return 0
    if args.cmd == "task":
        fac.tasks.add(Task(args.id, args.stage, args.prompt, args.split, args.template_dir))
        print(f"added {args.stage}/{args.id} ({args.split})")
        return 0
    if args.cmd == "workflows":
        for stage in [args.stage] if args.stage else sorted(fac.defaults):
            print(fac.workflow(stage).render() + "\n")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
