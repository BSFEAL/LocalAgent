"""Sevbot: investigates incidents, proposes mitigations, answers questions."""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from typing import Any

from ..core.agent import Agent
from ..core.events import EventBus
from ..core.tools import Tool, Toolbox, Workspace, schema
from ..core.trajectory import Trajectory
from ..core.workflow import Workflow, WorkflowRunner, system_prompt
from ..providers.base import extract_json
from .deploy import DeployLog, FlagStore
from .observability import MetricsSource


@dataclass
class IncidentReport:
    alert: str
    summary: str = ""
    suspected_cause: str = ""
    confidence: float = 0.0
    mitigations: list[str] = field(default_factory=list)
    rollback: bool = False
    flag: str | None = None
    mitigated: bool = False
    raw: Any = None


class Sevbot:
    def __init__(self, runner: WorkflowRunner, metrics: MetricsSource, deploys: DeployLog,
                 flags: FlagStore, bus: EventBus, *, auto_mitigate: bool = False):
        self.runner, self.metrics, self.deploys, self.flags, self.bus = runner, metrics, deploys, flags, bus
        self.auto_mitigate = auto_mitigate

    def toolbox(self, ws: Workspace, extra: Toolbox | None = None) -> Toolbox:
        def git_log(n: int = 20) -> str:
            return subprocess.run(["git", "log", "--stat", "-n", str(n)], cwd=ws.root,
                                  capture_output=True, text=True, timeout=30).stdout or "no git history"
        tb = ws.toolbox(read_only=True).add(
            Tool("metrics_now", "Current production metric values.", schema(),
                 lambda: json.dumps(self.metrics.read())),
            Tool("recent_deploys", "Recent deploys with flags, files, status.", schema([], n="integer"),
                 lambda n=10: json.dumps(self.deploys.recent(n), indent=1)),
            Tool("flag_state", "Current rollout percentage of a feature flag.", schema(["flag"], flag="string"),
                 lambda flag: str(self.flags.get(flag))),
            Tool("git_log", "Recent commits with changed files.", schema([], n="integer"), git_log),
        )
        if extra:
            tb.add(*extra.tools.values())
        return tb

    def investigate(self, workflow: Workflow, alert: str, ws: Workspace, extra: Toolbox | None = None,
                    traj: Trajectory | None = None) -> IncidentReport:
        self.bus.emit("sevbot.incident", alert=alert)
        out = self.runner.run(workflow, f"INCIDENT: {alert}\nInvestigate and return the JSON report.",
                              self.toolbox(ws, extra), stage="sevbot", trajectory=traj)
        rep = IncidentReport(alert, raw=out)
        try:
            d = extract_json(out)
            rep.summary = str(d.get("summary", ""))
            rep.suspected_cause = str(d.get("suspected_cause", ""))
            rep.confidence = float(d.get("confidence") or 0)
            rep.mitigations = [str(m) for m in d.get("mitigations") or []]
            rep.rollback = bool(d.get("rollback"))
            rep.flag = d.get("flag") or None
        except (ValueError, AttributeError, TypeError):
            rep.summary = out[:2000]
        if self.auto_mitigate and rep.rollback and rep.flag:
            self.flags.set(rep.flag, 0)
            rep.mitigated = True
            self.bus.emit("sevbot.mitigated", flag=rep.flag)
        self.bus.emit("sevbot.report", summary=rep.summary[:300], rollback=rep.rollback, flag=rep.flag)
        return rep

    def ask(self, workflow: Workflow, question: str, ws: Workspace) -> str:
        spec = workflow.orchestrator
        agent = Agent("sevbot_qa", system_prompt(spec) + "\n\nNow answer the responder's question with evidence.",
                      self.runner.router.for_role("sevbot"), self.toolbox(ws), 20)
        return agent.run(question).output
