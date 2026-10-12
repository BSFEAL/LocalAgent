"""Perf Factory: detect latency regressions, dedupe alerts, propose fixes."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

from ..core.agent import Agent
from ..core.events import EventBus
from ..core.tools import Toolbox
from ..core.trajectory import Trajectory
from ..core.workflow import Workflow, system_prompt
from ..providers.base import extract_json
from ..providers.registry import ProviderRouter
from .outcome import Outcome


@dataclass
class Regression:
    metric: str
    value: float
    baseline: float
    ratio: float

    @property
    def fingerprint(self) -> str:
        return f"latency:{self.metric}"

    def describe(self) -> str:
        return f"{self.metric} is {self.value:.4g} vs baseline {self.baseline:.4g} (x{self.ratio:.2f})"


class PerfFactory:
    def __init__(self, router: ProviderRouter, state_path: Path, bus: EventBus, *,
                 threshold: float = 0.2, alpha: float = 0.2, cooldown_s: float = 3600,
                 latency_markers: tuple[str, ...] = ("latency", "p50", "p90", "p95", "p99", "_ms", "duration")):
        self.router, self.state_path, self.bus = router, Path(state_path), bus
        self.threshold, self.alpha, self.cooldown_s = threshold, alpha, cooldown_s
        self.latency_markers = latency_markers
        self.state = json.loads(self.state_path.read_text()) if self.state_path.exists() else {
            "baselines": {}, "alerts": {}}

    def _save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(self.state, indent=2))

    def is_latency(self, metric: str) -> bool:
        return any(m in metric.lower() for m in self.latency_markers)

    def detect(self, values: dict[str, float], now: float | None = None) -> list[Regression]:
        """Compare to EWMA baselines; only healthy samples update the baseline.
        Returns *new* regressions - duplicates within the cooldown are filtered."""
        now = now or time.time()
        found = []
        for metric, v in values.items():
            if not self.is_latency(metric):
                continue
            base = self.state["baselines"].get(metric)
            if base is None:
                self.state["baselines"][metric] = v
                continue
            if base > 0 and v > base * (1 + self.threshold):
                r = Regression(metric, v, base, v / base)
                alert = self.state["alerts"].get(r.fingerprint)
                if alert and now - alert["last_seen"] < self.cooldown_s:
                    alert["last_seen"], alert["count"] = now, alert["count"] + 1
                    self.bus.emit("perf.duplicate", metric=metric, count=alert["count"])
                    continue
                self.state["alerts"][r.fingerprint] = {"first_seen": now, "last_seen": now, "count": 1,
                                                       "detail": r.describe()}
                found.append(r)
                self.bus.emit("perf.regression", metric=metric, detail=r.describe())
            else:
                self.state["baselines"][metric] = (1 - self.alpha) * base + self.alpha * v
                self.state["alerts"].pop(f"latency:{metric}", None)
        self._save()
        return found

    def propose_fix(self, workflow: Workflow, regression: Regression, recent_deploys: list[dict],
                    tools: Toolbox, traj: Trajectory | None = None) -> Outcome:
        spec = workflow.orchestrator
        agent = Agent(spec.id, system_prompt(spec),
                      self.router.for_role(spec.route or "perf"), tools.subset(spec.tools), 20,
                      "single", trajectory=traj)
        prompt = (f"Latency regression: {regression.describe()}\n\nRecent deploys (newest last):\n"
                  + json.dumps(recent_deploys[-5:], indent=1)[:8000])
        try:
            d = extract_json(agent.run(prompt).output)
            out = Outcome(str(d["title"]), str(d.get("description", "")), list(d.get("acceptance_criteria") or []),
                          origin="perf_factory", metadata={"regression": regression.describe()})
        except (ValueError, KeyError, TypeError, AttributeError):
            out = Outcome(f"Fix latency regression in {regression.metric}", regression.describe(),
                          [f"{regression.metric} back to <= {regression.baseline * (1 + self.threshold):.4g}"],
                          origin="perf_factory", metadata={"regression": regression.describe()})
        self.bus.emit("perf.proposal", title=out.title)
        return out
