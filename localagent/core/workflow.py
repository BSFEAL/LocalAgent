"""Multi-agent workflows as data, and the runner that executes them.

A :class:`Workflow` is the "multi-agent design" of a stage: an orchestrator,
a roster of specialist agents, their output contracts, and the routing
protocol. Because it is plain data (JSON on disk, versioned), self-improvement
strategies can propose, evaluate and retain new versions without code
changes - exactly the object MASS's workflow search optimises.
"""
from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ..providers.registry import ProviderRouter
from .agent import Agent
from .tools import Tool, Toolbox, schema
from .trajectory import Trajectory


@dataclass
class AgentSpec:
    id: str
    role: str
    instruction: str
    output_contract: str = ""  # what this agent must return to the orchestrator
    tools: list[str] | None = None  # None = every tool the stage offers
    route: str | None = None  # provider-router role override

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "AgentSpec":
        return cls(id=d["id"], role=d.get("role", ""), instruction=d.get("instruction", ""),
                   output_contract=d.get("output_contract", ""), tools=d.get("tools"),
                   route=d.get("route"))


@dataclass
class Workflow:
    name: str
    orchestrator: AgentSpec
    agents: list[AgentSpec] = field(default_factory=list)
    protocol: str = ""  # communication, recall and termination rules
    version: int = 0
    parent: int | None = None
    notes: list[str] = field(default_factory=list)  # changes_from_previous, rationale

    @classmethod
    def single(cls, name: str, instruction: str, tools: list[str] | None = None) -> "Workflow":
        """A one-agent workflow (the 'bare' reference MASS compares against)."""
        return cls(name, AgentSpec("agent", "solo agent", instruction, tools=tools))

    @property
    def is_multi_agent(self) -> bool:
        return bool(self.agents)

    @property
    def tag(self) -> str:
        return f"{self.name}@v{self.version}"

    def agent(self, agent_id: str) -> AgentSpec:
        for a in self.agents:
            if a.id == agent_id:
                return a
        raise KeyError(agent_id)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Workflow":
        return cls(name=d["name"], orchestrator=AgentSpec.from_dict(d["orchestrator"]),
                   agents=[AgentSpec.from_dict(a) for a in d.get("agents", [])],
                   protocol=d.get("protocol", ""), version=d.get("version", 0),
                   parent=d.get("parent"), notes=list(d.get("notes", [])))

    def design_dict(self) -> dict[str, Any]:
        """Only the evolvable part (what an optimizer may rewrite)."""
        d = self.to_dict()
        return {k: d[k] for k in ("orchestrator", "agents", "protocol")}

    def render(self) -> str:
        lines = [f"Workflow {self.tag}", f"Orchestrator ({self.orchestrator.role}): {self.orchestrator.instruction}"]
        for a in self.agents:
            lines.append(f"- {a.id} ({a.role}): {a.instruction}")
            if a.output_contract:
                lines.append(f"  Output to orchestrator: {a.output_contract}")
        if self.protocol:
            lines.append(f"Protocol: {self.protocol}")
        return "\n".join(lines)

    def validate(self, known_tools: set[str] | None = None) -> list[str]:
        errors = []
        ids = [a.id for a in self.agents]
        if len(ids) != len(set(ids)):
            errors.append("agent ids must be unique")
        if self.orchestrator.id in ids:
            errors.append("orchestrator id collides with a worker id")
        for a in [self.orchestrator, *self.agents]:
            if not a.id or not a.id.replace("_", "").replace("-", "").isalnum():
                errors.append(f"invalid agent id {a.id!r}")
            if not a.instruction.strip():
                errors.append(f"agent {a.id!r} has no instruction")
            if known_tools is not None and a.tools:
                unknown = set(a.tools) - known_tools
                if unknown:
                    errors.append(f"agent {a.id!r} uses unknown tools {sorted(unknown)}")
        return errors


def system_prompt(spec: AgentSpec, preamble: str = "") -> str:
    """Identity + instruction + output contract for an agent run outside an orchestrator."""
    text = f"{preamble}\n\nYou are {spec.id}, {spec.role}.\n{spec.instruction}".strip()
    return text + (f"\n\nOutput to orchestrator: {spec.output_contract}" if spec.output_contract else "")


class WorkflowStore:
    """Versioned workflows per stage on disk; ``current`` points at the retained one."""

    def __init__(self, root: Path):
        self.root = Path(root)

    def _dir(self, stage: str) -> Path:
        return self.root / stage

    def save(self, stage: str, wf: Workflow, make_current: bool = False) -> Path:
        d = self._dir(stage)
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"v{wf.version}.json"
        path.write_text(json.dumps(wf.to_dict(), indent=2))
        if make_current:
            (d / "current").write_text(str(wf.version))
        return path

    def versions(self, stage: str) -> list[int]:
        return sorted(int(p.stem[1:]) for p in self._dir(stage).glob("v*.json"))

    def load(self, stage: str, version: int | None = None) -> Workflow | None:
        d = self._dir(stage)
        if version is None:
            cur = d / "current"
            if not cur.exists():
                return None
            version = int(cur.read_text().strip())
        p = d / f"v{version}.json"
        return Workflow.from_dict(json.loads(p.read_text())) if p.exists() else None

    def current_or(self, stage: str, default: Workflow) -> Workflow:
        wf = self.load(stage)
        if wf is None:
            self.save(stage, default, make_current=True)
            return default
        return wf

    def next_version(self, stage: str) -> int:
        v = self.versions(stage)
        return (max(v) + 1) if v else 0


ORCHESTRATOR_PREAMBLE = """You are the orchestrator of a multi-agent team.
Delegate with `delegate` (one agent) or `delegate_parallel` (several agents at once).
Delegating again to an agent you already used RECALLS it with its full prior context;
use that for narrower follow-ups and updated acceptance criteria. Check each output
against its contract, iterate until acceptance criteria are met, then give the final answer."""


class WorkflowRunner:
    """Executes a :class:`Workflow` against a task with a given toolbox."""

    def __init__(self, router: ProviderRouter, *, max_steps: int = 40, max_workers: int = 4,
                 temperature: float | None = None):
        self.router = router
        self.max_steps = max_steps
        self.max_workers = max_workers
        self.temperature = temperature

    def _provider(self, stage: str, spec: AgentSpec):
        return self.router.for_role(spec.route or f"{stage}.{spec.id}")

    def run(self, workflow: Workflow, task: str, toolbox: Toolbox, *, stage: str,
            trajectory: Trajectory | None = None, seed: int | None = None,
            preamble: str = "") -> str:
        orch_spec = workflow.orchestrator
        if not workflow.is_multi_agent:
            agent = Agent(orch_spec.id, system_prompt(orch_spec, preamble),
                          self._provider(stage, orch_spec), toolbox.subset(orch_spec.tools),
                          self.max_steps, "single", self.temperature, seed, trajectory)
            return agent.run(task).output

        workers: dict[str, Agent] = {}
        locks: dict[str, threading.Lock] = {a.id: threading.Lock() for a in workflow.agents}
        guard = threading.Lock()

        def get_worker(agent_id: str) -> Agent:
            with guard:
                if agent_id not in workers:
                    spec = workflow.agent(agent_id)
                    workers[agent_id] = Agent(spec.id, system_prompt(spec, preamble), self._provider(stage, spec),
                                              toolbox.subset(spec.tools), self.max_steps, "worker",
                                              self.temperature, seed, trajectory)
                return workers[agent_id]

        def delegate(agent_id: str, task: str) -> str:
            if agent_id not in locks:
                return f"ERROR: no agent {agent_id!r}; team: {', '.join(locks)}"
            with locks[agent_id]:
                res = get_worker(agent_id).send(task)
            return res.output if res.finished else res.output + "\n[worker hit its step budget]"

        def delegate_parallel(assignments: list[dict[str, str]]) -> str:
            with ThreadPoolExecutor(self.max_workers) as pool:
                futs = [(a.get("agent_id", ""), pool.submit(delegate, a.get("agent_id", ""), a.get("task", "")))
                        for a in assignments]
                return json.dumps({aid: f.result() for aid, f in futs}, indent=1)

        orch_tools = toolbox.subset(orch_spec.tools).add(
            Tool("delegate", "Assign a task to one team member and wait for its output.",
                 schema(["agent_id", "task"], agent_id="string", task="string"), delegate),
            Tool("delegate_parallel", "Assign tasks to several team members concurrently.",
                 {"type": "object", "required": ["assignments"], "properties": {"assignments": {
                     "type": "array", "items": {"type": "object", "required": ["agent_id", "task"],
                                                "properties": {"agent_id": {"type": "string"},
                                                               "task": {"type": "string"}}}}}},
                 delegate_parallel),
        )
        system = "\n\n".join(x for x in [preamble, ORCHESTRATOR_PREAMBLE, orch_spec.instruction,
                                          "Team:\n" + workflow.render()] if x)
        orch = Agent(orch_spec.id, system, self._provider(stage, orch_spec), orch_tools,
                     self.max_steps, "orchestrator", self.temperature, seed, trajectory)
        return orch.run(task).output
