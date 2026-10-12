"""Recursive Harness Improvement (RHI): MASS's workflow search with fixed weights.

The current model executes a workflow, the same model judges the result
pairwise, and the same model proposes the next design. Two comparison rules:

* ``"reference"`` (the released code): each proposal is compared with a fixed
  bare single-agent reference; a proposal that beats it must also beat the
  reigning champion in a direct arbitration to take over.
* ``"best"`` (the paper's Algorithm 1): each proposal is compared directly
  with the retained best output (the reference until something beats it).

Adaptation for the factory: the paper searches one workflow per task. A
factory stage serves many tasks, so each design is evaluated on a minibatch of
the stage's training tasks and "beats" means more pairwise wins than losses.
With a single task this reduces to the paper's per-task search.
"""
from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from ...core.workflow import Workflow
from ..base import Execution, Task, TaskExecutor
from .judge import PairwiseJudge
from .optimizer import HistoryRow, WorkflowOptimizer


@dataclass
class SearchResult:
    stage: str
    retained: Workflow | None
    reference: Workflow
    proposals: list[Workflow] = field(default_factory=list)
    history: list[HistoryRow] = field(default_factory=list)
    rejected: list[list[str]] = field(default_factory=list)  # guardrail errors per failed proposal
    output_tokens: dict[str, float] = field(default_factory=dict)  # mean per design tag


def evidence_text(execs: list[Execution], max_chars: int = 30_000) -> str:
    per = max_chars // max(1, len(execs))
    out = []
    for e in execs:
        u = e.trajectory.usage
        convs = ", ".join(f"{c.agent_id}({c.role}, {len([m for m in c.messages if m.role == 'assistant'])} turns)"
                          for c in e.trajectory.conversations)
        out.append(f"## Task {e.task.id}\nchecks={e.checks} output_tokens={u.output_tokens} agents=[{convs}]\n"
                   f"{e.deliverable[:per]}")
    return "\n\n".join(out)


class RecursiveHarnessImprovement:
    def __init__(self, executor: TaskExecutor, judge: PairwiseJudge, optimizer: WorkflowOptimizer, *,
                 iterations: int = 4, comparison: str = "reference", seed: int = 0, parallel: int = 1,
                 on_proposal: Callable[[Workflow], None] | None = None,
                 log: Callable[[str], None] | None = None):
        if comparison not in ("reference", "best"):
            raise ValueError("comparison must be 'reference' or 'best'")
        self.executor, self.judge, self.optimizer = executor, judge, optimizer
        self.iterations, self.comparison, self.seed, self.parallel = iterations, comparison, seed, parallel
        self.on_proposal = on_proposal or (lambda wf: None)
        self.log = log or (lambda s: None)

    def _run(self, wf: Workflow, tasks: list[Task], salt: int) -> list[Execution]:
        def one(i_t: tuple[int, Task]) -> Execution:
            i, t = i_t
            return self.executor.execute(wf, t, seed=self.seed + salt * 1000 + i)
        if self.parallel > 1:
            with ThreadPoolExecutor(self.parallel) as pool:
                return list(pool.map(one, enumerate(tasks)))
        return [one(x) for x in enumerate(tasks)]

    def _compare(self, version: int, against: str, base: list[Execution], cand: list[Execution],
                 changes: list[str]) -> HistoryRow:
        row = HistoryRow(version, against, 0, 0, 0, changes=changes)
        for i, (a, b) in enumerate(zip(base, cand)):
            v = self.judge.compare(a.task.prompt, a, b, seed=self.seed + version * 100 + i)
            if v.winner == "B":
                row.wins += 1
            elif v.winner == "A":
                row.losses += 1
            else:
                row.ties += 1
            row.rationales.append(v.rationale)
        return row

    def search(self, stage: str, tasks: list[Task], initial: Workflow, reference: Workflow,
               first_version: int) -> SearchResult:
        res = SearchResult(stage, None, reference)
        ref_execs = self._run(reference, tasks, salt=0)
        res.output_tokens[reference.tag] = sum(e.output_tokens for e in ref_execs) / len(ref_execs)

        v0 = initial
        current, cur_execs = v0, self._run(v0, tasks, salt=1)
        res.output_tokens[v0.tag] = sum(e.output_tokens for e in cur_execs) / len(cur_execs)
        row = self._compare(v0.version, "reference", ref_execs, cur_execs, ["initial design"])
        res.history.append(row)
        champion, champ_execs = (v0, cur_execs) if row.won else (None, None)
        self.log(f"[rhi:{stage}] v{v0.version} vs reference: W{row.wins}/L{row.losses}/T{row.ties}")

        next_version = first_version
        for k in range(1, self.iterations + 1):
            proposal, errors = self.optimizer.propose(
                task_texts=[t.prompt for t in tasks], current=current, best=champion, v0=v0,
                evidence=evidence_text(cur_execs), history=res.history, new_version=next_version,
                seed=self.seed + k)
            if proposal is None:
                res.rejected.append(errors)
                self.log(f"[rhi:{stage}] proposal rejected by guardrails: {errors}")
                continue
            next_version += 1
            res.proposals.append(proposal)
            self.on_proposal(proposal)
            execs = self._run(proposal, tasks, salt=k + 1)
            res.output_tokens[proposal.tag] = sum(e.output_tokens for e in execs) / len(execs)

            if self.comparison == "best" and champion is not None:
                row = self._compare(proposal.version, f"champion v{champion.version}", champ_execs, execs,
                                    proposal.notes)
                res.history.append(row)
                if row.won:
                    champion, champ_execs = proposal, execs
            else:
                row = self._compare(proposal.version, "reference", ref_execs, execs, proposal.notes)
                res.history.append(row)
                if row.won:
                    if champion is None:
                        champion, champ_execs = proposal, execs
                    else:
                        arb = self._compare(proposal.version, f"champion v{champion.version}", champ_execs,
                                            execs, ["champion arbitration"])
                        res.history.append(arb)
                        if arb.won:
                            champion, champ_execs = proposal, execs
            self.log(f"[rhi:{stage}] v{proposal.version}: {row.against} W{row.wins}/L{row.losses}/T{row.ties}; "
                     f"champion={'v%d' % champion.version if champion else 'none'}")
            current, cur_execs = proposal, execs
        res.retained = champion
        return res
