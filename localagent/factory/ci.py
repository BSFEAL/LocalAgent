"""CI: build + test + perf harness. Pluggable via the ``CIRunner`` protocol."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Protocol

from ..core.tools import Workspace


@dataclass
class CIStep:
    name: str
    command: str
    exit_code: int
    output: str
    seconds: float

    @property
    def passed(self) -> bool:
        return self.exit_code == 0


@dataclass
class CIResult:
    steps: list[CIStep] = field(default_factory=list)
    perf: dict[str, float] = field(default_factory=dict)
    perf_regressions: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(s.passed for s in self.steps) and not self.perf_regressions

    def feedback(self, tail: int = 4000) -> str:
        lines = []
        for s in self.steps:
            if not s.passed:
                lines.append(f"CI step '{s.name}' failed (exit {s.exit_code}): `{s.command}`\n{s.output[-tail:]}")
        lines += [f"Perf regression: {r}" for r in self.perf_regressions]
        return "\n\n".join(lines)


class CIRunner(Protocol):
    def run(self, ws: Workspace) -> CIResult: ...


class CommandCI:
    """Runs named shell steps in order. An optional perf harness prints a JSON
    object of metrics (e.g. ``{"p95_ms": 41.2}``) checked against budgets."""

    def __init__(self, steps: dict[str, str] | None = None, perf_command: str | None = None,
                 perf_budgets: dict[str, float] | None = None, timeout: int = 1800, fail_fast: bool = True):
        self.steps = steps or {}
        self.perf_command = perf_command
        self.perf_budgets = perf_budgets or {}
        self.timeout = timeout
        self.fail_fast = fail_fast

    def run(self, ws: Workspace) -> CIResult:
        res = CIResult()
        for name, cmd in self.steps.items():
            t0 = time.time()
            code, out = ws.run(cmd, timeout=self.timeout)
            res.steps.append(CIStep(name, cmd, code, out, time.time() - t0))
            if code != 0 and self.fail_fast:
                return res
        if self.perf_command:
            t0 = time.time()
            code, out = ws.run(self.perf_command, timeout=self.timeout)
            res.steps.append(CIStep("perf", self.perf_command, code, out[-4000:], time.time() - t0))
            if code == 0:
                try:
                    res.perf = {k: float(v) for k, v in json.loads(out.strip().splitlines()[-1]).items()}
                except (ValueError, IndexError, AttributeError):
                    res.perf_regressions.append("perf harness did not print a JSON metrics object")
                for metric, budget in self.perf_budgets.items():
                    if metric in res.perf and res.perf[metric] > budget:
                        res.perf_regressions.append(f"{metric}={res.perf[metric]:.3g} exceeds budget {budget:.3g}")
        return res
