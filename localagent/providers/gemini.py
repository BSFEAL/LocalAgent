"""Google Gemini ``generateContent`` wire format."""
from __future__ import annotations

import os
from typing import Any

from ._http import post_json
from .base import Completion, Message, Provider, ProviderError, ToolCall, Usage

_UNSUPPORTED_SCHEMA_KEYS = {"additionalProperties", "$schema", "default"}


def _clean_schema(s: Any) -> Any:
    if isinstance(s, dict):
        return {k: _clean_schema(v) for k, v in s.items() if k not in _UNSUPPORTED_SCHEMA_KEYS}
    if isinstance(s, list):
        return [_clean_schema(x) for x in s]
    return s


class GeminiProvider(Provider):
    name = "gemini"

    def __init__(self, model: str, *, base_url: str | None = None, api_key: str | None = None,
                 timeout: float = 600, **kw: Any):
        super().__init__(model, **kw)
        self.base_url = (base_url or "https://generativelanguage.googleapis.com/v1beta").rstrip("/")
        self.api_key = api_key if api_key is not None else (
            os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY", ""))
        self.timeout = timeout

    def _to_wire(self, messages: list[Message]) -> tuple[str, list[dict[str, Any]]]:
        system = "\n\n".join(m.content for m in messages if m.role == "system")
        names = {c.id: c.name for m in messages for c in m.tool_calls}
        out: list[dict[str, Any]] = []
        for m in messages:
            if m.role == "system":
                continue
            if m.role == "tool":
                role, parts = "user", [{"functionResponse": {
                    "name": m.name or names.get(m.tool_call_id or "", "tool"),
                    "response": {"content": m.content}}}]
            else:
                role = "model" if m.role == "assistant" else "user"
                parts = ([{"text": m.content}] if m.content else []) + [
                    {"functionCall": {"name": c.name, "args": c.arguments}} for c in m.tool_calls]
                parts = parts or [{"text": ""}]
            if out and out[-1]["role"] == role:
                out[-1]["parts"].extend(parts)
            else:
                out.append({"role": role, "parts": parts})
        return system, out

    def complete(self, messages, tools=None, *, temperature=None, max_tokens=None, seed=None):
        system, contents = self._to_wire(messages)
        payload: dict[str, Any] = {"contents": contents}
        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}
        gen: dict[str, Any] = {}
        t = temperature if temperature is not None else self.temperature
        if t is not None:
            gen["temperature"] = t
        if max_tokens or self.max_tokens:
            gen["maxOutputTokens"] = max_tokens or self.max_tokens
        if seed is not None:
            gen["seed"] = seed
        if gen:
            payload["generationConfig"] = gen
        if tools:
            payload["tools"] = [{"functionDeclarations": [
                {"name": s.name, "description": s.description, "parameters": _clean_schema(s.parameters)}
                for s in tools]}]
        data = post_json(f"{self.base_url}/models/{self.model}:generateContent", payload,
                         {"x-goog-api-key": self.api_key}, timeout=self.timeout)
        try:
            cand = data["candidates"][0]
            parts = cand.get("content", {}).get("parts", [])
        except (KeyError, IndexError) as e:
            raise ProviderError(f"unexpected response from {self.spec}: {str(data)[:500]}") from e
        text, thoughts, calls = [], [], []
        for p in parts:
            if "functionCall" in p:
                fc = p["functionCall"]
                calls.append(ToolCall(name=fc["name"], arguments=fc.get("args") or {}))
            elif p.get("thought"):
                thoughts.append(p.get("text", ""))
            elif "text" in p:
                text.append(p["text"])
        u = data.get("usageMetadata") or {}
        return Completion(Message("assistant", "".join(text), tool_calls=calls,
                                  reasoning="\n".join(thoughts) or None),
                          Usage(u.get("promptTokenCount", 0), u.get("candidatesTokenCount", 0)),
                          model=self.model, stop_reason=cand.get("finishReason"), raw=data)
