"""OpenAI Chat Completions wire format.

Also the native API of vLLM, SGLang, Ollama, LM Studio, llama.cpp server,
OpenRouter, Together, Groq, Fireworks, DeepSeek, Mistral and many others, so
one adapter covers both hosted and fully local models.
"""
from __future__ import annotations

import json
import os
from typing import Any

from ._http import post_json
from .base import Completion, Message, Provider, ProviderError, ToolCall, Usage


class OpenAICompatProvider(Provider):
    name = "openai"

    def __init__(self, model: str, *, base_url: str | None = None, api_key: str | None = None,
                 api_key_env: str = "OPENAI_API_KEY", extra_body: dict[str, Any] | None = None,
                 extra_headers: dict[str, str] | None = None, timeout: float = 600, **kw: Any):
        super().__init__(model, **kw)
        self.base_url = (base_url or os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1").rstrip("/")
        self.api_key = api_key if api_key is not None else os.environ.get(api_key_env, "")
        self.extra_body = extra_body or {}
        self.extra_headers = extra_headers or {}
        self.timeout = timeout

    @staticmethod
    def _to_wire(m: Message) -> dict[str, Any]:
        if m.role == "tool":
            return {"role": "tool", "tool_call_id": m.tool_call_id, "content": m.content}
        out: dict[str, Any] = {"role": m.role, "content": m.content}
        if m.tool_calls:
            out["tool_calls"] = [{"id": c.id, "type": "function",
                                  "function": {"name": c.name, "arguments": json.dumps(c.arguments)}}
                                 for c in m.tool_calls]
            if not m.content:
                out["content"] = None
        return out

    def complete(self, messages, tools=None, *, temperature=None, max_tokens=None, seed=None):
        payload: dict[str, Any] = {"model": self.model, "messages": [self._to_wire(m) for m in messages]}
        t = temperature if temperature is not None else self.temperature
        if t is not None:
            payload["temperature"] = t
        mt = max_tokens or self.max_tokens
        if mt:
            payload["max_tokens"] = mt
        if seed is not None:
            payload["seed"] = seed
        if tools:
            payload["tools"] = [{"type": "function", "function": {
                "name": s.name, "description": s.description, "parameters": s.parameters}} for s in tools]
        payload.update(self.extra_body)
        headers = dict(self.extra_headers)
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        data = post_json(f"{self.base_url}/chat/completions", payload, headers, timeout=self.timeout)
        try:
            choice = data["choices"][0]
            msg = choice["message"]
        except (KeyError, IndexError) as e:
            raise ProviderError(f"unexpected response from {self.spec}: {str(data)[:500]}") from e
        calls = []
        for c in msg.get("tool_calls") or []:
            fn = c.get("function", {})
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {"_raw": fn.get("arguments")}
            calls.append(ToolCall(name=fn.get("name", ""), arguments=args, id=c.get("id") or ToolCall("", {}).id))
        usage = data.get("usage") or {}
        return Completion(
            message=Message("assistant", msg.get("content") or "", tool_calls=calls,
                            reasoning=msg.get("reasoning_content") or msg.get("reasoning")),
            usage=Usage(usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)),
            model=data.get("model", self.model), stop_reason=choice.get("finish_reason"), raw=data)
