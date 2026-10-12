"""Track what an agent changed in a workspace (content snapshots; works with or without git)."""
from __future__ import annotations

import difflib
import hashlib
from dataclasses import dataclass, field

from ..core.tools import Workspace


@dataclass
class ChangeSet:
    files: list[str] = field(default_factory=list)
    diff: str = ""
    added_lines: int = 0
    removed_lines: int = 0

    @property
    def empty(self) -> bool:
        return not self.files

    def summary(self) -> str:
        return f"{len(self.files)} files, +{self.added_lines}/-{self.removed_lines}"


class ChangeTracker:
    def __init__(self, ws: Workspace):
        self.ws = ws
        self._before: dict[str, tuple[str, str]] = {}

    def start(self) -> None:
        self._before = self._snapshot()

    def _snapshot(self) -> dict[str, tuple[str, str]]:
        snap = {}
        for rel in self.ws.files():
            try:
                text = (self.ws.root / rel).read_text()
            except (UnicodeDecodeError, OSError):
                text = ""
            snap[rel] = (hashlib.sha1(text.encode()).hexdigest(), text)
        return snap

    def collect(self) -> ChangeSet:
        after = self._snapshot()
        files = sorted(f for f in set(self._before) | set(after)
                       if self._before.get(f, ("",))[0] != after.get(f, ("",))[0])
        chunks, add, rem = [], 0, 0
        for f in files:
            a = self._before.get(f, ("", ""))[1].splitlines(keepends=True)
            b = after.get(f, ("", ""))[1].splitlines(keepends=True)
            for line in difflib.unified_diff(a, b, f"a/{f}", f"b/{f}"):
                chunks.append(line if line.endswith("\n") else line + "\n")
                if line.startswith("+") and not line.startswith("+++"):
                    add += 1
                elif line.startswith("-") and not line.startswith("---"):
                    rem += 1
        return ChangeSet(files, "".join(chunks), add, rem)
