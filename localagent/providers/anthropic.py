"""Anthropic Messages API wire format."""
from __future__ import annotations

import os
from typing import Any

from ._http import post_json
from .base import Completion, Message, Provider, ProviderError, ToolCall, Usage


class AnthropicProvider(Provider):
    name = "anthropic"

    def __init__(self, model: str, *, base_url: str | None = None, api_key: str | None = None,
                 api_version: str = "2023-06-01", timeout: float = 600, **kw: Any):
        super().__init__(model, **kw)
        self.base_url = (base_url or os.environ.get("ANTHROPIC_BASE_URL") or "https://api.anthropic.com").rstrip("/")
        self.api_key = api_key if api_key is not None else os.environ.get("ANTHROPIC_API_KEY", "")
        self.api_version = api_version
        self.timeout = timeout

    @staticmethod
    def _blocks(m: Message) -> list[dict[str, Any]]:
        if m.role == "tool":
            return [{"type": "tool_result", "tool_use_id": m.tool_call_id, "content": m.content}]
        blocks: list[dict[str, Any]] = []
        if m.content:
            blocks.append({"type": "text", "text": m.content})
        for c in m.tool_calls:
            blocks.append({"type": "tool_use", "id": c.id, "name": c.name, "input": c.arguments})
        return blocks or [{"type": "text", "text": ""}]

    def _to_wire(self, messages: list[Message]) -> tuple[str, list[dict[str, Any]]]:
        system = "\n\n".join(m.content for m in messages if m.role == "system")
        out: list[dict[str, Any]] = []
        for m in messages:
            if m.role == "system":
                continue
            role = "assistant" if m.role == "assistant" else "user"  # tool results ride on user turns
            blocks = self._blocks(m)
            if out and out[-1]["role"] == role:
                out[-1]["content"].extend(blocks)
            else:
                out.append({"role": role, "content": blocks})
        return system, out

    def complete(self, messages, tools=None, *, temperature=None, max_tokens=None, seed=None):
        system, wire = self._to_wire(messages)
        payload: dict[str, Any] = {"model": self.model, "messages": wire,
                                   "max_tokens": max_tokens or self.max_tokens or 8192}
        if system:
            payload["system"] = system
        t = temperature if temperature is not None else self.temperature
        if t is not None:
            payload["temperature"] = t
        if tools:
            payload["tools"] = [{"name": s.name, "description": s.description, "input_schema": s.parameters}
                                for s in tools]
        payload.update(self.options.get("extra_body", {}))
        headers = {"x-api-key": self.api_key, "anthropic-version": self.api_version}
        data = post_json(f"{self.base_url}/v1/messages", payload, headers, timeout=self.timeout)
        if "content" not in data:
            raise ProviderError(f"unexpected response from {self.spec}: {str(data)[:500]}")
        text, thinking, calls = [], [], []
        for b in data["content"]:
            if b.get("type") == "text":
                text.append(b.get("text", ""))
            elif b.get("type") == "thinking":
                thinking.append(b.get("thinking", ""))
            elif b.get("type") == "tool_use":
                calls.append(ToolCall(name=b["name"], arguments=b.get("input") or {}, id=b["id"]))
        u = data.get("usage") or {}
        return Completion(Message("assistant", "".join(text), tool_calls=calls,
                                  reasoning="\n".join(thinking) or None),
                          Usage(u.get("input_tokens", 0), u.get("output_tokens", 0)),
                          model=data.get("model", self.model), stop_reason=data.get("stop_reason"), raw=data)
