"""Agentic deploy: ship behind a feature flag and babysit the rollout."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from ..core.agent import Agent
from ..core.events import EventBus
from ..core.tools import Toolbox, Workspace
from ..core.trajectory import Trajectory
from ..core.workflow import Workflow, system_prompt
from ..providers.base import extract_json
from ..providers.registry import ProviderRouter
from .changes import ChangeSet
from .observability import MetricsSource, Signal, check, signals_to_dicts, write_dashboard
from .outcome import Outcome


@dataclass
class DeployRecord:
    outcome_id: str
    title: str
    flag: str
    files: list[str]
    ts: float = field(default_factory=time.time)
    status: str = "started"  # started | rolled_out | rolled_back | failed
    detail: str = ""
    signals: list[dict[str, Any]] = field(default_factory=list)
    dashboard: str = ""


class Deployer(Protocol):
    def deploy(self, ws: Workspace, record: DeployRecord) -> tuple[bool, str]: ...
    def rollback(self, ws: Workspace, record: DeployRecord) -> tuple[bool, str]: ...


class NoopDeployer:
    def deploy(self, ws, record):
        return True, "noop deploy"

    def rollback(self, ws, record):
        return True, "noop rollback"


class CommandDeployer:
    """Shell commands; ``{flag}`` and ``{outcome_id}`` are substituted."""

    def __init__(self, deploy_command: str, rollback_command: str = ""):
        self.deploy_command, self.rollback_command = deploy_command, rollback_command

    def _run(self, ws: Workspace, cmd: str, record: DeployRecord) -> tuple[bool, str]:
        if not cmd:
            return True, "no command configured"
        code, out = ws.run(cmd.format(flag=record.flag, outcome_id=record.outcome_id))
        return code == 0, out[-4000:]

    def deploy(self, ws, record):
        return self._run(ws, self.deploy_command, record)

    def rollback(self, ws, record):
        return self._run(ws, self.rollback_command, record)


class FlagStore(Protocol):
    def set(self, flag: str, percent: float) -> None: ...
    def get(self, flag: str) -> float: ...


class LocalFlagStore:
    """JSON-file flags. Swap for LaunchDarkly/Unleash/Statsig/etc. via the protocol."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def _all(self) -> dict[str, float]:
        return json.loads(self.path.read_text()) if self.path.exists() else {}

    def set(self, flag, percent):
        data = self._all()
        data[flag] = percent
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2))

    def get(self, flag):
        return float(self._all().get(flag, 0.0))


class DeployLog:
    def __init__(self, path: Path):
        self.path = Path(path)

    def append(self, rec: DeployRecord) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as f:
            f.write(json.dumps(rec.__dict__) + "\n")

    def recent(self, n: int = 10) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        return [json.loads(x) for x in self.path.read_text().splitlines()[-n:] if x]


class AgenticDeploy:
    def __init__(self, router: ProviderRouter, deployer: Deployer, flags: FlagStore, metrics: MetricsSource,
                 log: DeployLog, bus: EventBus, dashboards_dir: Path, *,
                 stages: tuple[float, ...] = (1, 5, 25, 50, 100), soak_seconds: float = 0.0,
                 checks_per_stage: int = 1, guardrails: list[Signal] | None = None):
        self.router, self.deployer, self.flags, self.metrics = router, deployer, flags, metrics
        self.log, self.bus, self.dashboards_dir = log, bus, dashboards_dir
        self.stages, self.soak_seconds, self.checks_per_stage = stages, soak_seconds, checks_per_stage
        self.guardrails = guardrails or []

    def plan(self, workflow: Workflow, outcome: Outcome, changes: ChangeSet, tools: Toolbox,
             traj: Trajectory | None) -> tuple[str, list[Signal]]:
        spec = workflow.orchestrator
        agent = Agent(spec.id, system_prompt(spec),
                      self.router.for_role(spec.route or "deploy"), tools.subset(spec.tools), 10,
                      "single", trajectory=traj)
        prompt = (f"{outcome.to_prompt()}\n\nChanged files: {changes.files}\n```diff\n{changes.diff[:20000]}\n```\n"
                  f"Known metrics now: {sorted(self.metrics.read())}")
        flag, signals = f"la-{outcome.id}", []
        try:
            data = extract_json(agent.run(prompt).output)
            flag = str(data.get("flag") or flag)
            signals = [Signal.from_dict(s) for s in data.get("signals") or [] if isinstance(s, dict) and s.get("metric")]
        except (ValueError, AttributeError, KeyError, TypeError):
            self.bus.emit("deploy.plan_fallback", outcome=outcome.id)
        names = {s.metric for s in signals}
        return flag, signals + [g for g in self.guardrails if g.metric not in names]

    def babysit(self, workflow: Workflow, outcome: Outcome, changes: ChangeSet, ws: Workspace,
                tools: Toolbox, traj: Trajectory | None = None) -> DeployRecord:
        flag, signals = self.plan(workflow, outcome, changes, tools, traj)
        rec = DeployRecord(outcome.id, outcome.title, flag, changes.files, signals=signals_to_dicts(signals))
        rec.dashboard = str(write_dashboard(self.dashboards_dir / f"{flag}.json", outcome.title, signals, flag))
        self.bus.emit("deploy.dashboard", flag=flag, path=rec.dashboard, signals=len(signals))
        self.flags.set(flag, 0)
        ok, out = self.deployer.deploy(ws, rec)
        if not ok:
            rec.status, rec.detail = "failed", out
            self.bus.emit("deploy.failed", flag=flag, detail=out[-500:])
            self.log.append(rec)
            return rec
        for pct in self.stages:
            self.flags.set(flag, pct)
            self.bus.emit("deploy.stage", flag=flag, percent=pct)
            for _ in range(self.checks_per_stage):
                if self.soak_seconds:
                    time.sleep(self.soak_seconds)
                breaches = check(signals, self.metrics.read())
                if breaches:
                    self.flags.set(flag, 0)
                    _, rb = self.deployer.rollback(ws, rec)
                    rec.status = "rolled_back"
                    rec.detail = f"at {pct}%: " + "; ".join(m for _, m in breaches) + f" | {rb[-300:]}"
                    self.bus.emit("deploy.rollback", flag=flag, percent=pct, breaches=[m for _, m in breaches])
                    self.log.append(rec)
                    return rec
        rec.status = "rolled_out"
        self.bus.emit("deploy.done", flag=flag)
        self.log.append(rec)
        return rec
