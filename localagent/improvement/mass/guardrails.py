"""Structural guardrails on proposed workflows (keeps the evolutionary search sane)."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from ...core.workflow import Workflow

# The paper's runs forbid arbitrary numeric caps on hops/recalls/turns: termination
# must come from acceptance gates, not budgets the optimizer invents.
_CAP_PATTERNS = [
    re.compile(r"\b(?:maximum|max(?:imum)?\s+of|at\s+most|up\s+to|no\s+more\s+than)\s+\d+\s+"
               r"(?:agent|subagent|recall|re-?delegation|hop|turn|tool|iteration|round|minute|hour)s?\b", re.I),
    re.compile(r"\b(?:agent|subagent|recall|re-?delegation|hop|turn|tool)s?(?:\s+\w+){0,3}\s+(?:is|are)?\s*"
               r"(?:limited|capped)\s+to\s+\d+\b", re.I),
]


@dataclass
class StructuralGuardrails:
    min_agents: int = 0
    max_agents: int = 8
    max_design_chars: int = 24_000
    require_output_contracts: bool = True
    forbid_numeric_caps: bool = True
    known_tools: set[str] | None = None
    required_agent_ids: list[str] = field(default_factory=list)  # e.g. fixed reviewer slots

    def check(self, wf: Workflow) -> list[str]:
        errors = wf.validate(self.known_tools)
        n = len(wf.agents)
        if n < self.min_agents:
            errors.append(f"needs at least {self.min_agents} worker agents (has {n})")
        if n > self.max_agents:
            errors.append(f"at most {self.max_agents} worker agents allowed (has {n})")
        size = len(json.dumps(wf.design_dict()))
        if size > self.max_design_chars:
            errors.append(f"design is {size} chars; limit {self.max_design_chars}")
        if self.require_output_contracts:
            missing = [a.id for a in wf.agents if not a.output_contract.strip()]
            if missing:
                errors.append(f"agents without an output contract: {missing}")
        if self.forbid_numeric_caps:
            text = json.dumps(wf.design_dict())
            caps = sorted({m.group(0) for p in _CAP_PATTERNS for m in p.finditer(text)})
            if caps:
                errors.append(f"remove numeric caps (use acceptance-based termination): {caps}")
        ids = {a.id for a in wf.agents}
        for rid in self.required_agent_ids:
            if rid not in ids:
                errors.append(f"required agent {rid!r} was removed")
        return errors
