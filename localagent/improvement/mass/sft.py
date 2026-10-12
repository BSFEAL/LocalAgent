"""Teacher collection -> self-ranking -> selection -> provider-neutral SFT data.

Datasets are written in the widely supported chat-JSONL format
(``{"messages": [...], "tools": [...]}``, OpenAI-style tool calls). Assistant
turns carry ``"weight": 1`` and are the only training targets; system, user and
tool turns are context. Each example records its conversation ``role`` so a
trainer can reproduce the paper's sampling (orchestrator p=1/3).
"""
from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ...core.trajectory import Conversation
from ...core.workflow import ORCHESTRATOR_PREAMBLE, Workflow
from ..base import Execution, Task, TaskExecutor
from .judge import PairwiseJudge
from .ranking import bradley_terry, tournament


@dataclass
class RankedTask:
    task: Task
    executions: list[Execution]
    scores: dict[str, float] = field(default_factory=dict)  # trajectory id -> BT log-strength
    matches: list[tuple[str, str, str]] = field(default_factory=list)

    def ordered(self) -> list[Execution]:
        return sorted(self.executions, key=lambda e: -self.scores.get(e.trajectory.id, 0.0))


def collect(executor: TaskExecutor, workflow: Workflow, tasks: list[Task], candidates: int,
            seed: int = 0) -> dict[str, list[Execution]]:
    return {t.id: [executor.execute(workflow, t, seed=seed + 10_000 + i) for i in range(candidates)] for t in tasks}


def rank(judge: PairwiseJudge, task: Task, execs: list[Execution], opponents: int = 6,
         seed: int = 0) -> RankedTask:
    by_id = {e.trajectory.id: e for e in execs}
    matches = []
    for k, (a, b) in enumerate(tournament(list(by_id), opponents, seed)):
        v = judge.compare(task.prompt, by_id[a], by_id[b], seed=seed + k)
        matches.append((a, b, a if v.winner == "A" else b if v.winner == "B" else "tie"))
    return RankedTask(task, execs, bradley_terry(list(by_id), matches), matches)


def select(ranked: list[RankedTask], train_ranks: int, val_ranks: int = 1
           ) -> tuple[list[Execution], list[Execution]]:
    """Top ``train_ranks`` per task -> train; the next ``val_ranks`` -> validation.
    Test-split tasks are refused here so they can never leak into training."""
    train, val = [], []
    for r in ranked:
        if r.task.split != "train":
            raise ValueError(f"task {r.task.id} is in split {r.task.split!r}; only train tasks may enter SFT")
        ordered = r.ordered()
        train += ordered[:train_ranks]
        val += ordered[train_ranks: train_ranks + val_ranks]
    return train, val


def _wire(m_role: str, m: Any) -> dict[str, Any]:
    if m_role == "tool":
        return {"role": "tool", "tool_call_id": m.tool_call_id, "content": m.content}
    out: dict[str, Any] = {"role": m_role, "content": m.content}
    if m.tool_calls:
        out["tool_calls"] = [{"id": c.id, "type": "function",
                              "function": {"name": c.name, "arguments": json.dumps(c.arguments)}}
                             for c in m.tool_calls]
    if m_role == "assistant":
        if m.reasoning:
            out["reasoning_content"] = m.reasoning
        out["weight"] = 1
    return out


def render_conversation(conv: Conversation, task_prompt: str, strip_workflow: bool) -> dict[str, Any]:
    msgs = conv.messages
    rendered = []
    for i, m in enumerate(msgs):
        if strip_workflow and conv.role == "orchestrator" and m.role == "system" and i == 0:
            # Workflow removal: the student must internalise the team design, so the
            # orchestrator sees only the generic orchestration preamble, not the roster.
            rendered.append({"role": "system", "content": ORCHESTRATOR_PREAMBLE})
            continue
        if strip_workflow and conv.role == "orchestrator" and m.role == "user" and i == 1:
            rendered.append({"role": "user", "content": task_prompt})
            continue
        rendered.append(_wire(m.role, m))
    return {"messages": rendered, "tools": [{"type": "function", "function": t.__dict__} for t in conv.tools],
            "metadata": {"role": conv.role, "agent_id": conv.agent_id}}


def write_dataset(path: Path, execs: list[Execution], *, strip_workflow: bool = True) -> dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    n, roles, tokens = 0, {}, 0
    with path.open("w") as f:
        for e in execs:
            for conv in e.trajectory.conversations:
                if not any(m.role == "assistant" for m in conv.messages):
                    continue
                ex = render_conversation(conv, e.task.prompt, strip_workflow)
                ex["metadata"].update(task_id=e.task.id, trajectory_id=e.trajectory.id, workflow=e.workflow.tag)
                f.write(json.dumps(ex) + "\n")
                n += 1
                roles[conv.role] = roles.get(conv.role, 0) + 1
                tokens += conv.usage.output_tokens
    return {"path": str(path), "examples": n, "by_role": roles, "assistant_output_tokens": tokens}


def sample_mix(path: Path, n: int, orchestrator_p: float = 1 / 3, seed: int = 0) -> list[dict[str, Any]]:
    """Task-uniform sampling with replacement, orchestrator conversations at probability p."""
    rows = [json.loads(x) for x in path.read_text().splitlines() if x]
    by_task: dict[str, dict[str, list]] = {}
    for r in rows:
        kind = "orch" if r["metadata"]["role"] == "orchestrator" else "other"
        by_task.setdefault(r["metadata"]["task_id"], {"orch": [], "other": []})[kind].append(r)
    rng, tasks, out = random.Random(seed), sorted(by_task), []
    for _ in range(n):
        pools = by_task[rng.choice(tasks)]
        kind = "orch" if (rng.random() < orchestrator_p and pools["orch"]) or not pools["other"] else "other"
        out.append(rng.choice(pools[kind]))
    return out
