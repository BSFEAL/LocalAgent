"""The agentic software factory, end to end.

    builder ─outcome─▶ coder ──▶ CI ──▶ review(parallel specialists + risk)
                         ▲        │fail     │blocking findings
                         └────────┴─────────┘
    review ─low risk─▶ agentic deploy ─▶ production ─monitor─▶ Perf Factory ─fix outcome─▶ coder
           └─else─▶ human review ─┘                  └─outage─▶ Sevbot (investigate, mitigate, Q&A)

    improvement slot (e.g. MASS): evolves stage workflows / the model offline
    from the trajectories every stage records.
"""
from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core.context import ContextHub, DirectorySource, GitSource
from ..core.events import EventBus
from ..core.tools import Toolbox, Workspace
from ..core.trajectory import Trajectory, TrajectoryStore
from ..core.workflow import Workflow, WorkflowRunner, WorkflowStore
from ..improvement.base import (ImprovementContext, ImprovementReport, ImprovementStrategy, NoopStrategy, Task,
                                TaskBank, TaskExecutor, WorkspaceExecutor)
from ..providers.registry import ProviderRouter
from .changes import ChangeSet, ChangeTracker
from .ci import CommandCI, CIResult, CIRunner
from .defaults import DEFAULT_WORKFLOWS
from .deploy import AgenticDeploy, DeployLog, DeployRecord, Deployer, FlagStore, LocalFlagStore, NoopDeployer
from .gates import Approval, Approver, CLIApprover
from .observability import MetricsSource, Signal, StaticMetrics, check
from .outcome import Outcome
from .perf import PerfFactory
from .review import ReviewPanel, ReviewResult, RiskPolicy
from .sevbot import IncidentReport, Sevbot

CODER_PREAMBLE = """You are an autonomous coding agent working directly in a repository.
Docs live in the code: read READMEs and docstrings before changing behaviour, and update them with it."""


@dataclass
class BuildReport:
    outcome: Outcome
    status: str = "pending"  # deployed | rolled_back | rejected | failed | no_changes | built
    iterations: int = 0
    changes: ChangeSet | None = None
    ci: CIResult | None = None
    review: ReviewResult | None = None
    approval: Approval | None = None
    deploy: DeployRecord | None = None
    trajectories: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return {"outcome": self.outcome.title, "status": self.status, "iterations": self.iterations,
                "changes": self.changes.summary() if self.changes else None,
                "ci_passed": self.ci.passed if self.ci else None,
                "risk": self.review.risk if self.review else None,
                "approved_by": self.approval.reviewer if self.approval else None,
                "deploy": self.deploy.status if self.deploy else None}


@dataclass
class MonitorReport:
    values: dict[str, float]
    breaches: list[str] = field(default_factory=list)
    proposals: list[Outcome] = field(default_factory=list)
    incidents: list[IncidentReport] = field(default_factory=list)


class Factory:
    def __init__(self, repo: str | Path, router: ProviderRouter, *,
                 state_dir: str | Path | None = None, ci: CIRunner | None = None,
                 approver: Approver | None = None, deployer: Deployer | None = None,
                 flags: FlagStore | None = None, metrics: MetricsSource | None = None,
                 context: ContextHub | None = None, improvement: ImprovementStrategy | None = None,
                 risk_policy: RiskPolicy | None = None, guardrails: list[Signal] | None = None,
                 max_iterations: int = 5, max_steps: int = 40, rollout_stages: tuple[float, ...] = (1, 5, 25, 50, 100),
                 soak_seconds: float = 0.0, auto_mitigate: bool = False, deploy_enabled: bool = True,
                 executors: dict[str, TaskExecutor] | None = None, echo: bool = False,
                 workflows: dict[str, Workflow] | None = None):
        self.repo = Path(repo).resolve()
        self.state = Path(state_dir) if state_dir else self.repo / ".localagent"
        self.router = router
        self.bus = EventBus(self.state / "events.jsonl", echo=echo)
        self.workflows = WorkflowStore(self.state / "workflows")
        self.trajectories = TrajectoryStore(self.state / "trajectories")
        self.tasks = TaskBank(self.state / "tasks.jsonl")
        self.defaults = {k: f() for k, f in DEFAULT_WORKFLOWS.items()} | (workflows or {})
        self.ci = ci or CommandCI()
        self.approver = approver or CLIApprover()
        self.flags = flags or LocalFlagStore(self.state / "flags.json")
        self.metrics = metrics or StaticMetrics({})
        self.context = context or ContextHub([DirectorySource(self.repo, "repo-docs"), GitSource(self.repo)])
        self.improvement = improvement or NoopStrategy()
        self.max_iterations = max_iterations
        self.guardrails = guardrails or []
        self.deploy_enabled = deploy_enabled
        self.runner = WorkflowRunner(router, max_steps=max_steps)
        self.review_panel = ReviewPanel(router, risk_policy)
        self.deploys = DeployLog(self.state / "deploys.jsonl")
        self.deployer = AgenticDeploy(router, deployer or NoopDeployer(), self.flags, self.metrics, self.deploys,
                                      self.bus, self.state / "dashboards", stages=rollout_stages,
                                      soak_seconds=soak_seconds, guardrails=self.guardrails)
        self.perf = PerfFactory(router, self.state / "perf_state.json", self.bus)
        self.sevbot = Sevbot(self.runner, self.metrics, self.deploys, self.flags, self.bus,
                             auto_mitigate=auto_mitigate)
        self.executors = executors or {}
        self._load_generation()

    # --- helpers --------------------------------------------------------------
    def _load_generation(self) -> None:
        p = self.state / "improvement" / "generations.json"
        if p.exists():
            rows = json.loads(p.read_text())
            if rows:
                self.router.set("default", rows[-1]["model"])

    def workflow(self, stage: str) -> Workflow:
        return self.workflows.current_or(stage, self.defaults[stage])

    def _record(self, traj: Trajectory) -> None:
        self.trajectories.save(traj)
        self.improvement.observe(traj)

    def _tools(self, ws: Workspace, read_only: bool = False) -> Toolbox:
        return ws.toolbox(read_only=read_only).add(self.context.tool())

    def _rev(self) -> str | None:
        try:
            r = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.repo, capture_output=True, text=True, timeout=10)
            return r.stdout.strip() or None
        except (OSError, subprocess.TimeoutExpired):
            return None

    # --- build loop: coder -> CI -> review (until green) ---------------------------
    def build(self, outcome: Outcome, *, deploy: bool | None = None) -> BuildReport:
        rep = BuildReport(outcome)
        ws = Workspace(self.repo)
        tracker = ChangeTracker(ws)
        tracker.start()
        self.tasks.add(Task(outcome.id, "coder", outcome.to_prompt(), repo=str(self.repo), rev=self._rev(),
                            metadata={"origin": outcome.origin}))
        self.bus.emit("outcome.received", id=outcome.id, title=outcome.title, origin=outcome.origin)
        context = self.context.gather(f"{outcome.title} {outcome.description}")
        feedback = ""
        coder_wf = self.workflow("coder")
        for it in range(1, self.max_iterations + 1):
            rep.iterations = it
            prompt = outcome.to_prompt()
            if context:
                prompt += f"\n\n## Context (from project knowledge)\n{context}"
            if feedback:
                prompt += f"\n\n## Fix these issues from the previous attempt\n{feedback}"
            traj = Trajectory(outcome.id, "coder", coder_wf.tag, outcome.to_prompt(), metadata={"iteration": it})
            out = self.runner.run(coder_wf, prompt, self._tools(ws), stage="coder", trajectory=traj,
                                  preamble=CODER_PREAMBLE)
            traj.output = out
            rep.changes = tracker.collect()
            self.bus.emit("coder.done", iteration=it, changes=rep.changes.summary())
            if rep.changes.empty:
                feedback = "No files were changed. Implement the outcome by editing files."
                traj.metadata["result"] = "no_changes"
                self._record(traj)
                rep.trajectories.append(traj.id)
                continue
            rep.ci = self.ci.run(ws)
            self.bus.emit("ci.passed" if rep.ci.passed else "ci.failed", iteration=it,
                          steps={s.name: s.exit_code for s in rep.ci.steps})
            traj.metadata["ci_passed"] = rep.ci.passed
            if not rep.ci.passed:
                feedback = rep.ci.feedback()
                self._record(traj)
                rep.trajectories.append(traj.id)
                continue
            rtraj = Trajectory(outcome.id, "review", self.workflow("review").tag, outcome.to_prompt())
            rep.review = self.review_panel.review(self.workflow("review"), outcome.to_prompt(), rep.changes,
                                                  self._tools(ws, read_only=True), rtraj)
            self._record(rtraj)
            self.bus.emit("review.done", risk=rep.review.risk, approve=rep.review.approve,
                          blocking=len(rep.review.blocking), reasons=rep.review.reasons)
            traj.metadata.update(review_risk=rep.review.risk, review_blocking=len(rep.review.blocking))
            self._record(traj)
            rep.trajectories.append(traj.id)
            if rep.review.blocking:
                feedback = "Code review blocking findings:\n" + rep.review.feedback()
                continue
            break
        else:
            rep.status = "no_changes" if rep.changes is None or rep.changes.empty else "failed"
            self.bus.emit("build.failed", outcome=outcome.id, status=rep.status)
            return rep
        return self._gate_and_deploy(rep, ws, deploy)

    def _gate_and_deploy(self, rep: BuildReport, ws: Workspace, deploy: bool | None) -> BuildReport:
        assert rep.review and rep.changes
        if rep.review.risk == "low" and rep.review.approve:
            rep.approval = Approval(True, "agentic-review", "low risk")
        else:
            self.bus.emit("review.human_requested", risk=rep.review.risk)
            rep.approval = self.approver.request(rep.outcome, rep.changes, rep.review)
        self.bus.emit("gate.decision", approved=rep.approval.approved, by=rep.approval.reviewer)
        if not rep.approval.approved:
            rep.status = "rejected"
            return rep
        if not (self.deploy_enabled if deploy is None else deploy):
            rep.status = "built"
            return rep
        dtraj = Trajectory(rep.outcome.id, "deploy", self.workflow("deploy").tag, rep.outcome.to_prompt())
        rep.deploy = self.deployer.babysit(self.workflow("deploy"), rep.outcome, rep.changes, ws,
                                           self._tools(ws, read_only=True), dtraj)
        self._record(dtraj)
        rep.status = {"rolled_out": "deployed"}.get(rep.deploy.status, rep.deploy.status)
        return rep

    # --- production: monitor -> perf factory / sevbot ---------------------------------
    def active_signals(self) -> list[Signal]:
        sig = {s.metric: s for s in self.guardrails}
        for d in self.deploys.recent(5):
            if d.get("status") == "rolled_out":
                for s in d.get("signals", []):
                    sig.setdefault(s["metric"], Signal.from_dict(s))
        return list(sig.values())

    def monitor_once(self) -> MonitorReport:
        values = self.metrics.read()
        rep = MonitorReport(values)
        ws = Workspace(self.repo)
        for reg in self.perf.detect(values):
            traj = Trajectory(reg.fingerprint, "perf", self.workflow("perf").tag, reg.describe())
            rep.proposals.append(self.perf.propose_fix(self.workflow("perf"), reg, self.deploys.recent(10),
                                                       self._tools(ws, read_only=True), traj))
            self._record(traj)
        breaches = [(s, m) for s, m in check(self.active_signals(), values) if s.kind != "latency"]
        rep.breaches = [m for _, m in breaches]
        if breaches:
            alert = "; ".join(rep.breaches)
            traj = Trajectory(f"incident-{int(time.time())}", "sevbot", self.workflow("sevbot").tag, alert)
            rep.incidents.append(self.sevbot.investigate(self.workflow("sevbot"), alert, ws,
                                                         Toolbox().add(self.context.tool()), traj))
            self._record(traj)
        return rep

    def ask_sevbot(self, question: str) -> str:
        return self.sevbot.ask(self.workflow("sevbot"), question, Workspace(self.repo))

    def run(self, outcome: Outcome, *, follow_up_rounds: int = 1, monitor_ticks: int = 1) -> list[BuildReport]:
        """Build/deploy, watch production, and feed Perf Factory proposals back into the coder."""
        reports, queue = [], [outcome]
        for _ in range(follow_up_rounds + 1):
            if not queue:
                break
            current = queue.pop(0)
            rep = self.build(current)
            reports.append(rep)
            if rep.status != "deployed":
                continue
            for _ in range(monitor_ticks):
                queue += self.monitor_once().proposals
        return reports

    # --- the paper slot -------------------------------------------------------------
    def improvement_context(self) -> ImprovementContext:
        executors: dict[str, TaskExecutor] = {
            "*": WorkspaceExecutor(self.runner, self.state / "improvement" / "workspaces", context=self.context),
            "coder": WorkspaceExecutor(self.runner, self.state / "improvement" / "workspaces", ci=self.ci,
                                       context=self.context, preamble=CODER_PREAMBLE),
            **self.executors,
        }
        return ImprovementContext(self.router, self.workflows, self.tasks, self.trajectories, executors,
                                  self.defaults, self.state / "improvement", self.bus)

    def improve(self, stages: list[str] | None = None) -> ImprovementReport:
        stages = stages or ["coder"]
        self.bus.emit("improve.start", strategy=self.improvement.name, stages=stages)
        report = self.improvement.run_cycle(self.improvement_context(), stages)
        self.bus.emit("improve.done", strategy=self.improvement.name, new_model=report.new_model)
        return report
