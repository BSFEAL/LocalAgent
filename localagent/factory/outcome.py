"""What the software 'builder' hands the factory: an outcome, not instructions."""
from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class Outcome:
    title: str
    description: str = ""
    acceptance_criteria: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    origin: str = "builder"  # "builder" | "perf_factory" | "sevbot"
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def id(self) -> str:
        return hashlib.sha1(f"{self.title}\n{self.description}".encode()).hexdigest()[:10]

    def to_prompt(self) -> str:
        parts = [f"# Outcome: {self.title}"]
        if self.description:
            parts.append(self.description)
        if self.acceptance_criteria:
            parts.append("## Acceptance criteria\n" + "\n".join(f"- {c}" for c in self.acceptance_criteria))
        if self.constraints:
            parts.append("## Constraints\n" + "\n".join(f"- {c}" for c in self.constraints))
        return "\n\n".join(parts)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
