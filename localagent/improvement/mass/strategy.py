"""MASS as an ImprovementStrategy: RHI -> collect -> rank -> select -> post-train -> L^(k+1)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ...core.workflow import Workflow
from ..base import ImprovementContext, ImprovementReport, ImprovementStrategy, register_strategy
from .guardrails import StructuralGuardrails
from .judge import PairwiseJudge
from .optimizer import WorkflowOptimizer
from .rhi import RecursiveHarnessImprovement
from .sft import collect, rank, select, write_dataset
from .trainer import ExportOnlyTrainer, Trainer

DEFAULTS: dict[str, Any] = {
    "iterations": 4,             # RHI proposals per stage
    "comparison": "reference",   # "reference" (released code) | "best" (Algorithm 1)
    "reference": "bare",         # "bare" single agent | "current" retained workflow
    "search_tasks": 3,           # minibatch of train tasks per design evaluation
    "candidates": 16,            # teacher trajectories per task (paper: 18 collected, 16 ranked)
    "opponents": 6,              # tournament degree
    "train_ranks": 15,           # ranks 1..15 -> train
    "val_ranks": 1,              # rank 16 -> validation
    "strip_workflow": True,      # remove the design from the orchestrator prompt
    "seed": 0,
    "parallel": 1,
    "role": "mass",              # router role used as executor/optimizer/judge = the current model
    "guardrails": {},
    "judge_rubric": "",
}


class MassStrategy(ImprovementStrategy):
    """Multi-Agent Self-Supervision (Lee et al., arXiv:2610.12176).

    The current model plays executor, workflow optimizer and evaluator. RHI
    retains a better multi-agent workflow per stage with weights fixed; the
    retained workflow's best self-ranked trajectories become SFT data with the
    workflow stripped from the prompt, so the next generation internalises it.
    """

    name = "mass"

    def __init__(self, trainer: Trainer | None = None, **config: Any):
        super().__init__(**{**DEFAULTS, **config})
        self.trainer = trainer or ExportOnlyTrainer()

    def _log(self, ctx: ImprovementContext, msg: str) -> None:
        ctx.bus.emit("improve.log", message=msg)

    def run_cycle(self, ctx: ImprovementContext, stages: list[str]) -> ImprovementReport:
        c = self.config
        report = ImprovementReport(self.name)
        run_dir = ctx.run_dir / f"mass-{report.run_id}"
        model = ctx.router.for_role(c["role"])
        judge = PairwiseJudge(ctx.router.for_role(f"{c['role']}.judge"), rubric=c["judge_rubric"])
        all_train, all_val = [], []

        for stage in stages:
            tasks = ctx.tasks.get(stage, "train")
            if not tasks:
                report.stages[stage] = {"skipped": "no train tasks"}
                continue
            current = ctx.workflow(stage)
            reference = (current if c["reference"] == "current" else
                         Workflow.single(stage, current.orchestrator.instruction, current.orchestrator.tools))
            reference.version = -1 if c["reference"] == "bare" else current.version
            executor = ctx.executor(stage)
            optimizer = WorkflowOptimizer(ctx.router.for_role(f"{c['role']}.optimizer"),
                                          StructuralGuardrails(**c["guardrails"]))
            rhi = RecursiveHarnessImprovement(
                executor, judge, optimizer, iterations=c["iterations"], comparison=c["comparison"],
                seed=c["seed"], parallel=c["parallel"],
                on_proposal=lambda wf, s=stage: ctx.workflows.save(s, wf),
                log=lambda m: self._log(ctx, m))
            search_tasks = tasks[: c["search_tasks"]]
            res = rhi.search(stage, search_tasks, current, reference, ctx.workflows.next_version(stage))
            stage_rep: dict[str, Any] = {
                "searched_on": [t.id for t in search_tasks],
                "proposals": [p.version for p in res.proposals],
                "rejected_proposals": len(res.rejected),
                "history": [r.to_dict() for r in res.history],
                "mean_output_tokens": res.output_tokens,
                "model": model.spec,
            }
            if res.retained is None:
                stage_rep["retained"] = None
                stage_rep["note"] = "no design beat the reference; stage excluded from teacher collection"
                report.stages[stage] = stage_rep
                self._log(ctx, f"[mass:{stage}] nothing retained")
                continue
            ctx.workflows.save(stage, res.retained, make_current=True)
            stage_rep["retained"] = res.retained.version
            ctx.bus.emit("improve.retained", stage=stage, version=res.retained.version)

            if c["candidates"] > 0:
                collected = collect(executor, res.retained, tasks, c["candidates"], c["seed"])
                for execs in collected.values():
                    for e in execs:
                        ctx.trajectories.save(e.trajectory)
                ranked = [rank(judge, t, collected[t.id], c["opponents"], c["seed"]) for t in tasks]
                train, val = select(ranked, c["train_ranks"], c["val_ranks"])
                all_train += train
                all_val += val
                stage_rep["collected"] = sum(len(v) for v in collected.values())
                stage_rep["selected"] = {"train": len(train), "val": len(val)}
            report.stages[stage] = stage_rep

        if all_train:
            tr = write_dataset(run_dir / "sft_train.jsonl", all_train, strip_workflow=c["strip_workflow"])
            va = write_dataset(run_dir / "sft_val.jsonl", all_val, strip_workflow=c["strip_workflow"])
            report.artifacts.update(train=tr["path"], val=va["path"])
            report.stages["_data"] = {"train": tr, "val": va}
            gen = self._generation(ctx) + 1
            result = self.trainer.train(Path(tr["path"]), Path(va["path"]), model.spec, run_dir / "model", gen)
            if result.model_spec:
                report.new_model = result.model_spec
                ctx.router.set("default", result.model_spec)
                self._record_generation(ctx, gen, result.model_spec, report.run_id)
                ctx.bus.emit("improve.new_model", generation=gen, model=result.model_spec)
            report.artifacts["trainer"] = result.detail

        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "report.json").write_text(json.dumps(report.to_dict(), indent=2, default=str))
        report.artifacts["report"] = str(run_dir / "report.json")
        return report

    @staticmethod
    def _gen_file(ctx: ImprovementContext) -> Path:
        return ctx.run_dir / "generations.json"

    def _generation(self, ctx: ImprovementContext) -> int:
        p = self._gen_file(ctx)
        return len(json.loads(p.read_text())) if p.exists() else 0

    def _record_generation(self, ctx: ImprovementContext, gen: int, spec: str, run_id: str) -> None:
        p = self._gen_file(ctx)
        rows = json.loads(p.read_text()) if p.exists() else []
        rows.append({"generation": gen, "model": spec, "run_id": run_id})
        p.write_text(json.dumps(rows, indent=2))


register_strategy("mass", MassStrategy)

__all__ = ["MassStrategy", "DEFAULTS"]
