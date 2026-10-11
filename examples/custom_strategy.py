"""Plugging a different paper into the slot.

This toy strategy ("best-of-n workflow sampling") shows the whole contract:
read tasks + the current workflow from the context, evaluate candidates with
the stage executor, and retain a winner. Swap in any method the same way.

    from examples.custom_strategy import BestOfN  # registers "best_of_n"
    factory.improvement = create_strategy("best_of_n", n=3)
    factory.improve(["coder"])
"""
from __future__ import annotations

from localagent.core.workflow import Workflow
from localagent.improvement import (ImprovementContext, ImprovementReport, ImprovementStrategy,
                                    register_strategy)
from localagent.improvement.mass import PairwiseJudge, StructuralGuardrails, WorkflowOptimizer


class BestOfN(ImprovementStrategy):
    name = "best_of_n"

    def run_cycle(self, ctx: ImprovementContext, stages: list[str]) -> ImprovementReport:
        n = int(self.config.get("n", 3))
        report = ImprovementReport(self.name)
        model = ctx.router.for_role("improve")
        judge = PairwiseJudge(model)
        optimizer = WorkflowOptimizer(model, StructuralGuardrails())
        for stage in stages:
            tasks = ctx.tasks.get(stage, "train")[:1]
            if not tasks:
                continue
            task, executor, best = tasks[0], ctx.executor(stage), ctx.workflow(stage)
            best_exec = executor.execute(best, task, seed=0)
            for i in range(n):
                cand: Workflow | None
                cand, _ = optimizer.propose(task_texts=[task.prompt], current=best, best=None, v0=best,
                                            evidence=best_exec.deliverable[:5000], history=[],
                                            new_version=ctx.workflows.next_version(stage), seed=i)
                if cand is None:
                    continue
                ctx.workflows.save(stage, cand)
                ex = executor.execute(cand, task, seed=i + 1)
                if judge.compare(task.prompt, best_exec, ex).winner == "B":
                    best, best_exec = cand, ex
            ctx.workflows.save(stage, best, make_current=True)
            report.stages[stage] = {"retained": best.version}
        return report


register_strategy("best_of_n", BestOfN)
