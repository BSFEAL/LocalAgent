"""The paper slot.

A self-improvement method from a paper plugs into the factory by implementing
:class:`ImprovementStrategy`. The factory hands it an
:class:`ImprovementContext` - providers, versioned stage workflows, a task
bank, recorded trajectories and a task executor - and the strategy may:

* retain new workflow versions per stage (picked up by the factory at once);
* emit fine-tuning datasets and, through a pluggable trainer, a new model
  spec that becomes the router's default (the next generation L^(k+1)).

MASS (arXiv:2610.12176) ships as ``"mass"``; ``"noop"`` disables improvement.
Register another paper with :func:`register_strategy`.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import uuid
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol

from ..core.context import ContextHub
from ..core.events import EventBus
from ..core.tools import Workspace
from ..core.trajectory import Trajectory, TrajectoryStore
from ..core.workflow import Workflow, WorkflowRunner, WorkflowStore
from ..providers.registry import ProviderRouter


@dataclass
class Task:
    id: str
    stage: str
    prompt: str
    split: str = "train"  # "train" | "test" (test tasks never enter training data)
    template_dir: str | None = None  # copied into a fresh workspace
    repo: str | None = None  # or: git repo + rev exported into the workspace
    rev: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Task":
        return cls(**{k: d.get(k) for k in ("id", "stage", "prompt")}, split=d.get("split", "train"),
                   template_dir=d.get("template_dir"), repo=d.get("repo"), rev=d.get("rev"),
                   metadata=d.get("metadata") or {})


class TaskBank:
    """JSONL task store. The factory adds every built outcome as a coder task."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def all(self) -> list[Task]:
        if not self.path.exists():
            return []
        seen: dict[str, Task] = {}
        for line in self.path.read_text().splitlines():
            if line:
                t = Task.from_dict(json.loads(line))
                seen[f"{t.stage}/{t.id}"] = t  # later lines override
        return list(seen.values())

    def add(self, task: Task) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as f:
            f.write(json.dumps(asdict(task)) + "\n")

    def get(self, stage: str, split: str | None = None) -> list[Task]:
        return [t for t in self.all() if t.stage == stage and (split is None or t.split == split)]


@dataclass
class Execution:
    task: Task
    workflow: Workflow
    output: str
    deliverable: str  # what a judge sees
    trajectory: Trajectory
    workspace: Path
    checks: dict[str, bool] = field(default_factory=dict)  # verifiable signals, e.g. {"ci": True}

    @property
    def output_tokens(self) -> int:
        return self.trajectory.usage.output_tokens


class TaskExecutor(Protocol):
    def execute(self, workflow: Workflow, task: Task, *, seed: int | None = None) -> Execution: ...


class WorkspaceExecutor:
    """Runs a workflow on a task in an isolated, fresh workspace."""

    def __init__(self, runner: WorkflowRunner, root: Path, *, ci: Any = None, context: ContextHub | None = None,
                 preamble: str = "", keep_workspaces: bool = True):
        self.runner, self.root, self.ci, self.context = runner, Path(root), ci, context
        self.preamble, self.keep = preamble, keep_workspaces

    def _prepare(self, task: Task) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        dest = Path(tempfile.mkdtemp(prefix=f"{task.stage}-{task.id}-", dir=self.root))
        if task.template_dir:
            shutil.copytree(task.template_dir, dest, dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns(*Workspace.IGNORE))
        elif task.repo and task.rev:
            archive = subprocess.run(["git", "-C", task.repo, "archive", task.rev], capture_output=True, check=True)
            subprocess.run(["tar", "-x", "-C", str(dest)], input=archive.stdout, check=True)
        return dest

    def execute(self, workflow: Workflow, task: Task, *, seed: int | None = None) -> Execution:
        from ..factory.changes import ChangeTracker  # local import: factory depends on this module
        path = self._prepare(task)
        ws = Workspace(path)
        tools = ws.toolbox()
        if self.context:
            tools.add(self.context.tool())
        tracker = ChangeTracker(ws)
        tracker.start()
        traj = Trajectory(task.id, task.stage, workflow.tag, task.prompt, metadata={"seed": seed})
        output = self.runner.run(workflow, task.prompt, tools, stage=task.stage, trajectory=traj,
                                 seed=seed, preamble=self.preamble)
        traj.output = output
        changes = tracker.collect()
        checks = {}
        if self.ci is not None:
            checks["ci"] = self.ci.run(ws).passed
        deliverable = (f"FINAL ANSWER:\n{output}\n\nCHANGES ({changes.summary()}):\n{changes.diff[:30_000]}"
                       if task.repo or task.template_dir else f"FINAL ANSWER:\n{output}\n\n{ws.snapshot()}")
        traj.metadata.update(checks=checks, workspace=str(path))
        return Execution(task, workflow, output, deliverable, traj, path, checks)


@dataclass
class ImprovementContext:
    router: ProviderRouter
    workflows: WorkflowStore
    tasks: TaskBank
    trajectories: TrajectoryStore
    executors: dict[str, TaskExecutor]
    default_workflows: dict[str, Workflow]
    run_dir: Path
    bus: EventBus

    def executor(self, stage: str) -> TaskExecutor:
        return self.executors.get(stage) or self.executors["*"]

    def workflow(self, stage: str) -> Workflow:
        return self.workflows.current_or(stage, self.default_workflows[stage])


@dataclass
class ImprovementReport:
    strategy: str
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    stages: dict[str, dict[str, Any]] = field(default_factory=dict)
    new_model: str | None = None
    artifacts: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ImprovementStrategy(ABC):
    name = "strategy"

    def __init__(self, **config: Any):
        self.config = config

    def observe(self, trajectory: Trajectory) -> None:
        """Online hook: called for every trajectory the factory records."""

    @abstractmethod
    def run_cycle(self, ctx: ImprovementContext, stages: list[str]) -> ImprovementReport:
        """One offline improvement cycle over the given stages."""


class NoopStrategy(ImprovementStrategy):
    name = "noop"

    def run_cycle(self, ctx, stages):
        return ImprovementReport(self.name)


_STRATEGIES: dict[str, type[ImprovementStrategy]] = {}


def register_strategy(name: str, cls: type[ImprovementStrategy]) -> None:
    _STRATEGIES[name] = cls


def available_strategies() -> list[str]:
    return sorted(_STRATEGIES)


def create_strategy(name: str, **config: Any) -> ImprovementStrategy:
    if name not in _STRATEGIES:
        raise ValueError(f"unknown improvement strategy {name!r}; available: {available_strategies()}")
    return _STRATEGIES[name](**config)


register_strategy("noop", NoopStrategy)
