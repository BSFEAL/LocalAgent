"""Deterministic offline providers for tests, demos and dry runs."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .base import Completion, Message, Provider, ToolCall, ToolSpec, Usage

Responder = Callable[[list[Message], list[ToolSpec] | None], "Message | str"]


def _approx_tokens(text: str) -> int:
    return max(1, len(text) // 4)


class ScriptedProvider(Provider):
    """Replies from a queue of canned responses or a responder function.

    A response may be a string, a ``Message``, or a dict
    ``{"tool": name, "args": {...}}`` / ``{"tools": [...]}`` for tool calls.
    """

    name = "mock"

    def __init__(self, model: str = "scripted", responses: list[Any] | None = None,
                 responder: Responder | None = None, **kw: Any):
        super().__init__(model, **kw)
        self.responses = list(responses or [])
        self.responder = responder
        self.calls: list[list[Message]] = []

    def complete(self, messages, tools=None, *, temperature=None, max_tokens=None, seed=None):
        self.calls.append(list(messages))
        if self.responses:
            r = self.responses.pop(0)
        elif self.responder:
            r = self.responder(messages, tools)
        else:
            r = "ok"
        msg = self._coerce(r)
        prompt = sum(_approx_tokens(m.content) for m in messages)
        return Completion(msg, Usage(prompt, _approx_tokens(msg.content) + 8 * len(msg.tool_calls)),
                          model=self.model, stop_reason="tool_calls" if msg.tool_calls else "stop")

    @staticmethod
    def _coerce(r: Any) -> Message:
        if isinstance(r, Message):
            return r
        if isinstance(r, dict):
            specs = r.get("tools") or ([{"tool": r["tool"], "args": r.get("args", {})}] if "tool" in r else [])
            return Message("assistant", r.get("content", ""),
                           tool_calls=[ToolCall(s["tool"], s.get("args", {})) for s in specs])
        return Message("assistant", str(r))


class EchoProvider(ScriptedProvider):
    """Echoes the last user message; handy for wiring checks."""

    name = "echo"

    def __init__(self, model: str = "echo", **kw: Any):
        super().__init__(model, responder=lambda msgs, _t: next(
            (m.content for m in reversed(msgs) if m.role == "user"), ""), **kw)
