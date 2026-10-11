"""The workflow optimizer role: the current model rewrites a stage's multi-agent design."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from ...core.workflow import AgentSpec, Workflow
from ...providers.base import Provider, ProviderError
from .guardrails import StructuralGuardrails

OPTIMIZER_SYSTEM = """You are a principal engineer who designs multi-agent workflows for autonomous agents.
You improve ONLY the multi-agent design (orchestrator, worker roster, output contracts, protocol) so the
deliverables of the task(s) get better.
Prioritise what multi-agent systems can do and single agents cannot: parallel specialist decomposition,
cross-agent critique and validation, agent-to-agent communication, iterative reconciliation.
In particular strengthen (1) each worker's 'output to orchestrator' contract so downstream agents can
consume it, and (2) orchestrator-worker feedback loops: explicit recall of earlier workers with narrower
follow-up scopes and updated acceptance criteria, instead of one-pass execution.
Terminate through acceptance gates, never through arbitrary numeric caps on hops, calls, turns or time.
Every change must be grounded in concrete evidence from the executions and the comparison history;
explain the mechanism, not generic claims."""

OUTPUT_SCHEMA = """Return JSON:
{
  "design": {
    "orchestrator": {"id": "...", "role": "...", "instruction": "...", "tools": null},
    "agents": [{"id": "...", "role": "...", "instruction": "...", "output_contract": "...", "tools": null}],
    "protocol": "..."
  },
  "changes_from_previous": ["..."],
  "why_it_should_improve": ["..."],
  "evidence_used": ["..."],
  "verification_checks": ["..."]
}
`tools` is null (all tools) or a subset of the available tool names."""


@dataclass
class HistoryRow:
    version: int
    against: str  # "reference" | "champion v3" | ...
    wins: int
    losses: int
    ties: int
    rationales: list[str] = field(default_factory=list)
    changes: list[str] = field(default_factory=list)

    @property
    def won(self) -> bool:
        return self.wins > self.losses

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def _history_text(rows: list[HistoryRow], max_rationale: int = 600) -> str:
    if not rows:
        return "No comparisons yet."
    out = []
    for r in rows:
        out.append(f"- v{r.version} vs {r.against}: {'WIN' if r.won else 'LOSS/TIE'} "
                   f"(W{r.wins}/L{r.losses}/T{r.ties})")
        if r.changes:
            out.append("  changes: " + "; ".join(r.changes)[:max_rationale])
        for rat in r.rationales[:2]:
            out.append("  judge: " + rat[:max_rationale])
    return "\n".join(out)


def _design_json(wf: Workflow) -> str:
    return json.dumps(wf.design_dict(), indent=1)


class WorkflowOptimizer:
    def __init__(self, provider: Provider, guardrails: StructuralGuardrails, *, retries: int = 2,
                 tool_names: list[str] | None = None):
        self.provider, self.guardrails, self.retries = provider, guardrails, retries
        self.tool_names = tool_names or []

    def build_prompt(self, task_texts: list[str], current: Workflow, best: Workflow | None, v0: Workflow,
                     evidence: str, history: list[HistoryRow]) -> str:
        sections = ["# Task(s) this stage must solve", "\n---\n".join(t[:4000] for t in task_texts[:4]),
                    f"# Current design (v{current.version})", _design_json(current)]
        if best is not None and best.version != current.version:
            sections += [f"# Best design so far (v{best.version})",
                         "Shown because comparisons judged it better than the current design. Keep what made it "
                         "strong and merge in only the genuinely useful changes attempted since.", _design_json(best)]
        if v0.version not in (current.version, getattr(best, "version", None)):
            sections += ["# Original design (v0, root of this lineage; do not revert wholesale)", _design_json(v0)]
        sections += ["# Evidence from the current design's executions", evidence,
                     "# Pairwise comparison history", _history_text(history),
                     "# Available tools", ", ".join(self.tool_names) or "(stage default)",
                     "# Instructions",
                     f"Write v{current.version + 1}. Preserve the task's required deliverables and intent; fix "
                     "weaknesses the evidence and judge rationales reveal. " + OUTPUT_SCHEMA]
        return "\n\n".join(sections)

    def propose(self, *, task_texts: list[str], current: Workflow, best: Workflow | None, v0: Workflow,
                evidence: str, history: list[HistoryRow], new_version: int, seed: int | None = None
                ) -> tuple[Workflow | None, list[str]]:
        prompt = self.build_prompt(task_texts, current, best, v0, evidence, history)
        errors: list[str] = []
        for attempt in range(self.retries + 1):
            if errors:
                prompt += "\n\n# Your previous proposal violated guardrails; fix these:\n- " + "\n- ".join(errors)
            try:
                data, _ = self.provider.complete_json(OPTIMIZER_SYSTEM, prompt,
                                                      seed=None if seed is None else seed + attempt)
                d = data["design"]
                wf = Workflow(current.name, AgentSpec.from_dict(d["orchestrator"]),
                              [AgentSpec.from_dict(a) for a in d.get("agents") or []], str(d.get("protocol", "")),
                              version=new_version, parent=current.version,
                              notes=[str(x) for x in data.get("changes_from_previous") or []])
            except (ProviderError, KeyError, TypeError, AttributeError) as e:
                errors = [f"malformed proposal: {e}"]
                continue
            errors = self.guardrails.check(wf)
            if not errors:
                return wf, []
        return None, errors
