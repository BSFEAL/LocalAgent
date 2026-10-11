"""Pairwise self-evaluation for non-verifiable deliverables.

MASS uses the *current* model as its own judge. To cut position bias, each
comparison is asked in both orders; disagreement is a tie. When verifiable
checks exist (e.g. CI), they decide first and the judge only breaks ties.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ...providers.base import Provider, ProviderError
from ..base import Execution

JUDGE_SYSTEM = """You are a strict expert evaluator comparing two deliverables produced for the same task.
Judge only the deliverables' quality against the task: correctness, completeness of the required
deliverables, rigor, reproducibility and clarity. Ignore length and style unless they hurt usefulness.
Return JSON: {"winner": "A" | "B" | "tie", "rationale": "<specific, evidence-based reasons>"}"""


@dataclass
class Verdict:
    winner: str  # "A" | "B" | "tie"
    rationale: str = ""
    votes: list[str] = field(default_factory=list)


def _clip(text: str, n: int) -> str:
    return text if len(text) <= n else text[: n // 2] + "\n...[truncated]...\n" + text[-n // 2:]


class PairwiseJudge:
    def __init__(self, provider: Provider, *, rubric: str = "", both_orders: bool = True,
                 prefer_verifiable: bool = True, max_chars: int = 60_000):
        self.provider, self.rubric = provider, rubric
        self.both_orders, self.prefer_verifiable, self.max_chars = both_orders, prefer_verifiable, max_chars

    def _ask(self, task: str, first: str, second: str, seed: int | None) -> tuple[str, str]:
        user = (f"# Task\n{task}\n\n" + (f"# Rubric\n{self.rubric}\n\n" if self.rubric else "")
                + f"# Deliverable A\n{_clip(first, self.max_chars)}\n\n# Deliverable B\n{_clip(second, self.max_chars)}")
        try:
            data, _ = self.provider.complete_json(JUDGE_SYSTEM, user, seed=seed)
            w = str(data.get("winner", "tie")).strip().upper()
            return (w if w in ("A", "B") else "tie"), str(data.get("rationale", ""))
        except (ProviderError, AttributeError):
            return "tie", "judge failed to return a verdict"

    def compare(self, task: str, a: Execution, b: Execution, seed: int | None = None) -> Verdict:
        if self.prefer_verifiable and a.checks and b.checks:
            pa, pb = all(a.checks.values()), all(b.checks.values())
            if pa != pb:
                return Verdict("A" if pa else "B", f"verifiable checks: A={a.checks} B={b.checks}", ["checks"])
        w1, r1 = self._ask(task, a.deliverable, b.deliverable, seed)
        if not self.both_orders:
            return Verdict(w1, r1, [w1])
        w2_raw, r2 = self._ask(task, b.deliverable, a.deliverable, None if seed is None else seed + 1)
        w2 = {"A": "B", "B": "A"}.get(w2_raw, "tie")
        winner = w1 if w1 == w2 else "tie"
        return Verdict(winner, r1 if winner != "tie" else f"order-dependent: {r1} || {r2}", [w1, w2])
