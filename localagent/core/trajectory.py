"""Trajectories: the complete record of every model conversation in a run.

They are the raw material for self-improvement strategies (MASS trains on
them) and an audit trail for humans.
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..providers.base import Message, ToolSpec, Usage


@dataclass
class Conversation:
    agent_id: str
    role: str  # "orchestrator" | "worker" | "single" | "judge" | ...
    messages: list[Message] = field(default_factory=list)
    tools: list[ToolSpec] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    model: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"agent_id": self.agent_id, "role": self.role, "model": self.model,
                "messages": [m.to_dict() for m in self.messages],
                "tools": [t.__dict__ for t in self.tools],
                "usage": self.usage.__dict__}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Conversation":
        return cls(d["agent_id"], d["role"], [Message.from_dict(m) for m in d["messages"]],
                   [ToolSpec(**t) for t in d.get("tools", [])], Usage(**d.get("usage", {})),
                   d.get("model", ""))


@dataclass
class Trajectory:
    task_id: str
    stage: str
    workflow: str = ""  # "<name>@v<k>"
    task_prompt: str = ""
    conversations: list[Conversation] = field(default_factory=list)
    output: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    created: float = field(default_factory=time.time)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def add(self, conv: Conversation) -> Conversation:
        with self._lock:
            self.conversations.append(conv)
        return conv

    @property
    def usage(self) -> Usage:
        total = Usage()
        for c in self.conversations:
            total = total + c.usage
        return total

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "task_id": self.task_id, "stage": self.stage, "workflow": self.workflow,
                "task_prompt": self.task_prompt, "output": self.output, "metadata": self.metadata,
                "created": self.created, "usage": self.usage.__dict__,
                "conversations": [c.to_dict() for c in self.conversations]}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Trajectory":
        return cls(task_id=d["task_id"], stage=d["stage"], workflow=d.get("workflow", ""),
                   task_prompt=d.get("task_prompt", ""),
                   conversations=[Conversation.from_dict(c) for c in d.get("conversations", [])],
                   output=d.get("output", ""), metadata=d.get("metadata", {}), id=d["id"],
                   created=d.get("created", 0.0))


class TrajectoryStore:
    """Append-only JSONL store, one file per stage."""

    def __init__(self, root: Path):
        self.root = Path(root)

    def save(self, t: Trajectory) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        with (self.root / f"{t.stage}.jsonl").open("a") as f:
            f.write(json.dumps(t.to_dict(), default=str) + "\n")

    def load(self, stage: str | None = None) -> list[Trajectory]:
        files = [self.root / f"{stage}.jsonl"] if stage else sorted(self.root.glob("*.jsonl"))
        out = []
        for p in files:
            if p.exists():
                out += [Trajectory.from_dict(json.loads(line)) for line in p.read_text().splitlines() if line]
        return out
