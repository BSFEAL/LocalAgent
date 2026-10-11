"""Provider-neutral message model and the ``Provider`` interface.

Every stage of the factory and every improvement strategy talks to models only
through these types, so swapping OpenAI for Anthropic, Gemini, a local vLLM /
Ollama server, or a test double never touches framework code.
"""
from __future__ import annotations

import json
import re
import uuid
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class ToolCall:
    name: str
    arguments: dict[str, Any]
    id: str = field(default_factory=lambda: "call_" + uuid.uuid4().hex[:12])


@dataclass
class Message:
    role: str  # "system" | "user" | "assistant" | "tool"
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str | None = None
    name: str | None = None  # tool name for role="tool"
    reasoning: str | None = None  # provider "thinking" text, kept for SFT traces

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return {k: v for k, v in d.items() if v not in (None, [], "") or k in ("role", "content")}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Message":
        calls = [ToolCall(**c) for c in d.get("tool_calls") or []]
        return cls(role=d["role"], content=d.get("content") or "", tool_calls=calls,
                   tool_call_id=d.get("tool_call_id"), name=d.get("name"),
                   reasoning=d.get("reasoning"))


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema object


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(self.input_tokens + other.input_tokens, self.output_tokens + other.output_tokens)


@dataclass
class Completion:
    message: Message
    usage: Usage = field(default_factory=Usage)
    model: str = ""
    stop_reason: str | None = None
    raw: dict[str, Any] | None = None


class ProviderError(RuntimeError):
    pass


class Provider(ABC):
    """A chat model with optional tool calling.

    Implementations translate the neutral ``Message``/``ToolSpec`` types to a
    vendor wire format and back. They must not hold conversation state.
    """

    name: str = "provider"

    def __init__(self, model: str, *, temperature: float | None = None,
                 max_tokens: int | None = None, **options: Any):
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.options = options

    @property
    def spec(self) -> str:
        return f"{self.name}:{self.model}"

    @abstractmethod
    def complete(self, messages: list[Message], tools: list[ToolSpec] | None = None, *,
                 temperature: float | None = None, max_tokens: int | None = None,
                 seed: int | None = None) -> Completion:
        ...

    def complete_json(self, system: str, user: str, *, seed: int | None = None,
                      retries: int = 2) -> tuple[Any, Completion]:
        """Ask for JSON and parse it. Works on any provider (no native JSON mode needed)."""
        messages = [Message("system", system + "\nRespond with a single valid JSON value only."),
                    Message("user", user)]
        last_err: Exception | None = None
        for _ in range(retries + 1):
            comp = self.complete(messages, seed=seed)
            try:
                return extract_json(comp.message.content), comp
            except ValueError as e:
                last_err = e
                messages += [comp.message,
                             Message("user", f"That was not valid JSON ({e}). Return only the JSON.")]
        raise ProviderError(f"{self.spec} did not return JSON: {last_err}")

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.spec}>"


_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def extract_json(text: str) -> Any:
    """Parse JSON from model output, tolerating code fences and surrounding prose."""
    text = (text or "").strip()
    candidates = [text] + _FENCE.findall(text)
    for c in candidates:
        try:
            return json.loads(c)
        except (json.JSONDecodeError, TypeError):
            pass
    # Fall back to the outermost balanced {...} or [...]
    for open_c, close_c in (("{", "}"), ("[", "]")):
        start = text.find(open_c)
        while start != -1:
            depth, in_str, esc = 0, False, False
            for i in range(start, len(text)):
                ch = text[i]
                if in_str:
                    if esc:
                        esc = False
                    elif ch == "\\":
                        esc = True
                    elif ch == '"':
                        in_str = False
                    continue
                if ch == '"':
                    in_str = True
                elif ch == open_c:
                    depth += 1
                elif ch == close_c:
                    depth -= 1
                    if depth == 0:
                        try:
                            return json.loads(text[start:i + 1])
                        except json.JSONDecodeError:
                            break
            start = text.find(open_c, start + 1)
    raise ValueError("no JSON object found")
