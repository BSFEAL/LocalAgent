"""In-process event bus + JSONL ledger. Every factory stage reports through it."""
from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Event:
    kind: str  # e.g. "coder.done", "ci.failed", "review.finding", "deploy.rollback"
    data: dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)


Listener = Callable[[Event], None]


class EventBus:
    def __init__(self, ledger: Path | None = None, echo: bool = False):
        self.ledger = ledger
        self.echo = echo
        self._listeners: list[tuple[str, Listener]] = []
        self._lock = threading.Lock()
        self.history: list[Event] = []

    def subscribe(self, prefix: str, fn: Listener) -> None:
        """Receive every event whose kind starts with ``prefix`` ("" = all)."""
        self._listeners.append((prefix, fn))

    def emit(self, kind: str, **data: Any) -> Event:
        ev = Event(kind, data)
        with self._lock:
            self.history.append(ev)
            if self.ledger:
                self.ledger.parent.mkdir(parents=True, exist_ok=True)
                with self.ledger.open("a") as f:
                    f.write(json.dumps({"ts": ev.ts, "kind": kind, **data}, default=str) + "\n")
        if self.echo:
            summary = ", ".join(f"{k}={str(v)[:60]}" for k, v in data.items())
            print(f"[{kind}] {summary}", flush=True)
        for prefix, fn in list(self._listeners):
            if kind.startswith(prefix):
                fn(ev)
        return ev

    def of(self, prefix: str) -> list[Event]:
        return [e for e in self.history if e.kind.startswith(prefix)]
