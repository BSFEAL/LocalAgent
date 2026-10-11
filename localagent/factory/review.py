"""Agentic code review: parallel specialists + risk classification."""
from __future__ import annotations

import fnmatch
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from ..core.agent import Agent
from ..core.tools import Toolbox
from ..core.trajectory import Trajectory
from ..core.workflow import AgentSpec, Workflow, system_prompt
from ..providers.base import extract_json
from ..providers.registry import ProviderRouter
from .changes import ChangeSet

RISK_ORDER = {"low": 0, "medium": 1, "high": 2}


@dataclass
class Finding:
    severity: str
    message: str
    file: str = ""
    line: int = 0
    reviewer: str = ""

    @property
    def blocking(self) -> bool:
        return self.severity in ("blocker", "major")


@dataclass
class ReviewResult:
    risk: str
    approve: bool
    findings: list[Finding] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    reports: dict[str, Any] = field(default_factory=dict)

    @property
    def blocking(self) -> list[Finding]:
        return [f for f in self.findings if f.blocking]

    def feedback(self) -> str:
        return "\n".join(f"[{f.reviewer}/{f.severity}] {f.file}:{f.line} {f.message}" for f in self.blocking)


@dataclass
class RiskPolicy:
    """Deterministic floor under the model's risk judgement."""

    high_risk_paths: list[str] = field(default_factory=lambda: [
        "*migration*", "*migrations/*", "*.sql", "*auth*", "*secret*", "*security*", "*iam*",
        "*.tf", "*terraform/*", "*k8s/*", "*helm/*", "Dockerfile", "*.github/workflows/*", "*deploy*"])
    medium_risk_paths: list[str] = field(default_factory=lambda: [
        "*config*", "*.toml", "*.yaml", "*.yml", "*requirements*.txt", "package.json", "*lock*"])
    max_low_risk_lines: int = 200

    def floor(self, changes: ChangeSet) -> tuple[str, list[str]]:
        level, reasons = "low", []
        for f in changes.files:
            if any(fnmatch.fnmatch(f, p) for p in self.high_risk_paths):
                return "high", [f"touches sensitive path {f}"]
            if any(fnmatch.fnmatch(f, p) for p in self.medium_risk_paths):
                level, reasons = "medium", reasons + [f"touches config/dependency file {f}"]
        size = changes.added_lines + changes.removed_lines
        if size > self.max_low_risk_lines:
            level = max(level, "medium", key=RISK_ORDER.get)
            reasons.append(f"large diff ({size} lines)")
        return level, reasons


def _parse(raw: str, reviewer: str) -> dict[str, Any]:
    try:
        data = extract_json(raw)
        if isinstance(data, dict):
            return data
    except ValueError:
        pass
    # An unparseable review must never silently count as approval.
    return {"risk": "high", "approve": False,
            "findings": [{"severity": "major", "message": f"{reviewer} returned an unparseable review"}]}


class ReviewPanel:
    def __init__(self, router: ProviderRouter, policy: RiskPolicy | None = None, max_steps: int = 15):
        self.router = router
        self.policy = policy or RiskPolicy()
        self.max_steps = max_steps

    def _ask(self, spec: AgentSpec, prompt: str, tools: Toolbox, traj: Trajectory | None) -> dict[str, Any]:
        agent = Agent(spec.id, system_prompt(spec), self.router.for_role(spec.route or f"review.{spec.id}"),
                      tools.subset(spec.tools), self.max_steps, "worker", trajectory=traj)
        return _parse(agent.run(prompt).output, spec.id)

    def review(self, workflow: Workflow, outcome_prompt: str, changes: ChangeSet, tools: Toolbox,
               trajectory: Trajectory | None = None) -> ReviewResult:
        base = f"{outcome_prompt}\n\n## Diff ({changes.summary()})\n```diff\n{changes.diff[:60_000]}\n```"
        specialists = workflow.agents
        with ThreadPoolExecutor(max(1, len(specialists))) as pool:
            reports = dict(zip([s.id for s in specialists],
                               pool.map(lambda s: self._ask(s, base, tools, trajectory), specialists)))
        agg_prompt = base + "\n\n## Specialist reviews\n" + "\n".join(
            f"### {k}\n{v}" for k, v in reports.items())
        verdict = self._ask(workflow.orchestrator, agg_prompt, tools, trajectory)

        findings = []
        for who, rep in list(reports.items()) + [(workflow.orchestrator.id, verdict)]:
            for f in rep.get("findings") or []:
                if isinstance(f, dict):
                    findings.append(Finding(str(f.get("severity", "minor")).lower(), str(f.get("message", "")),
                                            str(f.get("file", "")), int(f.get("line") or 0), who))
        model_risk = str(verdict.get("risk", "high")).lower()
        if model_risk not in RISK_ORDER:
            model_risk = "high"
        specialist_max = max([str(r.get("risk", "low")).lower() for r in reports.values()] or ["low"],
                             key=lambda r: RISK_ORDER.get(r, 2))
        floor, reasons = self.policy.floor(changes)
        risk = max([model_risk, floor, specialist_max], key=lambda r: RISK_ORDER.get(r, 2))
        reasons = [f"classifier={model_risk}", f"max specialist={specialist_max}", f"policy floor={floor}"] + reasons
        approve = bool(verdict.get("approve", False)) and not any(f.blocking for f in findings)
        return ReviewResult(risk, approve, findings, reasons, {**reports, "_classifier": verdict})
